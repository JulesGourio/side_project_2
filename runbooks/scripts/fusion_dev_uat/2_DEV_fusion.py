# Databricks notebook source
# One-shot. Workspace DEV. Identity: your own account if the rights check passes, otherwise the DEV job runner.
# DEV + UAT merge, DEV side. For every document (IDDOC, document_sha256) it keeps the best parse result of the two
# environments: Docling before the XML fallback, then more images, then more text (+1 %). The same rule runs on both
# sides, so both end with the same results. No parsing, no LLM call: the winning row, its image_metadata rows (with
# their descriptions) and, by the image copy step of README.md, its image files come from the other environment.
# Rows queued for LLM-OCR (pending_llm_ocr) are never touched. An IDDOC the other side knows with other bytes is skipped.
# Changes, with a backup of each table first: parsing_checkpoint (rows replaced or added), image_metadata (rows of the
# switched IDDOCs), parsed_documents (rows of the switched IDDOCs deleted, so the next incremental run rebuilds their
# chunks only). chunks and the index are NOT touched here: the next incremental run updates the switched documents only.
# Before it: snapshot of the other side copied into /Volumes/dev_proj/qualibot/staging/merge/uat.

# COMMAND ----------

import pyspark.sql.functions as f
from pyspark.sql.window import Window

SCHEMA = "dev_proj.qualibot"
OTHER_DIR = "/Volumes/dev_proj/qualibot/staging/merge/uat"
OWN_PREFIX = "/Volumes/dev_proj/"
OTHER_PREFIX = "/Volumes/uat_proj/"
IMAGES_LIST_FILE = "/Volumes/dev_proj/qualibot/staging/merge/dev_images_from_uat.txt"
# Rows this side already received from the other one (notebook 2): their images still have to come over.
RECEIVED_BACKUP = None
BACKUP_SUFFIX = "_before_merge"
REUSABLE_STATUSES = ["SUCCESS", "SKIPPED_EMPTY_SMALL_FILE"]  # utils/parsing_pipeline/parse_steps.py
APPLY = True  # False: prints the plan only

CHECKPOINT = f"{SCHEMA}.parsing_checkpoint"
IMAGES = f"{SCHEMA}.image_metadata"
PROCESSED = f"{SCHEMA}.parsed_documents"
KEY = ["IDDOC", "document_sha256"]

# COMMAND ----------

# Rights check: changes nothing
try:
    for table in (CHECKPOINT, IMAGES, PROCESSED):
        spark.sql(f"DELETE FROM {table} WHERE 1 = 0")
    spark.sql(f"CREATE TABLE IF NOT EXISTS {SCHEMA}._rights_probe_merge (x INT)")
    spark.sql(f"DROP TABLE {SCHEMA}._rights_probe_merge")
except Exception as error:
    raise RuntimeError("Droits insuffisants : lance ce notebook avec le job runner (Workflows > Create job > Run as).") from error

# COMMAND ----------

# Other side's snapshot, complete?
def read_snapshot(table):
    df = spark.read.format("parquet").load(f"{OTHER_DIR}/{table}")
    expected = int(dbutils.fs.head(f"dbfs:{OTHER_DIR}/rows_{table}.txt").strip())
    if df.count() != expected:
        raise RuntimeError(f"Snapshot {table} incomplet : refaire la copie de fichiers")
    return df


df_other_ckpt = read_snapshot("parsing_checkpoint")
df_other_img = read_snapshot("image_metadata")
df_own_ckpt = spark.table(CHECKPOINT)

# COMMAND ----------

# Best reusable row per document on each side
def best_rows(df):
    ranked = (
        df.filter(f.col("parse_status").isin(REUSABLE_STATUSES) | f.col("parser_strategy").startswith("pending_llm_ocr"))
        .withColumn("_rank", f.when(f.col("parser_strategy").startswith("docling"), 0).otherwise(1))
        .withColumn("_img", f.coalesce(f.col("image_count"), f.lit(0)))
        .withColumn("_len", f.coalesce(f.length("document_text"), f.lit(0)))
        .withColumn("_pending", f.coalesce(f.col("parser_strategy").startswith("pending_llm_ocr"), f.lit(False)))
        .withColumn("_present", f.lit(True))
    )
    order = Window.partitionBy(*KEY).orderBy("_rank", f.desc("_img"), f.desc("_len"), f.desc("ingestion_timestamp"))
    return ranked.withColumn("_rn", f.row_number().over(order)).filter("_rn = 1").drop("_rn")


o = best_rows(df_own_ckpt).alias("o")
t = best_rows(df_other_ckpt).alias("t")
df_cmp = o.join(t, KEY, "full_outer")

own_shas = df_own_ckpt.groupBy("IDDOC").agg(f.collect_set("document_sha256").alias("own_shas"))
df_cmp = df_cmp.join(own_shas, "IDDOC", "left")

