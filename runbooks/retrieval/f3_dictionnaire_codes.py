# Databricks notebook source
# MAGIC %md
# MAGIC # Canonical token preview
# MAGIC
# MAGIC Runs the code normalization of the parsing pipeline (`utils/parsing_pipeline/code_normalization.py`, the module that writes `chunk_preprocessed` and rewrites the chat queries) on the DEV passages and on the UAT chat questions, and shows the text before and after. Nothing is written: the passages and the index stay as they are.
# MAGIC
# MAGIC | | |
# MAGIC | --- | --- |
# MAGIC | Reads | the module, from the repository folder holding this notebook or from the DEV bundle; `dev_proj.qualibot.doc_catalog` and `uat_proj.qualibot.doc_catalog` (REFs, for the coverage), `dev_proj.qualibot.chunks`, `uat_proj.qualibot.chat_turns`, `dev_proj.qualibot.f2_familles_codes` (code patterns, for the comparison) |
# MAGIC | Writes | nothing |
# MAGIC | Rights | the user's own |
# MAGIC
# MAGIC ## How the module finds a code
# MAGIC
# MAGIC One table of named patterns (`CODE_PATTERNS`), each with the examples it must find and the texts it must leave alone; the first table below lists them. Of two overlapping codes the one starting first wins, then the longest, then the first pattern. The token is the code without separators, upper-cased, without diacritics and without its language or variant suffix (`SF-1027_BG` → `SF1027`, `QIS-46A-352 A` → `QIS46A352`). It is appended after the code, or after the word glued to it (`MI-13991Table [MI13991]`); nothing is added when the code is written as its token, and a second pass adds nothing. In a question, a REF in lower case is a code too (`sf 1027`), except the prefixes that are also words (`if 2`, `go 2024`, `le 23`).

# COMMAND ----------

# Parameters

import importlib.util
import logging
import os
import sys
import types
from typing import Iterable

import pandas as pd
from pyspark.sql import Window
from pyspark.sql import functions as F

MODULE = "utils/parsing_pipeline/code_normalization.py"
# Folders that may hold the repository: the one of this notebook (runbooks/retrieval/ two levels down), then the DEV
# bundle.
REPOSITORY_ROOTS = [os.path.dirname(os.path.dirname(os.getcwd())), "/Workspace/.bundle/qualibot/dev/files"]
CATALOGS = ["dev_proj.qualibot.doc_catalog", "uat_proj.qualibot.doc_catalog"]
PASSAGES = "dev_proj.qualibot.chunks"
QUESTIONS = "uat_proj.qualibot.chat_turns"
PATTERNS = "dev_proj.qualibot.f2_familles_codes"
EXAMPLES = 15   # before / after examples per pattern

logger = logging.getLogger("canonical_token_preview")
if not logger.handlers:
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(_handler)
logger.setLevel(logging.INFO)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Module
# MAGIC The first repository folder that holds the module. Its source is also sent to the executors, which load it themselves: a shared cluster refuses `addPyFile`.

# COMMAND ----------

module_path = next((os.path.join(root, MODULE) for root in REPOSITORY_ROOTS
                    if os.path.isfile(os.path.join(root, MODULE))), None)
if module_path is None:
    raise FileNotFoundError(f"{MODULE} found in none of {REPOSITORY_ROOTS}: import the repository first")
with open(module_path, encoding="utf-8") as fh:
    MODULE_SOURCE = fh.read()
_spec = importlib.util.spec_from_file_location("code_normalization", module_path)
cn = importlib.util.module_from_spec(_spec)
sys.modules["code_normalization"] = cn
_spec.loader.exec_module(cn)
logger.info("module %s, NORMALIZATION_VERSION %s, %d patterns, %d families",
            module_path, cn.NORMALIZATION_VERSION, len(cn.CODE_PATTERNS), len(cn.CODE_FAMILIES))
display(spark.createDataFrame(
    [(p.pattern_id, p.name, ", ".join(t for t, _ in p.examples), ", ".join(p.counter_examples)) for p in cn.CODE_PATTERNS],
    "pattern_id string, name string, examples string, counter_examples string"))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Coverage of the catalog
# MAGIC The REFs of the DEV and UAT document catalogs that no pattern reads from their first character, by shape (`A` letter, `9` digit). An empty table means every REF is normalised. A REF here is a new format (a pattern to add) or a new prefix (`CODE_FAMILIES` to regenerate).

