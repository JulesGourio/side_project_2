# Databricks notebook source
# MAGIC %md
# MAGIC # Canonical token preview
# MAGIC
# MAGIC Applies the dictionary of match keys to the DEV passages and to the UAT chat questions, and shows the text before and after. Nothing is written: the passages and the index stay as they are.
# MAGIC
# MAGIC | | |
# MAGIC | --- | --- |
# MAGIC | Reads | `dev_proj.qualibot.f3_cles_codes` (dictionary, written by the code patterns notebook), `dev_proj.qualibot.chunks`, `uat_proj.qualibot.chat_turns` |
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
# MAGIC | `MTC-03` | Start | at the start of a word | nothing in `XSF-1027` |
# MAGIC | `MTC-04` | End | at the end of a word, or between a digit and a glued letter | `MI-13991` in `MI-13991Table` |
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
PASSAGES = "dev_proj.qualibot.chunks"
QUESTIONS = "uat_proj.qualibot.chat_turns"
EXAMPLES = 5   # before / after examples per pattern

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
PP_VERSION = 1

# A piece is letters only or digits only (Q0375MI gives Q, 0375, MI).
_PIECE = re.compile(r'[A-Za-z]+|[0-9]+')
# Gap allowed between two pieces of one code: nothing, up to 3 spaces around one or two separators (hyphens,
# unicode dashes, _ / .), or 1 to 3 spaces alone (IAQ 04 04 05, MI - 1365, 19 --058).
_GAP = re.compile(r'(?:[  ]{0,3}[-‐‑−–—_/.]{1,2}[  ]{0,3}|[  ]{1,3})?')
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

        A code starts at the start of a word; it ends at the end of a word or between a digit and a glued letter
        (``MI-13991`` in ``MI-13991Table``).

        :param text: question or passage
        :return: the codes found, with their position in the text and the key written for them
        """
        text = text or ''
        pieces = [(m.start(), m.end()) for m in _PIECE.finditer(text)]
        found: list[CodeMatch] = []
        i = 0
        while i < len(pieces):
            start = pieces[i][0]
            if start > 0 and text[start - 1].isalnum():
                i += 1
                continue
            best = None
            key = ''
            for j in range(i, min(i + _MAX_PIECES, len(pieces))):
                if j > i and not _GAP.fullmatch(text, pieces[j - 1][1], pieces[j][0]):
                    break
                key += text[pieces[j][0]:pieces[j][1]].upper()
                end = pieces[j][1]
                at_word_end = end == len(text) or not text[end].isalnum()
                digit_to_letter = not at_word_end and text[end - 1].isdigit() and text[end].isalpha()
                if (at_word_end or digit_to_letter) and key in self.keys:
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
keys_b = spark.sparkContext.broadcast({r["match_key"]: r["canonical_token"]
                                       for r in dictionary.select("match_key", "canonical_token").collect()})
pattern_of = (dictionary.groupBy(F.col("canonical_token").alias("token"))
              .agg(F.min("pattern_id").alias("pattern_id")))
logger.info("dictionary: %d match keys", len(keys_b.value))

SCHEMA = "id string, token string, written string, before string, after string"


def annotated(batches: Iterable[pd.DataFrame]) -> Iterable[pd.DataFrame]:
    """Codes found in each text, with an extract before and after the token is appended.

    :param batches: pandas batches with the columns id and txt
    :return: pandas batches with the columns of SCHEMA, one row per code that gets a token
    """
    ck = CodeKeys(keys_b.value)
    for pdf in batches:
        rows = []
        for i, txt in zip(pdf["id"], pdf["txt"]):
            txt = txt or ""
            for m in ck.find(txt):
                before = txt[max(0, m.start - 60):m.end + 40]
                after = ck.annotate(before)
                if after != before:
                    rows.append((i, m.key, txt[m.start:m.end], before, after))
        yield pd.DataFrame(rows, columns=["id", "token", "written", "before", "after"])


passages = spark.table(PASSAGES).select(F.col("chunk_id").cast("string").alias("id"), F.col("chunk_text").alias("txt"))
hits = passages.mapInPandas(annotated, SCHEMA).join(pattern_of, "token", "left").cache()
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
# MAGIC ## Chat questions
# MAGIC Every UAT chat question that gets a token, before and after. A question must carry the same token as the passages that cite its code.

# COMMAND ----------

questions = (spark.table(QUESTIONS).where(F.col("question").isNotNull())
             .select(F.col("id").cast("string").alias("id"),
                     F.regexp_replace("question", r"\[Division:[^\]]*\]", " ").alias("txt")))
q_hits = questions.mapInPandas(annotated, SCHEMA).join(pattern_of, "token", "left")
display(q_hits.select("pattern_id", "token", "written", "after").orderBy("pattern_id", "token"))
