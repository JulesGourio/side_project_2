# Databricks notebook source
# MAGIC %md
# MAGIC # Canonical token preview
# MAGIC
# MAGIC Runs the code normalization of the parsing pipeline (`utils/parsing_pipeline/code_normalization.py`, the module that writes `chunk_preprocessed` and rewrites the chat queries) on the DEV passages and on the UAT chat questions, with the catalog of today, and shows the text before and after. Nothing is written: the passages and the index stay as they are.
# MAGIC
# MAGIC | | |
# MAGIC | --- | --- |
# MAGIC | Reads | the module, from the repository folder holding this notebook or from the DEV bundle; `dev_proj.qualibot.doc_catalog` and `uat_proj.qualibot.doc_catalog` (REFs), `dev_proj.qualibot.chunks`, `uat_proj.qualibot.chat_turns`, `dev_proj.qualibot.f2_familles_codes` (code patterns, for the comparison) |
# MAGIC | Writes | nothing |
# MAGIC | Rights | the user's own |
# MAGIC
# MAGIC ## How the module finds a code
# MAGIC
# MAGIC Three matchers; of two overlapping codes the one starting first, then the longest, then the catalog one wins.
# MAGIC
# MAGIC | Matcher | Finds | Example |
# MAGIC | --- | --- | --- |
# MAGIC | `CATALOG` | a catalog REF in any spelling: pieces of letters or digits glued across separators and spaces, the longest run that is a catalog key | `MI - 1365`, `Q0697MI_FR`, `LdB IT-243`, `19 --058`, `ŘLCZ-07/18` |
# MAGIC | `REF_SHAPE` | a REF absent from the catalog: the shape of a REF family (prefix and number groups, site codes, lettres de branchement, programme phases, IF), kept when it has the signature of a catalog REF | `SF-1027` (shape of `SF-1029`), `Q0256MI` |
# MAGIC | `STD-01`, `ATA-01`, `ACF-01`, `REG-01`, `MAT-01`, `ACR-02`, `SAP-01`, `PN-01`, `TRM-01` | codes that are not REFs: standards, ATA chapters, aircraft, regulations, alloys, acronyms written with dots, SAP transactions, part numbers, quality methods | `EN 9100`, `ATA 52`, `A350-900`, `Part-145`, `2024-T3`, `E.A.S.A`, `MM-02`, `L523 77000`, `8-D` |
# MAGIC
# MAGIC The token is the REF without separators and without language suffix (`SF-1027_BG` → `SF1027`); a key that several REFs share is its own token (`777F`); a key that one REF completes takes that REF (`QIS-46A-352` → `QIS46A352A`). It is appended after the identifier holding the code (`MI-13991Table [MI13991]`, `IP-029-005 [IP029]`); nothing is added when the code is written as its token, and a second pass adds nothing.
# MAGIC
# MAGIC In a question, a prefix in lower case followed by a space is also a code (`sf 1027`), except the prefixes that are also words (`if 2`, `go 2024`, `le 23`).

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
EXAMPLES = 15   # before / after examples per matcher

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
_spec.loader.exec_module(cn)
logger.info("module %s, NORMALIZATION_VERSION %s, generated catalog: %d keys, %d families",
            module_path, cn.NORMALIZATION_VERSION, len(cn.CATALOG_CANONICAL), len(cn.CODE_FAMILIES))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Catalog of today
# MAGIC The REFs of the DEV and UAT document catalogs. First table: the REFs that the catalog generated into the module does not know (an empty table means the module is up to date). The preview below runs on the catalog of today either way, so it shows what the passages get once the module is regenerated. Second table: every REF, one per line, to download (CSV) and send back to regenerate the module.

# COMMAND ----------

refs_df = None
for table in CATALOGS:
    part = spark.table(table).select(F.trim("ref").alias("ref")).where("ref IS NOT NULL AND ref <> ''")
    refs_df = part if refs_df is None else refs_df.unionByName(part)
REFS = sorted(r["ref"] for r in refs_df.distinct().collect())
drift = cn.catalog_drift(REFS)
logger.info("catalog of today: %d REFs, %d unknown to the module", len(REFS), len(drift))
display(spark.createDataFrame([(r,) for r in drift] or [("",)], "ref string").where("ref <> ''"))

# COMMAND ----------

display(spark.createDataFrame([(r,) for r in REFS], "ref string"))

# COMMAND ----------

CATALOG = cn.catalog_from_refs(REFS)
cn.use_catalog(CATALOG)
# Plain types only for the executors: the module's own classes do not exist there before they load it.
CATALOG_PLAIN = {**CATALOG, "families": {p: tuple(f) for p, f in CATALOG["families"].items()}}
logger.info("preview catalog: %d keys, %d families, %d signatures, site letters %s, LdB types %s, phases %s",
            len(CATALOG["canonical"]), len(CATALOG["families"]), len(CATALOG["signatures"]),
            CATALOG["site_letters"], ",".join(CATALOG["ldb_types"]), ",".join(CATALOG["phase_prefixes"]))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Passages