# COMMAND ----------

refs_df = None
for table in CATALOGS:
    part = spark.table(table).select(F.trim("ref").alias("ref")).where("ref IS NOT NULL AND ref <> ''")
    refs_df = part if refs_df is None else refs_df.unionByName(part)
REFS = sorted(r["ref"] for r in refs_df.distinct().collect())
unread = cn.unmatched_refs(REFS)
logger.info("catalog: %d REFs, %d read by no pattern", len(REFS), len(unread))
display(spark.createDataFrame([(r,) for r in unread] or [("",)], "ref string").where("ref <> ''")
        .withColumn("shape", F.regexp_replace(F.regexp_replace(F.upper("ref"), "[A-Z]", "A"), "[0-9]", "9"))
        .groupBy("shape").agg(F.count("*").alias("refs"), F.slice(F.array_sort(F.collect_list("ref")), 1, 5)
                              .alias("examples"))
        .orderBy(F.desc("refs")))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Passages
# MAGIC Every DEV passage goes through the module. First table: passages changed, distinct tokens and tokens appended, per pattern. A passage counts once per pattern it holds.

# COMMAND ----------

SCHEMA = ("id string, token string, pattern_id string, written string, appended boolean, word string, cut boolean, "
          "before string, after string")
COLUMNS = ["id", "token", "pattern_id", "written", "appended", "word", "cut", "before", "after"]


def annotated(batches: Iterable[pd.DataFrame], query: bool) -> Iterable[pd.DataFrame]:
    """Codes found in each text by the module, with an extract before and after the token is appended.

    :param batches: pandas batches with the columns id and txt
    :param query: the texts are questions (``normalize_query``), else passages (``normalize_codes``)
    :return: pandas batches with the columns of SCHEMA, one row per code found; appended is False when the module
        adds nothing for that code
    """
    module = sys.modules.get("code_normalization")
    if module is None:
        module = types.ModuleType("code_normalization")
        sys.modules["code_normalization"] = module
        exec(compile(MODULE_SOURCE, MODULE, "exec"), module.__dict__)
    for pdf in batches:
        rows = []
        for i, txt in zip(pdf["id"], pdf["txt"]):
            txt = txt or ""
            for start, end, token, pattern_id in module.match_codes(txt, accept_lowercase=query):
                stop = module._word_end(txt, end)
                written = txt[start:end]
                appended = not (txt[start:stop] == token or token in module._brackets_after(txt, stop))
                # The extract runs from word boundary to word boundary, so no code in it is cut.
                left = txt.rfind(" ", 0, max(0, start - 60)) + 1
                right = txt.find(" ", stop + 40)
                before = txt[left:right if right >= 0 else len(txt)]
                after = module.normalize_codes(before, accept_lowercase=query)
                w_left = max(txt.rfind(c, 0, start) for c in " \n\t") + 1
                w_right = min([k for k in (txt.find(c, stop) for c in " \n\t") if k >= 0] or [len(txt)])
                cut = any(c.isalnum() for c in txt[w_left:start] + txt[stop:w_right])
                rows.append((i, token, pattern_id, written, appended, txt[w_left:w_right], cut, before, after))
        yield pd.DataFrame(rows, columns=COLUMNS)


passages = spark.table(PASSAGES).select(F.col("chunk_id").cast("string").alias("id"), F.col("chunk_text").alias("txt"))
found = passages.mapInPandas(lambda b: annotated(b, query=False), SCHEMA).cache()
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
                                                 F.first("before").alias("before"), F.first("after").alias("after"))
w = Window.partitionBy("pattern_id").orderBy(F.desc("n"))
display(per_token.withColumn("rank", F.row_number().over(w)).where(F.col("rank") <= EXAMPLES)
        .orderBy("pattern_id", "rank").select("pattern_id", "token", "written", "n", "before", "after"))

# COMMAND ----------

# MAGIC %md
# MAGIC ## REF-shaped codes absent from the catalog
# MAGIC Tokens of the REF patterns that are the token of no catalog REF: obsolete REFs, other sites, typing errors, or text that only looks like a REF. One row per token, most frequent first, with its spellings: a token here that is not a document code means a pattern is too wide.

# COMMAND ----------