other_better = (
    ~f.col("o._pending") & ~f.col("t._pending")
    & (
        (f.col("t._rank") < f.col("o._rank"))
        | ((f.col("t._rank") == f.col("o._rank")) & (f.col("t._img") > f.col("o._img")))
        | ((f.col("t._rank") == f.col("o._rank")) & (f.col("t._img") == f.col("o._img")) & (f.col("t._len") > 1.01 * f.col("o._len")))
    )
)
both = f.col("o._present").isNotNull() & f.col("t._present").isNotNull()
only_other = f.col("o._present").isNull() & f.col("t._present").isNotNull()
# Same parse on both sides (a row received earlier): same ingestion run.
same_parse = both & (f.col("o.ingestion_run_id") == f.col("t.ingestion_run_id"))
other_version = f.col("own_shas").isNotNull() & ~f.array_contains(f.col("own_shas"), f.col("document_sha256"))

df_plan = df_cmp.select(
    *KEY,
    f.when(both & other_better, "REPLACE")
    .when(only_other & ~other_version, "ADD")
    .when(only_other & other_version, "SKIP_OTHER_VERSION")
    .otherwise("KEEP")
    .alias("action"),
    same_parse.alias("_same_parse"),
)
if RECEIVED_BACKUP:
    df_received = df_own_ckpt.select(*KEY).distinct().join(spark.table(RECEIVED_BACKUP).select(*KEY).distinct(), KEY, "left_anti")
    df_plan = df_plan.join(df_received.withColumn("_received", f.lit(True)), KEY, "left").withColumn(
        "action",
        f.when((f.col("action") == "KEEP") & f.col("_received") & f.col("_same_parse"), "IMAGES_ONLY").otherwise(f.col("action")),
    ).drop("_received")
df_plan = df_plan.drop("_same_parse").cache()

print("Plan (documents) :")
df_plan.groupBy("action").count().orderBy("action").show()

# COMMAND ----------

# Apply
switched = df_plan.filter(f.col("action").isin("REPLACE", "ADD", "IMAGES_ONLY")).select("IDDOC").distinct()
switched_ids = sorted(r.IDDOC for r in switched.collect())
print(f"IDDOC dont les images et descriptions viennent de l'autre cote : {len(switched_ids)}")


def rewrite_paths(df, schema):
    return df.select(
        f.from_json(f.regexp_replace(f.to_json(f.struct(*[f.col(c) for c in schema.fieldNames()])), OTHER_PREFIX, OWN_PREFIX), schema).alias("r")
    ).select("r.*")


if APPLY and switched_ids:
    for table in (CHECKPOINT, IMAGES, PROCESSED):
        spark.sql(f"CREATE TABLE IF NOT EXISTS {table}{BACKUP_SUFFIX} DEEP CLONE {table}")
    print("Sauvegardes : *" + BACKUP_SUFFIX)

    # parsing_checkpoint: rows of the REPLACE documents removed, the other side's best row inserted (REPLACE and ADD)
    keys_in = df_plan.filter(f.col("action").isin("REPLACE", "ADD")).select(*KEY)
    keys_replace = df_plan.filter(f.col("action") == "REPLACE").select(*KEY)
    keys_replace.createOrReplaceTempView("_merge_replace_keys")
    spark.sql(f"""
        DELETE FROM {CHECKPOINT} c
        WHERE EXISTS (SELECT 1 FROM _merge_replace_keys k WHERE k.IDDOC = c.IDDOC AND k.document_sha256 = c.document_sha256)
    """)
    ckpt_schema = spark.table(CHECKPOINT).schema
    df_new_rows = best_rows(df_other_ckpt).join(keys_in, KEY, "left_semi").select(*ckpt_schema.fieldNames())
    rewrite_paths(df_new_rows, ckpt_schema).write.format("delta").mode("append").saveAsTable(CHECKPOINT)

    # image_metadata: rows of the switched IDDOCs replaced by the other side's (descriptions kept, no LLM call)
    switched.createOrReplaceTempView("_merge_switched")
    spark.sql(f"DELETE FROM {IMAGES} WHERE IDDOC IN (SELECT IDDOC FROM _merge_switched)")
    img_schema = spark.table(IMAGES).schema
    df_img = df_other_img.join(switched, "IDDOC", "left_semi").select(*img_schema.fieldNames())
    rewrite_paths(df_img, img_schema).write.format("delta").mode("append").saveAsTable(IMAGES)

    # parsed_documents: rows of the switched IDDOCs removed, so the next incremental run rebuilds their chunks only
    spark.sql(f"DELETE FROM {PROCESSED} WHERE IDDOC IN (SELECT IDDOC FROM _merge_switched)")

    dbutils.fs.put(f"dbfs:{IMAGES_LIST_FILE}", "\n".join(str(i) for i in switched_ids), overwrite=True)

    left = sum(
        spark.table(tb).filter(f.to_json(f.struct("*")).contains(OTHER_PREFIX)).count() for tb in (CHECKPOINT, IMAGES)
    )
    print(f"OK. Chemins de l'autre environnement restants : {left}. Liste des dossiers images a copier : {IMAGES_LIST_FILE}")
    print("Retour arriere : CREATE OR REPLACE TABLE <table> DEEP CLONE <table>" + BACKUP_SUFFIX)
elif not APPLY:
    print("APPLY = False : rien n'a ete ecrit")
