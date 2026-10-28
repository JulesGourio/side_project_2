# Databricks notebook source
# MAGIC %md
# MAGIC # Code inventory
# MAGIC
# MAGIC Lists every code-like token of the parsed documents, of the document catalogue and of the chat questions, with how often and how it is spelled. The output feeds the code pattern analysis; nothing is filtered here except plain numbers and ordinary words.
# MAGIC
# MAGIC | | |
# MAGIC | --- | --- |
# MAGIC | Reads | `<env>_proj.qualibot.parsing_checkpoint` (one text per document), `<env>_proj.qualibot.doc_catalog`, `<env>_proj.qualibot.chat_turns`, for each environment of `ENVS` |
# MAGIC | Writes | `dev_proj.qualibot.f1_inventaire_codes`, rows of the environments of `ENVS` replaced |
# MAGIC | Rights | the user's own (read on DEV and UAT, write on the DEV analysis table) |

# COMMAND ----------

# MAGIC %md
# MAGIC ## Extraction regexes
# MAGIC
# MAGIC | ID | Regex | Captures | Examples | Not captured |
# MAGIC | --- | --- | --- | --- | --- |
# MAGIC | `TOKEN` | `(?<![A-Za-z0-9])[A-Za-z0-9]+(?:[-_/.][A-Za-z0-9]+)*(?![A-Za-z0-9])` | A run of letters and digits, joined to the next run by one `-`, `_`, `/` or `.`; no length bound | `ST-1027`, `MI-1413_FR`, `INAQ/560`, `4.2.1`, `Q0375MI` | the final dot of a sentence |
# MAGIC | `SEQUENCE` | `(?<![A-Za-z0-9])[A-Za-z][A-Za-z0-9]*(?:[-_/.][A-Za-z0-9]+)*(?: [0-9]+(?:[-_/.][0-9]+)*)+(?![A-Za-z0-9])` | A token starting with a letter, followed by one or more numbers separated by single spaces | `IAQ 20 00 04`, `BAC 5423`, `ISO 9001`, `QTP 01-174`, `mars 2026` | `MI - 1365` (spaces around the dash) |
# MAGIC
# MAGIC ## Token categories
# MAGIC
# MAGIC | Category | Rule | Examples |
# MAGIC | --- | --- | --- |
# MAGIC | `code_lettres_chiffres` | letters and digits | `ST-1027`, `Q0375MI`, `A320` |
# MAGIC | `nombre_compose` | digits and separators only | `4.2.1`, `05-02-011` |
# MAGIC | `sigle` | at least 2 letters, all upper case, no digit | `DOA`, `E.A.S.A` |
# MAGIC | `casse_mixte` | at least one lower-case and two upper-case letters, no digit | `eFNC`, `PIRs` |
# MAGIC | `sequence_espace` | matched by `SEQUENCE` | `IAQ 04 04 05` |
# MAGIC | discarded | plain numbers, lower-case words, capitalised words, single capitals | `1027`, `rapport`, `Annexe`, `A` |
# MAGIC
# MAGIC ## Output columns
# MAGIC
# MAGIC | Column | Content |
# MAGIC | --- | --- |
# MAGIC | `env`, `section` | environment; `A_reference` (one row per catalogue REF), `B_corpus` (one row per key of the documents), `C_questions` (one row per key of the questions) |
# MAGIC | `categorie`, `cle` | token category; key = upper case, separators removed (`ST-1027`, `st1027` and `ST 1027` give `ST1027`) |
# MAGIC | `occ`, `docs` | occurrences; documents (`B`) or questions (`C`); for `A`, exact citations of the REF |
# MAGIC | `formes`, `structures` | spellings seen; their shape, `A` upper case, `a` lower case, `9` digits (`ST-1027` gives `A-9`) |
# MAGIC | `occ_autre_graphie`, `formes_autre_graphie` | `A` only: citations of the REF spelled differently (space, dash, case) |
# MAGIC | `occ_prefixe`, `formes_prefixe` | `A` only: citations of the REF followed by a glued suffix (`MI-1413_FR`) |
# MAGIC | `occ_corpus`, `formes_corpus` | `C` only: occurrences and spellings of the same key in the documents, null when absent |
# MAGIC | `nb_refs`, `refs_source`, `urls_source` | number of document REFs containing the key; the 10 main ones as `REF (n)`; their Intraqual links |

