# Databricks notebook source
# MAGIC %md
# MAGIC # Canonical token preview
# MAGIC
# MAGIC Applies the dictionary of match keys to the DEV passages and to the UAT chat questions, and shows the text before and after. Nothing is written: the passages and the index stay as they are.
# MAGIC
# MAGIC | | |
# MAGIC | --- | --- |
# MAGIC | Reads | `dev_proj.qualibot.f3_cles_codes` (dictionary) and `dev_proj.qualibot.f2_familles_codes` (patterns), written by the code patterns notebook, `dev_proj.qualibot.chunks`, `uat_proj.qualibot.chat_turns` |
# MAGIC | Writes | nothing |
# MAGIC | Rights | the user's own |
# MAGIC
# MAGIC ## Matching rules
# MAGIC
# MAGIC Codes are found in the text by one generic matcher and a dictionary lookup: no code regex is applied to the text. The code patterns only decide which keys enter the dictionary.
# MAGIC
# MAGIC | ID | Rule | Regex or condition | Example |
# MAGIC | --- | --- | --- | --- |
# MAGIC | `MTC-01` | Split into pieces | `[A-Za-z]+\|[0-9]+` | `Q0375MI` → `Q`, `0375`, `MI` |
# MAGIC | `MTC-02` | Gap allowed between two pieces | nothing; or up to 3 spaces, one or two separators (`-`, unicode dashes, `_`, `/`, `.`), up to 3 spaces; or 1 to 3 spaces | `MI - 1365`, `IAQ 04 04 05`, `19 --058`, `TDA8 - 00/027` |
# MAGIC | `MTC-02b` | Words next to a space | a gap holding a space joins only digits or upper-case words of at most 4 letters, and the code never ends on a word after a space; a tight separator joins any case | `BAC 5578`, `sf-1027`; never `of 10`, `Le 23`, `R30 Develop`, `STRUCTURE 1000`, `NASM21059 - NUT` |
# MAGIC | `MTC-02c` | Codes without digits | 3 letters or more, upper case, a separator without spaces in every gap | `E.A.S.A`, `N.D.T`, `M.O.E`; never `A/B`, `N/A`, `y a`, `e.g`, `SAP` in `Regles_SAP_controle` |
# MAGIC | `MTC-02d` | Decimal numbers | a number never starts or stops at a decimal point or comma | never `T31` in `T3 1,2`, `R0` in `R 0,55`, `DA9` in `DA 9.10` |
# MAGIC | `MTC-03` | Start | at the start of a word | nothing in `XSF-1027` |
# MAGIC | `MTC-03b` | Inside a chain | a code preceded or followed by another part joined by `-` `_` `/` `.` needs a key of 5 characters or more | `IP-025/Installation`, `GO-1585-B`; never `A01` in `PTD-TEST-6-A-01`, `A30` in `145.A.30`, `V20` in `Rev2025.11_v2.0` |
# MAGIC | `MTC-04` | End | at the end of a word, or between a digit and a glued word of letters when the code holds a separator | `MI-13991` in `MI-13991Table`; never `F7` in `F7XC5355`, `A2` in `A2PFACT` |
# MAGIC | `MTC-05` | Choice | the longest run of at most 12 pieces whose glued key is in the dictionary | `QIS-46A-352 A` rather than `QIS-46A-352` |
# MAGIC | `MTC-06` | One token per document | every match key maps to one canonical token | `IT-243` and `LdB IT-243` → `LDBIT243`; `Q0697MI_FR` → `Q0697MI` |
# MAGIC | `MTC-07` | Output | token appended after the word, in brackets; nothing added when the word already is the token; a second pass adds nothing | `SF-1027` → `SF-1027 [SF1027]`; `SF1027` unchanged |

# COMMAND ----------

# Parameters

import logging
import sys

import pandas as pd
from pyspark.sql import Window
from pyspark.sql import functions as F

DICTIONARY = "dev_proj.qualibot.f3_cles_codes"
PATTERNS = "dev_proj.qualibot.f2_familles_codes"
PASSAGES = "dev_proj.qualibot.chunks"
QUESTIONS = "uat_proj.qualibot.chat_turns"
EXAMPLES = 15   # before / after examples per pattern

logger = logging.getLogger("canonical_token_preview")
if not logger.handlers:
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(_handler)
logger.setLevel(logging.INFO)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Matcher
# MAGIC The rules `MTC-01` to `MTC-07` in code. `find` returns the codes of a text with their token, `annotate` returns the text with the tokens appended.

# COMMAND ----------

import re
from typing import Iterable, Mapping, NamedTuple

# --- matcher: start ---
# Bump when the matching or the key changes: stored annotations are recomputed for the new version.
PP_VERSION = 3

