# Databricks notebook source
# One-shot, submitted as the PROD job service principal: clones the parsing state of the former PROD schema
# (prod_landingzone.qualibot) into prod_proj.qualibot under the new names, so the first run parses only the files that
# the former state does not hold. The volume paths stored in the tables are rewritten to prod_proj. chunks is not copied:
# the run rebuilds it from the checkpoint. A table that already exists is left as it is unless "overwrite" is true.

import logging

logger = logging.getLogger("clone_legacy_state_to_prod")
logging.basicConfig(level=logging.INFO)

dbutils.widgets.removeAll()
dbutils.widgets.dropdown("dry_run", "true", ["true", "false"], "Dry run (read and list only)")
dbutils.widgets.dropdown("overwrite", "false", ["false", "true"], "Replace a table that already exists")
DRY_RUN = dbutils.widgets.get("dry_run") == "true"
OVERWRITE = dbutils.widgets.get("overwrite") == "true"

SOURCE_SCHEMA = "prod_landingzone.qualibot"
TARGET_SCHEMA = "prod_proj.qualibot"
TABLES = {
    "_pipeline_checkpoint": "parsing_checkpoint",
    "image_metadata": "image_metadata",
}
SOURCE_PREFIX = "/Volumes/prod_landingzone/qualibot/"
TARGET_PREFIX = "/Volumes/prod_proj/qualibot/"

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
    """Point the volume paths stored in a cloned table to prod_proj.

    :param target: Fully qualified name of the cloned table.
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

for source_name, target_name in TABLES.items():
    source = f"{SOURCE_SCHEMA}.{source_name}"
    target = f"{TARGET_SCHEMA}.{target_name}"
    try:
        df = spark.table(source)
        source_rows = df.count()
    except Exception as error:
        failures.append(source)
        logger.error("source not readable: %s: %s", source, str(error).splitlines()[0][:200])
        continue
    extra = ""
    if "IDDOC" in df.columns:
        extra = f", {df.select('IDDOC').distinct().count()} distinct IDDOC"
    logger.info("source %s: %d rows%s; columns: %s", source, source_rows, extra, df.columns)
    if table_exists(target) and not OVERWRITE:
        logger.info("left as it is (exists): %s", target)
        continue
    logger.info("%s%s -> %s", "[dry run] " if DRY_RUN else "", source, target)
    if DRY_RUN:
        continue
    try:
        spark.sql(f"CREATE OR REPLACE TABLE {target} DEEP CLONE {source}")
        rewrite_paths(target)
        target_rows = spark.table(target).count()
        if target_rows != source_rows:
            raise RuntimeError(f"{target_rows} rows cloned, {source_rows} in the source")
        logger.info("cloned: %s (%d rows)", target, target_rows)
    except Exception as error:
        failures.append(target)
        logger.error("not cloned: %s: %s", target, error)

if failures:
    raise RuntimeError(f"not done: {failures}")
