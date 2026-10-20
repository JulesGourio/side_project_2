"""selection.py — driver-side file selection of the `3_parse` task.

Lists the volume folders of the target IDDOCs with `binaryFile` (metadata and content apart), ranks every candidate
file per IDDOC (D_ before Dm_, then the format priority, then the newest mtime) and keeps the best supported one.
The IDDOC extraction is defined once (`iddoc_column`, and its driver-side twin `extract_iddoc_from_name`).
"""

import re
from itertools import chain
from typing import List

from pyspark.sql import Column, DataFrame
from pyspark.sql import functions as F
from pyspark.sql import types as T
from pyspark.sql.window import Window, WindowSpec

from config import ENRICHMENT_COLS

FORMAT_PRIORITIES = {
    "docx": 1, "pdf": 2, "docm": 3, "doc": 4, "html": 5,
    "pptx": 6, "ppt": 7, "txt": 8, "xml": 9,
    "xlsx": 10, "xls": 11, "xlsm": 12, "xlsb": 13,
    # Must stay listed here or these formats are silently excluded.
    "rtf": 14, "odt": 15, "ods": 16,
}
UNSUPPORTED_PRIORITY = 99

TRASH_FILES_BLACKLIST = ["headers.html", "thumbs.db", ".ds_store", "desktop.ini", "copyright.txt"]

# Ranking-only columns, dropped once a rank has been picked.
RANK_HELPER_COLS = ("ext_priority", "is_dm_file", "priority_rank", "files_count_for_iddoc")


def iddoc_column(path_col: str = "source_path", name_col: str = "source_file_name") -> Column:
    """Spark column with the IDDOC as bigint. Priority: /D_<n>/ folder, D_<n> file name, leading bare number.
    Matches the D_ and Dm_ prefixes, case-insensitive.

    :param path_col: Column with the volume path of the file.
    :param name_col: Column with the file name.
    :return: IDDOC as bigint, null when no id is found.
    """
    folder_id = F.regexp_extract(F.col(path_col), r"/[dD]m?_(\d+)/", 1)
    file_id = F.regexp_extract(F.col(name_col), r"^[dD]m?_(\d+)", 1)
    raw_num = F.regexp_extract(F.col(name_col), r"^(\d+)", 1)
    return F.coalesce(
        F.nullif(folder_id, F.lit("")),
        F.nullif(file_id, F.lit("")),
        F.nullif(raw_num, F.lit("")),
    ).try_cast("bigint")


_IDDOC_NAME_RE = re.compile(r"^[Dd]m?_(\d+)")


def extract_iddoc_from_name(name: str) -> int | None:
    """Driver-side twin of `iddoc_column` for a root-level volume item name (D_<n> / Dm_<n>, case-insensitive), or
    None. Keep both in sync if the naming convention changes.

    :param name: Root-level item name, such as ``D_123`` or ``Dm_123/``; None is accepted.
    """
    m = _IDDOC_NAME_RE.match((name or "").rstrip("/"))
    return int(m.group(1)) if m else None


# --- Distributed file scan (metadata only) ---
_EMPTY_META_SCHEMA = T.StructType([
    T.StructField("source_path", T.StringType()),
    T.StructField("source_file_size_bytes", T.LongType()),
    T.StructField("source_modification_time", T.TimestampType()),
    T.StructField("source_file_name", T.StringType()),
    T.StructField("source_file_extension", T.StringType()),
    T.StructField("source_folder_path", T.StringType()),
    T.StructField("IDDOC", T.LongType()),
])
_EMPTY_CONTENT_SCHEMA = T.StructType([
    T.StructField("source_path", T.StringType()),
    T.StructField("content", T.BinaryType()),
])


def scan_volume_paths(spark, paths: List[str]) -> tuple[DataFrame, DataFrame]:
    """(df_meta_raw, df_content) read distributively with binaryFile from an explicit list of folder paths, one per
    target IDDOC, so a run does not re-list the whole volume.

    df_meta_raw: one row per file (path, size, mtime, extension, IDDOC), no content.
    df_content: (source_path, content) to join the bytes back on the selected files.
    Empty but correctly typed DataFrames when `paths` is empty: binaryFile errors out on a zero-path load.

    :param spark: Active Spark session.
    :param paths: Folders to scan, one per target IDDOC.
    :return: metadata DataFrame, one row per file, and content DataFrame.
    """
    if not paths:
        return (
            spark.createDataFrame([], schema=_EMPTY_META_SCHEMA),
            spark.createDataFrame([], schema=_EMPTY_CONTENT_SCHEMA),
        )
    df_files_raw = spark.read.format("binaryFile").option("recursiveFileLookup", "true").load(paths)

    df_meta_raw = (
        df_files_raw
        .select(
            F.col("path").alias("source_path"),
            F.col("length").try_cast("long").alias("source_file_size_bytes"),
            F.col("modificationTime").alias("source_modification_time"),
        )
        .withColumn("source_file_name", F.element_at(F.split(F.col("source_path"), "/"), -1))
        .withColumn("source_file_extension", F.lower(F.regexp_extract(F.col("source_file_name"), r"\.([^.]+)$", 1)))
        .withColumn("source_folder_path", F.regexp_replace(F.col("source_path"), r"/[^/]+$", ""))
        .filter(
            ~F.lower(F.col("source_file_name")).isin([t.lower() for t in TRASH_FILES_BLACKLIST])
            & ~F.col("source_file_name").startswith("~")
        )
        .withColumn("IDDOC", iddoc_column())
    )
    return df_meta_raw, df_files_raw.select(F.col("path").alias("source_path"), "content")


