"""selection.py — Driver-side file selection for the generic pipeline.

Simplified version of parsing_pipeline/selection.py, stripped of all
Intraqual-specific logic (IDDOC extraction, category hierarchy, division
AS/IS split, gd_doc/gd_cat tables).

Metadata is derived from the file name only (title = stem, cleaned up).
"""


from pyspark.sql import functions as F
from pyspark.sql.window import Window

# ---------------------------------------------------------------------------
# Selection constants (same as parsing_pipeline)
# ---------------------------------------------------------------------------
FORMAT_PRIORITIES = {
    "docx": 1, "pdf": 2, "docm": 3, "doc": 4, "html": 5,
    "pptx": 6, "ppt": 7, "txt": 8, "xml": 9,
    "xlsx": 10, "xls": 11, "xlsm": 12, "xlsb": 13,
    "rtf": 14, "odt": 15, "ods": 16,
}
SUPPORTED_EXTENSIONS = set(FORMAT_PRIORITIES.keys())

TRASH_FILES_BLACKLIST = [
    "headers.html", "thumbs.db", ".ds_store", "desktop.ini", "copyright.txt",
]


# ---------------------------------------------------------------------------
# Doc ID extraction — replaces IDDOC logic with a file-name-based stable ID
# ---------------------------------------------------------------------------
def doc_id_column(path_col: str = "path"):
    """Return a Spark Column computing a stable doc_id from the file path.

    Uses the file name (without extension) as the document identifier.
    """
    file_name = F.element_at(F.split(F.col(path_col), "/"), -1)
    return F.regexp_replace(
        F.element_at(F.split(file_name, r"\."), 1),
        r"[_\-]+", " "
    )


# ---------------------------------------------------------------------------
# Volume scanning
# ---------------------------------------------------------------------------
def scan_volume(spark, volume_path: str):
    """Read all supported files from a UC Volume as binary.

    :param spark: Active SparkSession.
    :param volume_path: UC Volume path (e.g. /Volumes/catalog/schema/volume).
    :return: (df_meta, df_content) — metadata and binary content DataFrames.
    """
    df_raw = (
        spark.read.format("binaryFile")
        .option("recursiveFileLookup", "true")
        .option("pathGlobFilter", "*")
        .load(volume_path)
    )

    df_meta = (
        df_raw
        .withColumn("source_file_name",
                    F.element_at(F.split(F.col("path"), "/"), -1))
        .withColumn("source_file_extension",
                    F.lower(F.element_at(F.split(F.col("source_file_name"), r"\."), -1)))
        .withColumn("source_file_size_bytes", F.col("length"))
        .withColumn("source_path", F.col("path"))
        .withColumn("doc_title",
                    F.regexp_replace(
                        F.element_at(F.split(F.col("source_file_name"), r"\."), 1),
                        r"[_\-]+", " "
                    ))
        .filter(F.col("source_file_extension").isin(list(SUPPORTED_EXTENSIONS)))
        .filter(F.col("source_file_size_bytes") > 0)
        .filter(~F.lower(F.col("source_file_name")).isin(TRASH_FILES_BLACKLIST))
    )

    # Keep content separate (heavy column) — joined back in attach_content.
    df_content = df_meta.select("path", "content")
    df_meta_light = df_meta.drop("content", "length")

    return df_meta_light, df_content


def select_files(df_meta):
    """Select one file per document (by name), preferring higher-priority formats.

    When multiple files share the same stem (e.g. report.docx and report.pdf),
    the one with the best format priority wins.

    :param df_meta: DataFrame from scan_volume (metadata only).
    :return: DataFrame with one row per unique document.
    """
    # Stem = file name without extension.
    df = df_meta.withColumn(
        "file_stem",
        F.element_at(F.split(F.col("source_file_name"), r"\."), 1),
    )

    # Priority ranking.
    priority_map = F.create_map(
        *[item for pair in FORMAT_PRIORITIES.items() for item in (F.lit(pair[0]), F.lit(pair[1]))]
    )
    df = df.withColumn("ext_priority",
                       F.coalesce(priority_map[F.col("source_file_extension")], F.lit(99)))

    w = Window.partitionBy("file_stem").orderBy(
        F.col("ext_priority").asc(),
        F.col("modificationTime").desc(),
    )
    df = (
        df.withColumn("_rank", F.row_number().over(w))
        .filter(F.col("_rank") == 1)
        .drop("_rank", "ext_priority", "file_stem")
    )
    return df


def attach_content(df_selected, df_content, ingestion_run_id: str):
    """Re-join binary content onto the selected files.

    :param df_selected: One-row-per-document metadata.
    :param df_content: (path, content) from scan_volume.
    :param ingestion_run_id: UUID for this run.
    :return: DataFrame with content column attached.
    """
    return (
        df_selected
        .join(df_content, on="path", how="inner")
        .withColumn("ingestion_run_id", F.lit(ingestion_run_id))
    )