catalog_tokens = sorted({code for ref in REFS for code in cn.canonical_codes(ref)})
display(hits.where(F.col("pattern_id").startswith("REF-"))
        .join(spark.createDataFrame([(t,) for t in catalog_tokens], "token string"), "token", "left_anti")
        .groupBy("pattern_id", "token")
        .agg(F.count("*").alias("n"), F.countDistinct("id").alias("passages"),
             F.slice(F.array_sort(F.collect_set("written")), 1, 8).alias("spellings"))
        .orderBy(F.desc("n")).limit(500))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Codes inside a longer identifier
# MAGIC Tokens appended to a code that is only part of a longer identifier (between two spaces): `SF-1154-1_Fr [SF1154]`, `AMC1 145.A.30 [AMC1145]`. One row per token and identifier, most frequent first.

# COMMAND ----------

display(hits.where("cut").groupBy("pattern_id", "token", "word").agg(F.count("*").alias("n"))
        .orderBy(F.desc("n")).limit(500))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Codes without digits
# MAGIC Every token appended to a code written without digits (catalog codes such as `M.O.P`, acronyms with dots), with its spellings.

# COMMAND ----------

display(hits.where(~F.col("written").rlike("[0-9]")).groupBy("pattern_id", "token")
        .agg(F.count("*").alias("n"), F.slice(F.array_sort(F.collect_set("written")), 1, 10).alias("spellings"))
        .orderBy(F.desc("n")))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Codes of the code patterns that the module misses
# MAGIC Codes that the code patterns notebook classified `CANONICAL_TOKEN` and that DEV documents cite, whose token the module finds in no DEV passage, by number of documents. Separated acronyms are left out: written plainly (`SAP`) they are already their token. Expected: documents outside the chat scope (the inventory reads every parsed document, the passages only the documents in scope), and codes written only as their token. Anything else is a code the module misses.

# COMMAND ----------

found_tokens = found.select("token").distinct()
display(spark.table(PATTERNS).where((F.col("env") == "dev") & (F.col("treatment") == "CANONICAL_TOKEN")
                                    & (F.col("pattern_id") != "ACR-02") & (F.col("document_count") > 0))
        .join(found_tokens, F.col("canonical_token") == F.col("token"), "left_anti")
        .select("pattern_id", "code_key", "canonical_token", F.slice("spellings", 1, 6).alias("spellings"),
                "document_count", F.slice("top_documents", 1, 3).alias("top_documents"))
        .orderBy(F.desc("document_count")).limit(500))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Tokens the code patterns did not ask for
# MAGIC Tokens the module appends whose code the code patterns notebook classified otherwise (`KEEP_AS_IS`, `GLOSSARY_CANDIDATE`) or did not see, most frequent first. A token here is a code the module annotates although the analysis left it alone: either the analysis or the module is wrong.

# COMMAND ----------

wanted = (spark.table(PATTERNS).where((F.col("env") == "dev") & (F.col("treatment") == "CANONICAL_TOKEN"))
          .select(F.col("canonical_token").alias("token")).distinct())
classified = (spark.table(PATTERNS).where(F.col("env") == "dev")
              .groupBy(F.col("code_key").alias("token"))
              .agg(F.first("pattern_id").alias("pattern_id"), F.first("treatment").alias("treatment")))
display(hits.join(wanted, "token", "left_anti").groupBy("pattern_id", "token")
        .agg(F.count("*").alias("n"), F.slice(F.array_sort(F.collect_set("written")), 1, 6).alias("spellings"),
             F.first("before").alias("before"))
        .join(classified, "token", "left").orderBy(F.desc("n")).limit(500))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Chat questions
# MAGIC Every UAT chat question that gets a token, before and after (`normalize_query`). A question must carry the same token as the passages that cite its code.

# COMMAND ----------

questions = (spark.table(QUESTIONS).where(F.col("question").isNotNull())
             .select(F.col("id").cast("string").alias("id"),
                     F.regexp_replace("question", r"\[Division:[^\]]*\]", " ").alias("txt")))
q_hits = questions.mapInPandas(lambda b: annotated(b, query=True), SCHEMA).where("appended")
logger.info("UAT questions with a token: %d", q_hits.select("id").distinct().count())
display(q_hits.select("pattern_id", "token", "written", "after").orderBy("pattern_id", "token"))
