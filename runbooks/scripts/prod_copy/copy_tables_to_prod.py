# Databricks notebook source
# One-shot, submitted as the PROD job service principal: copies the corpus and parsing state of one environment
# (default dev) into prod_proj.qualibot, so the first PROD run neither parses nor describes anything again.
# The volume paths stored in the tables are rewritten to prod_proj. The tables of the chat, of the comparison and of
# Lakebase are never touched. A table that already exists is left as it is unless "overwrite" is true.

import logging

logger = logging.getLogger("copy_tables_to_prod")
logging.basicConfig(level=logging.INFO)

dbutils.widgets.removeAll()
dbutils.widgets.dropdown("source_env", "dev", ["dev", "uat"], "Environment to copy from")
dbutils.widgets.dropdown("dry_run", "true", ["true", "false"], "Dry run (list only)")
dbutils.widgets.dropdown("overwrite", "false", ["false", "true"], "Replace a table that already exists")
SOURCE_ENV = dbutils.widgets.get("source_env")
DRY_RUN = dbutils.widgets.get("dry_run") == "true"
OVERWRITE = dbutils.widgets.get("overwrite") == "true"

SOURCE_SCHEMA = f"{SOURCE_ENV}_proj.qualibot"
TARGET_SCHEMA = "prod_proj.qualibot"
TABLES = [
    "category_reference",
    "parse_manifest",
    "parsing_checkpoint",
    "image_metadata",
    "parsed_documents",
    "chunks_archive",
    "chunks",
]
SOURCE_PREFIX = f"/Volumes/{SOURCE_ENV}_proj/"
TARGET_PREFIX = "/Volumes/prod_proj/"

failures = []


def table_exists(name: str) -> bool:
    """Tell whether a table exists.

    :param name: Fully qualified table name.
    :return: True when the table can be read.
    """
    try:
        spark.table(name).limit(1).collect()
        return True
    except Exception:
        return False


def rewrite_paths(target: str) -> None:
    """Point the volume paths stored in a copied table to prod_proj.

    :param target: Fully qualified name of the copied table.
    :return: None
    """
    columns = {field.name: field.dataType.simpleString() for field in spark.table(target).schema.fields}
    if "volume_path" in columns:
        spark.sql(
            f"UPDATE {target} SET volume_path = replace(volume_path, '{SOURCE_PREFIX}', '{TARGET_PREFIX}') "
            f"WHERE volume_path LIKE '{SOURCE_PREFIX}%'"
        )
    if "images" in columns and "volume_path" in columns["images"]:
        spark.sql(
            f"UPDATE {target} SET images = transform(images, x -> named_struct("
            "'image_id', x.image_id, 'page_no', x.page_no, 'label', x.label, 'area_ratio', x.area_ratio, "
            "'captions', x.captions, 'context_text', x.context_text, "
            f"'volume_path', replace(x.volume_path, '{SOURCE_PREFIX}', '{TARGET_PREFIX}'), "
            "'image_width', x.image_width, 'image_height', x.image_height)) WHERE images IS NOT NULL"
        )


# COMMAND ----------

if not DRY_RUN:
    spark.sql(f"CREATE SCHEMA IF NOT EXISTS {TARGET_SCHEMA}")

for table in TABLES:
    source = f"{SOURCE_SCHEMA}.{table}"
    target = f"{TARGET_SCHEMA}.{table}"
    try:
        source_rows = spark.table(source).count()
    except Exception as error:
        failures.append(source)
        logger.error("source not readable: %s: %s", source, str(error).splitlines()[0][:200])
        continue
    if table_exists(target) and not OVERWRITE:
        logger.info("left as it is (exists): %s", target)
        continue
    logger.info("%s%s -> %s (%d rows)", "[dry run] " if DRY_RUN else "", source, target, source_rows)
    if DRY_RUN:
        continue
    try:
        spark.sql(f"CREATE OR REPLACE TABLE {target} DEEP CLONE {source}")
        rewrite_paths(target)
        target_rows = spark.table(target).count()
        if target_rows != source_rows:
            raise RuntimeError(f"{target_rows} rows copied, {source_rows} in the source")
        logger.info("copied: %s (%d rows)", target, target_rows)
    except Exception as error:
        failures.append(target)
        logger.error("not copied: %s: %s", target, error)

if failures:
    raise RuntimeError(f"not done: {failures}")