# COMMAND ----------

# Parameters

import logging
import sys
import time
from functools import reduce

from pyspark.sql import DataFrame
from pyspark.sql import Window
from pyspark.sql import functions as F

ENVS = ["dev", "uat"]   # ["dev", "uat"], ["dev"] or ["uat"]
TARGET = "dev_proj.qualibot.f1_inventaire_codes"
MIN_OCC = 1   # minimum occurrences in the documents for a key of section B
MAX_SPACE_REFS = 2000   # above this number, catalogue REFs containing a space are not counted
INTRAQUAL_BASE = "https://intraqual.lat.corp/intraqual_prod/identification.aspx?ref="

TOKEN = r"(?<![A-Za-z0-9])[A-Za-z0-9]+(?:[-_/.][A-Za-z0-9]+)*(?![A-Za-z0-9])"
SEQUENCE = (r"(?<![A-Za-z0-9])[A-Za-z][A-Za-z0-9]*(?:[-_/.][A-Za-z0-9]+)*"
            r"(?: [0-9]+(?:[-_/.][0-9]+)*)+(?![A-Za-z0-9])")

TYPES = {
    "cle": "string", "ref_catalogue": "string", "occ": "long", "docs": "long",
    "formes": "array<string>", "structures": "array<string>",
    "occ_corpus": "long", "formes_corpus": "array<string>",
    "occ_autre_graphie": "long", "formes_autre_graphie": "array<string>",
    "occ_prefixe": "long", "formes_prefixe": "array<string>",
    "nb_refs": "long", "refs_source": "array<string>", "urls_source": "array<string>",
}

logger = logging.getLogger("code_inventory")
if not logger.handlers:
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(_handler)
logger.setLevel(logging.INFO)

# COMMAND ----------

# Functions (no write)


def with_key(tok_df: DataFrame) -> DataFrame:
    """Adds the key of each token: upper case, without any character other than letters and digits.

    :param tok_df: DataFrame with a column tok
    :return: the same DataFrame with a column cle
    """
    return tok_df.withColumn("cle", F.upper(F.regexp_replace("tok", "[^A-Za-z0-9]", "")))


def shape(col):
    """Shape of a token: runs of upper-case letters give A, lower-case letters a, digits 9; separators kept.

    :param col: column holding the token
    :return: column of the shape
    """
    return F.regexp_replace(F.regexp_replace(F.regexp_replace(col, "[A-Z]+", "A"), "[a-z]+", "a"), "[0-9]+", "9")


def tokens(src: DataFrame, pattern: str) -> DataFrame:
    """One row per match of the pattern in the column txt.

    :param src: DataFrame with the columns doc, doc_ref and txt
    :param pattern: regular expression, Java syntax
    :return: DataFrame with the columns doc, doc_ref and tok
    """
    pat = pattern.replace("\\", "\\\\")
    return src.select("doc", "doc_ref", F.explode(F.expr(f"regexp_extract_all(txt, '{pat}', 0)")).alias("tok"))