# A piece is letters only or digits only (Q0375MI gives Q, 0375, MI).
_PIECE = re.compile(r'[A-Za-z]+|[0-9]+')
# Gap allowed between two pieces of one code: nothing, up to 3 spaces around one or two separators (hyphens,
# unicode dashes, _ / .), or 1 to 3 spaces alone (IAQ 04 04 05, MI - 1365, 19 --058).
_GAP = re.compile(r'(?:[  ]{0,3}[-‐‑−–—_/.]{1,2}[  ]{0,3}|[  ]{1,3})?')
_SPACE = re.compile(r'[ \u00a0]')
# Separators that chain codes into one longer identifier (PTD-TEST-6-A-01, 145.A.30, Rev2025.11_v2.0).
_CHAIN = '-\u2010\u2011\u2212\u2013\u2014_/.'
# Shortest key accepted inside such a chain: shorter ones (A01, A30, V20) are parts of a longer identifier.
_MIN_KEY_IN_CHAIN = 5
# Longest word that a space may join to a code: longer ones are words (STRUCTURE 1000, ALUMINUM 7475).
_MAX_WORD_NEXT_TO_SPACE = 4
# Shortest code written without digits (E.A.S.A, N.D.T): shorter ones are mostly ranges and pairs (A/B, X-Y).
_MIN_LETTERS_KEY = 3
# Longest code, in pieces (IF20037_DLV-31-5207 has 6).
_MAX_PIECES = 12


class CodeMatch(NamedTuple):
    """One known code found in a text."""

    start: int
    end: int
    key: str


def _word_end(text: str, pos: int) -> int:
    """End of the word that contains ``pos``; ``_`` joins a word, as in the keyword tokenizer (``Q0697MI_FR``).

    :param text: text
    :param pos: position in the text
    :return: index of the first character after the word
    """
    while pos < len(text) and (text[pos].isalnum() or text[pos] == '_'):
        pos += 1
    return pos


def _strong(piece: str) -> bool:
    """Whether a piece may sit next to a gap with a space: digits, or a short word in upper case.

    :param piece: letters only or digits only
    :return: True for ``1027`` or ``BAC``, False for ``of``, ``Part`` or ``STRUCTURE``
    """
    return piece.isdigit() or (piece.isupper() and len(piece) <= _MAX_WORD_NEXT_TO_SPACE)


def _inside_number(text: str, pos: int) -> bool:
    """Whether the digit at ``pos`` follows a decimal point or comma of the same number (``5`` in ``0,55``).

    :param text: text
    :param pos: position of a digit, or just after a run of digits for ``pos - 1``
    :return: True when ``text[pos - 2:pos + 1]`` reads digit, ``.`` or ``,``, digit
    """
    return (1 < pos < len(text) and text[pos - 1] in '.,' and text[pos - 2].isdigit()
            and text[pos].isdigit())