# --- Ranking and selection ---
def _priority_window() -> WindowSpec:
    """Order of the candidates of one IDDOC; the last key keeps row_number() stable on same-mtime ties.

    :return: window that orders the candidates of one IDDOC.
    """
    return Window.partitionBy("IDDOC").orderBy(
        F.col("is_dm_file").asc(), F.col("ext_priority").asc(),
        F.col("source_modification_time").desc(), F.col("source_path").asc(),
    )


def rank_candidates(df_meta_raw) -> DataFrame:
    """Rank every file that has an IDDOC. The volume is the source of truth: the business metadata does not filter
    here, it is joined later by `select_best_files`. Adds ext_priority, is_dm_file, priority_rank and
    files_count_for_iddoc.

    :param df_meta_raw: File metadata, from ``scan_volume_paths``.
    :return: candidate files that have an IDDOC, with the rank columns.
    """
    mapping_expr = F.create_map([F.lit(x) for x in chain(*FORMAT_PRIORITIES.items())])
    return (
        df_meta_raw.filter(F.col("IDDOC").isNotNull())
        .withColumn("ext_priority", F.coalesce(mapping_expr[F.col("source_file_extension")], F.lit(UNSUPPORTED_PRIORITY)))
        .withColumn("is_dm_file", F.when(F.col("source_file_name").rlike(r"^[dD]m_"), 1).otherwise(0))
        .withColumn("priority_rank", F.row_number().over(_priority_window()))
        .withColumn("files_count_for_iddoc", F.count("*").over(Window.partitionBy("IDDOC")))
    )


def select_best_files(df_matched_full, df_business_meta) -> DataFrame:
    """The best SUPPORTED file per IDDOC, enriched with the business metadata (no content yet).

    Re-ranks among the supported candidates only: an IDDOC whose overall rank-1 file is an unsupported format (a stray
    .msg or .zip) still gets its best supported file. IDDOCs whose `ref` cannot be resolved are dropped: their folder
    is an orphan (the document was created then deleted in Intraqual), so there is no metadata to parse with.
    REF '-' is a placeholder used by some documents and is kept.

    :param df_matched_full: Ranked candidate files, from ``rank_candidates``.
    :param df_business_meta: Business metadata with an ``IDDOC`` column.
    """
    df_sel = (
        df_matched_full.filter(F.col("ext_priority") < UNSUPPORTED_PRIORITY)
        .withColumn("_supported_rank", F.row_number().over(_priority_window()))
        .filter(F.col("_supported_rank") == 1)
        .drop("_supported_rank", *RANK_HELPER_COLS)
    )

    biz_cols = [c for c in ENRICHMENT_COLS if c in df_business_meta.columns]
    df_biz = df_business_meta.select("IDDOC", *biz_cols).dropDuplicates(["IDDOC"])
    df_sel = df_sel.join(F.broadcast(df_biz), on="IDDOC", how="left").filter(F.col("ref").isNotNull())

    # The string columns and doc_date exist even when the manifest lacks them.
    for c in ENRICHMENT_COLS:
        if c not in df_sel.columns:
            df_sel = df_sel.withColumn(c, F.lit(None).try_cast("date" if c == "doc_date" else "string"))
    return df_sel


def attach_content(df_selected, df_content, ingestion_run_id: str) -> DataFrame:
    """Join the file bytes back to the selected files and add the ingestion columns.

    :param df_selected: Selected files, from ``select_best_files``.
    :param df_content: Source path and bytes of the files, from ``scan_volume_paths``.
    :param ingestion_run_id: Id of the run, stored with every file.
    :return: selected files with their bytes, SHA-256 and ingestion columns.
    """
    return (
        df_selected
        .join(df_content, on="source_path", how="inner")
        .withColumn("document_sha256", F.sha2(F.col("content"), 256))
        .withColumn("ingestion_run_id", F.lit(ingestion_run_id))
        .withColumn("ingestion_timestamp", F.current_timestamp())
    )