def classified(src: DataFrame) -> DataFrame:
    """Tokens and sequences of a source with their category and shape; uncategorised tokens are dropped.

    :param src: DataFrame with the columns doc, doc_ref and txt
    :return: DataFrame with the columns doc, doc_ref, tok, categorie and forme
    """
    t = F.col("tok")
    letters = F.length(F.regexp_replace(t, "[^A-Za-z]", ""))
    has_digit = t.rlike("[0-9]")
    has_lower = t.rlike("[a-z]")
    n_upper = F.length(F.regexp_replace(t, "[^A-Z]", ""))
    simple = (tokens(src, TOKEN)
              .withColumn("categorie",
                          F.when((letters > 0) & has_digit, "code_lettres_chiffres")
                          .when((letters == 0) & t.rlike("[-_/.]"), "nombre_compose")
                          .when((letters >= 2) & ~has_digit & ~has_lower, "sigle")
                          .when((letters > 0) & ~has_digit & has_lower & (n_upper >= 2), "casse_mixte"))
              .where(F.col("categorie").isNotNull()))
    seq = tokens(src, SEQUENCE).withColumn("categorie", F.lit("sequence_espace"))
    return simple.unionByName(seq).withColumn("forme", shape(F.col("tok")))


def keyed(tok_df: DataFrame) -> DataFrame:
    """Statistics per category and key, with the document REFs containing the key and their Intraqual links.

    :param tok_df: DataFrame with the columns doc, doc_ref, tok, categorie and forme
    :return: DataFrame with the columns categorie, cle, occ, docs, formes, structures, nb_refs, refs_source, urls_source
    """
    t = with_key(tok_df)
    stats = (t.groupBy("categorie", "cle")
             .agg(F.count("*").alias("occ"), F.countDistinct("doc").alias("docs"),
                  F.array_sort(F.collect_set("tok")).alias("formes"),
                  F.array_sort(F.collect_set("forme")).alias("structures")))
    per_ref = (t.where(F.col("doc_ref").isNotNull())
               .groupBy("categorie", "cle", "doc_ref").agg(F.countDistinct("doc").alias("n"))
               .groupBy("categorie", "cle")
               .agg(F.sort_array(F.collect_list(F.struct("n", "doc_ref")), asc=False).alias("s"),
                    F.count("doc_ref").alias("nb_refs")))
    refs = per_ref.select(
        "categorie", "cle", "nb_refs",
        F.transform(F.slice("s", 1, 10),
                    lambda x: F.concat(x["doc_ref"], F.lit(" ("), x["n"].cast("string"), F.lit(")")))
        .alias("refs_source"),
        F.transform(F.slice("s", 1, 10),
                    lambda x: F.concat(F.lit(INTRAQUAL_BASE), F.regexp_replace(x["doc_ref"], " ", "%20")))
        .alias("urls_source"))
    return stats.join(refs, ["categorie", "cle"], "left")


def conform(df: DataFrame, env: str, section: str, categorie: str | None = None) -> DataFrame:
    """Brings a DataFrame to the columns of the table; a column the section does not have is null.

    :param df: DataFrame of the section
    :param env: source environment (dev or uat)
    :param section: section name (A_reference, B_corpus, C_questions)
    :param categorie: fixed category of the section, or None to keep the column categorie of df
    :return: DataFrame with the columns of the table
    """
    cols = [F.col(c).cast(t).alias(c) if c in df.columns else F.lit(None).cast(t).alias(c)
            for c, t in TYPES.items()]
    cat_col = F.lit(categorie) if categorie is not None else F.col("categorie")
    return df.select(F.lit(env).alias("env"), F.lit(section).alias("section"), cat_col.alias("categorie"),
                     *cols, F.current_timestamp().alias("date_run"))


def corpus_source(env: str) -> DataFrame:
    """One text per document of parsing_checkpoint: the latest SUCCESS row, else the latest row.

    :param env: environment (dev or uat)
    :return: DataFrame with the columns doc, doc_ref and txt
    """
    df = spark.table(f"{env}_proj.qualibot.parsing_checkpoint").where(F.col("document_text").isNotNull())
    rank = F.when(F.col("parse_status") == "SUCCESS", 0).otherwise(1)
    w = Window.partitionBy("IDDOC").orderBy(rank, F.desc("ingestion_timestamp"))
    return (df.withColumn("_rn", F.row_number().over(w)).where("_rn = 1")
            .select(F.col("IDDOC").cast("string").alias("doc"),
                    F.col("ref").cast("string").alias("doc_ref"),
                    F.col("document_text").alias("txt")))