class CodeKeys:
    """Finds the known codes of a text and their glued key."""

    def __init__(self, keys: Mapping[str, str] | Iterable[str]) -> None:
        """Builds the dictionary of keys.

        :param keys: glued upper-case keys (``SF1027``), or a mapping from the glued spelling found in a text to the
            key written for it, so that every spelling of one document gives one key (``LDBIT243`` and ``IT243``
            both give ``LDBIT243``, ``Q0697MIFR`` gives ``Q0697MI``)
        """
        pairs = keys.items() if isinstance(keys, Mapping) else ((k, k) for k in keys)
        self.keys = {k.strip().upper(): v.strip().upper() for k, v in pairs if k and k.strip() and v and v.strip()}

    def find(self, text: str) -> list[CodeMatch]:
        """Known codes of the text, left to right, without overlap.

        A code starts at the start of a word and ends at the end of a word, or between a digit and a glued word of
        letters when the code holds a separator (``MI-13991`` in ``MI-13991Table``, never ``F7`` in ``F7XC5355``).
        Inside a longer chain of separated parts, only keys of 5 characters or more count (never ``A01`` in
        ``PTD-TEST-6-A-01``). A number never stops at a decimal point (never ``T31`` in ``T3 1,2``). A gap with a
        space joins only upper-case words of at most 4 letters or digits (``BAC 5578``; never ``of 10`` or
        ``STRUCTURE 1000``) and never ends on a word (never ``NAS1789 - NUT``). A code without digits has 3 letters
        or more, in upper case, joined by separators without spaces (``E.A.S.A``; never ``A/B`` or ``SAP`` alone).

        :param text: question or passage
        :return: the codes found, with their position in the text and the key written for them
        """
        text = text or ''
        pieces = [(m.start(), m.end()) for m in _PIECE.finditer(text)]
        found: list[CodeMatch] = []
        i = 0
        while i < len(pieces):
            start = pieces[i][0]
            if (start > 0 and text[start - 1].isalnum()) or _inside_number(text, start):
                i += 1
                continue
            chain_before = start > 1 and text[start - 1] in _CHAIN and text[start - 2].isalnum()
            best = None
            key = ''
            has_digit = has_separator = has_space = ends_on_spaced_word = False
            letters_upper = True
            for j in range(i, min(i + _MAX_PIECES, len(pieces))):
                piece = text[pieces[j][0]:pieces[j][1]]
                ends_on_spaced_word = False
                if j > i:
                    gap = text[pieces[j - 1][1]:pieces[j][0]]
                    if not _GAP.fullmatch(gap):
                        break
                    previous = text[pieces[j - 1][0]:pieces[j - 1][1]]
                    if _SPACE.search(gap):
                        if not (_strong(previous) and _strong(piece)):
                            break
                        has_space = True
                        ends_on_spaced_word = piece.isalpha() and len(piece) > 1
                    has_separator = has_separator or bool(gap.strip())
                has_digit = has_digit or piece.isdigit()
                letters_upper = letters_upper and (piece.isdigit() or piece.isupper())
                key += piece.upper()
                end = pieces[j][1]
                at_word_end = end == len(text) or not text[end].isalnum()
                digit_to_letter = (not at_word_end and piece.isdigit() and has_separator
                                   and text[end:_word_end(text, end)].isalpha())
                chain_after = end + 1 < len(text) and text[end] in _CHAIN and text[end + 1].isalnum()
                if has_digit:
                    shape_ok = not ends_on_spaced_word and not (piece.isdigit() and _inside_number(text, end + 1))
                else:
                    shape_ok = j > i and letters_upper and not has_space and len(key) >= _MIN_LETTERS_KEY
                if (chain_before or chain_after) and len(key) < _MIN_KEY_IN_CHAIN:
                    shape_ok = False
                if (at_word_end or digit_to_letter) and shape_ok and key in self.keys:
                    best = (j, end, self.keys[key])
            if best:
                j, end, k = best
                found.append(CodeMatch(start, end, k))
                i = j + 1
            else:
                i += 1
        return found

    def annotate(self, text: str) -> str:
        """The text with the key added after each code not already written as its key.

        ``SF-1027`` becomes ``SF-1027 [SF1027]``, ``MI-13991Table`` becomes ``MI-13991Table [MI13991]``, ``SF1027``
        stays as it is. Idempotent: a code already followed by its key gets no second one.

        :param text: question or passage
        :return: the annotated text
        """
        text = text or ''
        out, last = [], 0
        for m in self.find(text):
            written = text[m.start:m.end]
            end = _word_end(text, m.end)
            if written.upper() == m.key and end == m.end:
                continue
            tag = f' [{m.key}]'
            if text.startswith(tag, end) or end < last:
                continue
            out.append(text[last:end])
            out.append(tag)
            last = end
        out.append(text[last:])
        return ''.join(out)
# --- matcher: end ---

# COMMAND ----------

# MAGIC %md
# MAGIC ## Passages
# MAGIC Every DEV passage goes through the matcher. First table: passages changed, distinct match keys and tokens appended, per pattern. A passage counts once per pattern it holds.

# COMMAND ----------

dictionary = spark.table(DICTIONARY)
# Plain dict captured by the function below: shared clusters refuse spark.sparkContext (no broadcast).
KEYS = {r["match_key"]: r["canonical_token"] for r in dictionary.select("match_key", "canonical_token").collect()}
pattern_of = (dictionary.groupBy(F.col("canonical_token").alias("token"))
              .agg(F.min("pattern_id").alias("pattern_id")))
logger.info("dictionary: %d match keys", len(KEYS))

SCHEMA = ("id string, token string, written string, appended boolean, word string, cut boolean, before string, "
          "after string")


def annotated(batches: Iterable[pd.DataFrame]) -> Iterable[pd.DataFrame]:
    """Codes found in each text, with an extract before and after the token is appended.

    :param batches: pandas batches with the columns id and txt
    :return: pandas batches with the columns of SCHEMA, one row per code found; appended is False when the code is
        already written as its token
    """
    ck = CodeKeys(KEYS)
    for pdf in batches:
        rows = []
        for i, txt in zip(pdf["id"], pdf["txt"]):
            txt = txt or ""
            for m in ck.find(txt):
                end = _word_end(txt, m.end)
                written = txt[m.start:m.end]
                appended = not ((written.upper() == m.key and end == m.end) or txt.startswith(f" [{m.key}]", end))
                # The extract runs from word boundary to word boundary, so no code in it is cut.
                left = txt.rfind(" ", 0, max(0, m.start - 60)) + 1
                right = txt.find(" ", end + 40)
                before = txt[left:right if right >= 0 else len(txt)]
                after = before[:end - left] + f" [{m.key}]" + before[end - left:]
                w_left = max(txt.rfind(c, 0, m.start) for c in " \n\t") + 1
                w_right = min([k for k in (txt.find(c, end) for c in " \n\t") if k >= 0] or [len(txt)])
                word = txt[w_left:w_right]
                cut = any(c.isalnum() for c in txt[w_left:m.start] + txt[end:w_right])
                rows.append((i, m.key, written, appended, word, cut, before, after if appended else before))
        yield pd.DataFrame(rows, columns=["id", "token", "written", "appended", "word", "cut", "before", "after"])