# MAGIC Every DEV passage goes through the module. First table: passages changed, distinct tokens and tokens appended, per matcher. A passage counts once per matcher it holds.

# COMMAND ----------

SCHEMA = ("id string, token string, matcher string, written string, appended boolean, word string, cut boolean, "
          "before string, after string")
COLUMNS = ["id", "token", "matcher", "written", "appended", "word", "cut", "before", "after"]


def annotated(batches: Iterable[pd.DataFrame], query: bool) -> Iterable[pd.DataFrame]:
    """Codes found in each text by the module, with an extract before and after the token is appended.

    :param batches: pandas batches with the columns id and txt
    :param query: the texts are questions (``normalize_query``), else passages (``normalize_codes``)
    :return: pandas batches with the columns of SCHEMA, one row per code found; appended is False when the module
        adds nothing for that code
    """
    module = types.ModuleType("code_normalization")
    exec(compile(MODULE_SOURCE, MODULE, "exec"), module.__dict__)
    module.use_catalog({**CATALOG_PLAIN, "families": {p: module.CodeFamily(*f)
                                                      for p, f in CATALOG_PLAIN["families"].items()}})
    for pdf in batches:
        rows = []
        for i, txt in zip(pdf["id"], pdf["txt"]):
            txt = txt or ""
            normalized = module.normalize_codes(txt, accept_lowercase=query)
            for start, end, token, matcher in module.explain_codes(txt, accept_lowercase=query):
                stop = module._token_end(txt, end)
                written = txt[start:end]
                appended = not ((written == token and stop == end) or token in module._brackets_after(txt, stop))
                # The extract runs from word boundary to word boundary, so no code in it is cut.
                left = txt.rfind(" ", 0, max(0, start - 60)) + 1
                right = txt.find(" ", stop + 40)
                before = txt[left:right if right >= 0 else len(txt)]
                after = module.normalize_codes(before, accept_lowercase=query)
                w_left = max(txt.rfind(c, 0, start) for c in " \n\t") + 1
                w_right = min([k for k in (txt.find(c, stop) for c in " \n\t") if k >= 0] or [len(txt)])
                cut = any(c.isalnum() for c in txt[w_left:start] + txt[stop:w_right])
                rows.append((i, token, matcher, written, appended and normalized != txt, txt[w_left:w_right], cut,
                             before, after))
        yield pd.DataFrame(rows, columns=COLUMNS)


passages = spark.table(PASSAGES).select(F.col("chunk_id").cast("string").alias("id"), F.col("chunk_text").alias("txt"))
found = passages.mapInPandas(lambda b: annotated(b, query=False), SCHEMA).cache()
hits = found.where("appended")
total = passages.count()
changed = hits.select("id").distinct().count()
logger.info("DEV passages: %d, changed: %d (%.1f %%)", total, changed, 100 * changed / max(total, 1))
display(hits.groupBy("matcher")
        .agg(F.countDistinct("id").alias("passages"), F.countDistinct("token").alias("tokens"),
             F.count("*").alias("appended"))
        .orderBy(F.desc("passages")))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Before and after, per matcher
# MAGIC The most frequent tokens of each matcher, with one extract of a passage before and after.

# COMMAND ----------

per_token = hits.groupBy("matcher", "token").agg(F.count("*").alias("n"), F.first("written").alias("written"),
                                                 F.first("before").alias("before"), F.first("after").alias("after"))
w = Window.partitionBy("matcher").orderBy(F.desc("n"))
display(per_token.withColumn("rank", F.row_number().over(w)).where(F.col("rank") <= EXAMPLES)
        .orderBy("matcher", "rank").select("matcher", "token", "written", "n", "before", "after"))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Codes found by their shape only
# MAGIC Every token of `REF_SHAPE`: codes that look like a catalog REF without being one (obsolete REF, other site, typing error). One row per token, most frequent first, with its spellings: a token here that is not a document code means a family is too wide.

# COMMAND ----------

display(hits.where(F.col("matcher") == "REF_SHAPE").groupBy("token")
        .agg(F.count("*").alias("n"), F.countDistinct("id").alias("passages"),
             F.slice(F.array_sort(F.collect_set("written")), 1, 8).alias("spellings"))
        .orderBy(F.desc("n")).limit(500))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Codes inside a longer identifier
# MAGIC Tokens appended to a code that is only part of a longer identifier (between two spaces): `SF-1154-1_Fr [SF1154]`, `AMC1 145.A.30 [AMC1145]`. One row per token and identifier, most frequent first.

# COMMAND ----------

display(hits.where("cut").groupBy("matcher", "token", "word").agg(F.count("*").alias("n"))
        .orderBy(F.desc("n")).limit(500))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Codes without digits
# MAGIC Every token appended to a code written without digits (catalog codes such as `M.O.P`, acronyms with dots), with its spellings.

# COMMAND ----------

display(hits.where(~F.col("written").rlike("[0-9]")).groupBy("matcher", "token")
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
display(hits.join(wanted, "token", "left_anti").groupBy("matcher", "token")
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
display(q_hits.select("matcher", "token", "written", "after").orderBy("matcher", "token"))