def question_source(env: str) -> DataFrame:
    """Questions typed in the chat of the environment, without the division prefix.

    :param env: environment (dev or uat)
    :return: DataFrame with the columns doc, doc_ref (null) and txt; empty when the table cannot be read
    """
    try:
        df = spark.table(f"{env}_proj.qualibot.chat_turns").where(F.col("question").isNotNull())
    except Exception as e:
        logger.warning("[%s] chat_turns not readable: %s", env, str(e)[:160])
        return spark.createDataFrame([], "doc string, doc_ref string, txt string")
    return df.select(
        F.concat(F.lit("turn#"), F.col("id").cast("string")).alias("doc"),
        F.lit(None).cast("string").alias("doc_ref"),
        F.regexp_replace(F.col("question"), r"\[Division:[^\]]*\]", " ").alias("txt"))


def space_ref_counts(corpus: DataFrame, cat: DataFrame) -> DataFrame:
    """Documents containing the exact string of each catalogue REF that contains a space.

    :param corpus: DataFrame with the columns doc, doc_ref and txt
    :param cat: DataFrame of the catalogue column ref
    :return: DataFrame with the columns ref and occ_espace
    """
    refs = [r["ref"] for r in cat.where(F.col("ref").contains(" ")).collect()]
    if len(refs) > MAX_SPACE_REFS:
        logger.warning("%d REFs with a space: not counted (MAX_SPACE_REFS = %d)", len(refs), MAX_SPACE_REFS)
        refs = []
    counts = {}
    logger.info("REFs with a space: %d", len(refs))
    for k in range(0, len(refs), 50):
        t0 = time.time()
        batch = refs[k:k + 50]
        row = corpus.agg(*[F.sum(F.when(F.instr(F.col("txt"), r) > 0, 1).otherwise(0)).alias(f"c{j}")
                           for j, r in enumerate(batch)]).collect()[0]
        counts.update({r: int(row[f"c{j}"] or 0) for j, r in enumerate(batch)})
        logger.info("  REFs with a space %d/%d (%.0f s)", k + len(batch), len(refs), time.time() - t0)
    return spark.createDataFrame(list(counts.items()), "ref string, occ_espace long")


def section_a(env: str, corpus: DataFrame) -> DataFrame:
    """One row per catalogue REF: exact citations, other spellings, glued suffix.

    :param env: environment (dev or uat)
    :param corpus: DataFrame with the columns doc, doc_ref and txt
    :return: DataFrame of section A with the columns of the table
    """
    cat = spark.table(f"{env}_proj.qualibot.doc_catalog").select("ref").where("ref IS NOT NULL")
    refs_k = cat.withColumn("cle", F.upper(F.regexp_replace("ref", "[^A-Za-z0-9]", "")))
    # A token with a digit is compared with the REFs whole and cut before each separator, to see glued suffixes.
    parts = F.split("tok", "(?=[-_/.])")
    exp = (tokens(corpus, TOKEN).where(F.col("tok").rlike("[0-9]"))
           .withColumn("parts", parts)
           .select("tok", F.size("parts").alias("n"),
                   F.posexplode(F.transform(F.sequence(F.lit(1), F.size("parts")),
                                            lambda i: F.array_join(F.slice("parts", 1, i), ""))).alias("i", "debut")))
    whole = (F.col("i") + 1) == F.col("n")
    same = F.col("tok") == F.col("ref")
    hits = (exp.withColumn("cle", F.upper(F.regexp_replace("debut", "[^A-Za-z0-9]", "")))
            .join(F.broadcast(refs_k), "cle")
            .groupBy("ref")
            .agg(F.sum(F.when(whole & same, 1).otherwise(0)).alias("exact"),
                 F.sum(F.when(whole & ~same, 1).otherwise(0)).alias("occ_autre_graphie"),
                 F.array_sort(F.collect_set(F.when(whole & ~same, F.col("tok")))).alias("formes_autre_graphie"),
                 F.sum(F.when(~whole, 1).otherwise(0)).alias("occ_prefixe"),
                 F.array_sort(F.collect_set(F.when(~whole, F.col("tok")))).alias("formes_prefixe")))
    empty = F.array().cast("array<string>")
    a = (cat.join(hits, "ref", "left").join(space_ref_counts(corpus, cat), "ref", "left")
         .select(F.col("ref").alias("cle"), F.col("ref").alias("ref_catalogue"),
                 F.coalesce("exact", "occ_espace", F.lit(0)).alias("occ"),
                 F.coalesce("occ_autre_graphie", F.lit(0)).alias("occ_autre_graphie"),
                 F.coalesce("formes_autre_graphie", empty).alias("formes_autre_graphie"),
                 F.coalesce("occ_prefixe", F.lit(0)).alias("occ_prefixe"),
                 F.coalesce("formes_prefixe", empty).alias("formes_prefixe"),
                 F.lit(1).alias("nb_refs"),
                 F.array(F.col("ref")).alias("refs_source"),
                 F.array(F.concat(F.lit(INTRAQUAL_BASE), F.regexp_replace("ref", " ", "%20"))).alias("urls_source")))
    return conform(a, env, "A_reference", "reference")