passages = spark.table(PASSAGES).select(F.col("chunk_id").cast("string").alias("id"), F.col("chunk_text").alias("txt"))
found = passages.mapInPandas(annotated, SCHEMA).join(pattern_of, "token", "left").cache()
hits = found.where("appended")
total = passages.count()
changed = hits.select("id").distinct().count()
logger.info("DEV passages: %d, changed: %d (%.1f %%)", total, changed, 100 * changed / max(total, 1))
display(hits.groupBy("pattern_id")
        .agg(F.countDistinct("id").alias("passages"), F.countDistinct("token").alias("tokens"),
             F.count("*").alias("appended"))
        .orderBy(F.desc("passages")))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Before and after, per pattern
# MAGIC The most frequent tokens of each pattern, with one extract of a passage before and after.

# COMMAND ----------

per_token = hits.groupBy("pattern_id", "token").agg(F.count("*").alias("n"), F.first("written").alias("written"),
                                                     F.first("before").alias("before"),
                                                     F.first("after").alias("after"))
w = Window.partitionBy("pattern_id").orderBy(F.desc("n"))
display(per_token.withColumn("rank", F.row_number().over(w)).where(F.col("rank") <= EXAMPLES)
        .orderBy("pattern_id", "rank").select("pattern_id", "token", "written", "n", "before", "after"))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Codes cut inside a word
# MAGIC Tokens appended to a code that is only part of a longer word (between two spaces): `SF-1154 [SF1154]-1_Fr`, `AMC1 145 [AMC1145].A.30`. One row per token and word, most frequent first.

# COMMAND ----------

display(hits.where("cut").groupBy("pattern_id", "token", "word").agg(F.count("*").alias("n"))
        .orderBy(F.desc("n")).limit(500))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Codes without digits
# MAGIC Every token appended to a code written without digits (acronyms with dots or slashes), with its spellings.

# COMMAND ----------

display(hits.where(~F.col("written").rlike("[0-9]")).groupBy("pattern_id", "token")
        .agg(F.count("*").alias("n"), F.slice(F.array_sort(F.collect_set("written")), 1, 10).alias("spellings"))
        .orderBy(F.desc("n")))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Codes of the corpus not found in the passages
# MAGIC Codes classified `CANONICAL_TOKEN` and cited in DEV documents, whose token the matcher finds in no DEV passage, by number of documents. Expected: documents outside the chat scope (the inventory reads every parsed document, the passages only the documents in scope). Anything else is a code the matcher misses.

# COMMAND ----------

found_tokens = found.select("token").distinct()
display(spark.table(PATTERNS).where((F.col("env") == "dev") & (F.col("treatment") == "CANONICAL_TOKEN")
                                    & (F.col("document_count") > 0))
        .join(found_tokens, F.col("canonical_token") == F.col("token"), "left_anti")
        .select("pattern_id", "code_key", "canonical_token", F.slice("spellings", 1, 6).alias("spellings"),
                "document_count", F.slice("top_documents", 1, 3).alias("top_documents"))
        .orderBy(F.desc("document_count")).limit(500))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Codes left as they are
# MAGIC Codes cited in DEV documents that get no token (`KEEP_AS_IS`, `GLOSSARY_CANDIDATE`), plain numbers and glued codes aside, by number of documents. A code that should get a token in this list means an exclusion pattern is too wide.

# COMMAND ----------

display(spark.table(PATTERNS).where((F.col("env") == "dev") & (F.col("treatment") != "CANONICAL_TOKEN")
                                    & ~F.col("pattern_id").isin("NUM-01", "GLU-01") & (F.col("document_count") > 0))
        .select("pattern_id", "code_key", F.slice("spellings", 1, 6).alias("spellings"), "document_count")
        .orderBy(F.desc("document_count")).limit(500))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Chat questions
# MAGIC Every UAT chat question that gets a token, before and after. A question must carry the same token as the passages that cite its code.

# COMMAND ----------

questions = (spark.table(QUESTIONS).where(F.col("question").isNotNull())
             .select(F.col("id").cast("string").alias("id"),
                     F.regexp_replace("question", r"\[Division:[^\]]*\]", " ").alias("txt")))
q_hits = questions.mapInPandas(annotated, SCHEMA).join(pattern_of, "token", "left").where("appended")
display(q_hits.select("pattern_id", "token", "written", "after").orderBy("pattern_id", "token"))