def build(env: str) -> DataFrame:
    """Rows of the three sections for one environment.

    :param env: environment (dev or uat)
    :return: DataFrame with the columns of the table
    """
    corpus = corpus_source(env).cache()
    qsrc = question_source(env).cache()
    logger.info("[%s] parsed documents: %d, chat questions: %d", env, corpus.count(), qsrc.count())

    t0 = time.time()
    corpus_stats = keyed(classified(corpus)).cache()
    logger.info("[%s] section B: %d keys (%.0f s)", env, corpus_stats.count(), time.time() - t0)
    b = corpus_stats.where(F.col("occ") >= MIN_OCC)

    # Questions join the documents on the key alone: one key can have another category there (ST 1027 / ST-1027).
    by_key = (corpus_stats.groupBy("cle")
              .agg(F.sum("occ").alias("occ_corpus"),
                   F.array_sort(F.array_distinct(F.flatten(F.collect_list("formes")))).alias("formes_corpus"),
                   F.max_by(F.struct("nb_refs", "refs_source", "urls_source"), "occ").alias("r"))
              .select("cle", "occ_corpus", "formes_corpus", "r.nb_refs", "r.refs_source", "r.urls_source"))
    q = keyed(classified(qsrc)).select("categorie", "cle", "occ", "docs", "formes", "structures")
    c = q.join(by_key, "cle", "left")

    t0 = time.time()
    a = section_a(env, corpus).cache()
    logger.info("[%s] section A: %d REFs (%.0f s)", env, a.count(), time.time() - t0)

    return reduce(DataFrame.unionByName, [a, conform(b, env, "B_corpus"), conform(c, env, "C_questions")])

# COMMAND ----------

# MAGIC %md
# MAGIC ## Build
# MAGIC Replaces the rows of the environments of `ENVS`; the rows of the other environment are kept. About 40 minutes for DEV and UAT together.

# COMMAND ----------

out = reduce(DataFrame.unionByName, [build(env) for env in ENVS])
if spark.catalog.tableExists(TARGET) and "structures" not in spark.table(TARGET).columns:
    spark.sql(f"DROP TABLE {TARGET}")
if spark.catalog.tableExists(TARGET):
    for env in ENVS:
        spark.sql(f"DELETE FROM {TARGET} WHERE env = '{env}'")
    out.write.mode("append").option("mergeSchema", "true").saveAsTable(TARGET)
else:
    out.write.mode("overwrite").saveAsTable(TARGET)
logger.info("written to %s: %s", TARGET, ENVS)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Row counts
# MAGIC Rows per environment, section and category.

# COMMAND ----------

display(spark.table(TARGET).groupBy("env", "section", "categorie").count().orderBy("env", "section", "categorie"))
