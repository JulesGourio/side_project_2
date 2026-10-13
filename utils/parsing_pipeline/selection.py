"""selection.py — Driver-side file selection, business enrichment & audit.

Everything here runs on the driver and returns Spark DataFrames:

  - Business metadata from gd_doc_cat / gd_knowledge_base / gd_doc / gd_cat,
    with ref / titre / category hierarchy enrichment.
  - Distributed file reading via spark.read.format("binaryFile").
  - Per-IDDOC prioritisation (D_ > Dm_ > format priority > newest mtime) keeping
    one file per IDDOC, with PARSE_FILTER (None / int sample / explicit list).
  - A retry path that picks the next-best file for IDDOCs that failed to parse.
  - A unified diagnostic audit table.

IDDOC extraction is defined in one place (iddoc_column) and reused throughout.
"""

import re as _re
from itertools import chain
from typing import Optional, List

from pyspark.sql import functions as F
from pyspark.sql import types as T
from pyspark.sql.window import Window

# ---------------------------------------------------------------------------
# Selection constants
# ---------------------------------------------------------------------------
FORMAT_PRIORITIES = {
    "docx": 1, "pdf": 2, "docm": 3, "doc": 4, "html": 5,
    "pptx": 6, "ppt": 7, "txt": 8, "xml": 9,
    "xlsx": 10, "xls": 11, "xlsm": 12, "xlsb": 13,
    # Must stay listed here or these formats are silently excluded.
    "rtf": 14, "odt": 15, "ods": 16,
}
SUPPORTED_EXTENSIONS = set(FORMAT_PRIORITIES.keys())

TRASH_FILES_BLACKLIST = ["headers.html", "thumbs.db", ".ds_store", "desktop.ini", "copyright.txt"]

# Ranking-only columns dropped once a rank has been picked (select_best_files,
# get_retry_candidates) — not part of the business-facing schema downstream.
_RANK_HELPER_COLS = ("ext_priority", "is_dm_file", "priority_rank", "files_count_for_iddoc")

# ---------------------------------------------------------------------------
# Category hierarchy constants (replaces the former CATEGORY_PREFIX_MAP)
# ---------------------------------------------------------------------------
IDCAT_PROCESSES = 12       # "1 - PROCESSES / PROCESSUS" → belongs to AS
IDCAT_AS = 3346            # "AS - Aerostructures"
IDCAT_IS = 3284            # "IS - Interconnection Systems"
IDCAT_DIRECTIVE = 962      # "0 - DIRECTIVE GROUPE LATECOERE"
MAX_HIERARCHY_DEPTH = 7    # Levels 1 to 7 in gd_cat

# Business tables — MV bronze (*_latest), auto-refreshed daily and always
# the current snapshot: no extra "keep only the latest extraction" read needed.
from config import (
    GD_DOC_CAT_LATEST     as GD_DOC_CAT_TABLE,
    GD_CAT_LATEST         as GD_CAT_TABLE,
    GD_TYPDOC_LATEST      as GD_TYPDOC_TABLE,
    GD_UTILISATEUR_LATEST as GD_UTILISATEUR_TABLE,
)


def _normalize_cols_upper(df):
    """Rename all DataFrame columns to UPPERCASE.

    MV bronze tables (*_latest) sometimes expose lowercase columns (e.g.
    gd_doc_cat_latest: iddoc, idcat, principale) while the rest of the code
    expects UPPERCASE columns (original gd_doc* schema). A no-op rename is
    applied to columns already in uppercase.
    """
    for c in df.columns:
        if c != c.upper():
            df = df.withColumnRenamed(c, c.upper())
    return df


# ---------------------------------------------------------------------------
# IDDOC extraction
# ---------------------------------------------------------------------------
def iddoc_column(path_col: str = "source_path", name_col: str = "source_file_name"):
    """Return a Spark Column extracting the IDDOC as bigint.

    Priority: /D_<n>/ folder → D_<n> filename → leading bare number.
    Matches D_ and Dm_ prefixes (case-insensitive)."""
    folder_id = F.regexp_extract(F.col(path_col), r"/[dD]m?_(\d+)/", 1)
    file_id = F.regexp_extract(F.col(name_col), r"^[dD]m?_(\d+)", 1)
    raw_num = F.regexp_extract(F.col(name_col), r"^(\d+)", 1)
    return F.coalesce(
        F.nullif(folder_id, F.lit("")),
        F.nullif(file_id, F.lit("")),
        F.nullif(raw_num, F.lit("")),
    ).cast("bigint")


_IDDOC_NAME_RE = _re.compile(r"^[Dd]m?_(\d+)")


def extract_iddoc_from_name(name: str) -> Optional[int]:
    """Driver-side counterpart of iddoc_column(), for a root-level volume item
    name obtained via dbutils.fs.ls (D_<n> / Dm_<n>, case-insensitive).
    Returns None if the name doesn't match. Same pattern as iddoc_column() —
    keep both in sync if the naming convention ever changes."""
    m = _IDDOC_NAME_RE.match((name or "").rstrip("/"))
    return int(m.group(1)) if m else None


# ===========================================================================
# Category hierarchy builder (dynamic, from gd_cat + gd_doc_cat)
# ===========================================================================
def build_category_hierarchy(spark, idlg: int = 1, max_depth: int = MAX_HIERARCHY_DEPTH):
    """Build a per-IDDOC category hierarchy by walking up gd_cat.IDPERE.

    Returns a DataFrame with columns:
        IDDOC, est_principale, division,
        niveau_plus_1, niveau_plus_2, ..., niveau_plus_{max_depth-1}

    Reproduces the logic of the Intraqual application for category display.
    """
    # The IDPERE walk-up below self-joins gd_cat against itself, which Spark
    # blocks by default for this MV type.
    spark.conf.set("spark.databricks.remoteFiltering.blockSelfJoins", "false")

    df_doc_cat = _normalize_cols_upper(spark.read.table(GD_DOC_CAT_TABLE))
    df_cat = _normalize_cols_upper(spark.read.table(GD_CAT_TABLE)).filter(F.col("IDLG") == idlg)

    # Flag principal category
    df_doc_cat = df_doc_cat.withColumn(
        "est_principale",
        F.when(F.lower(F.col("PRINCIPALE")) == "oui", True).otherwise(False)
    )

    # Build initial hierarchy (leaf level)
    df_hierarchy = (
        df_doc_cat
        .join(df_cat.select("IDCAT", "IDPERE", "NIVEAU", "NOMCAT"), on="IDCAT", how="inner")
        .select(
            "IDDOC",
            "est_principale",
            F.col("IDCAT").alias("current_idcat"),
            F.col("IDPERE").alias("current_idpere"),
            F.array(F.struct(
                F.col("NIVEAU").alias("niveau"),
                F.col("NOMCAT").alias("nomcat"),
                F.col("IDCAT").alias("idcat")
            )).alias("categories")
        )
    )

    # Walk up the tree following IDPERE
    for _ in range(max_depth - 1):
        df_parent = df_cat.select(
            F.col("IDCAT").alias("parent_idcat"),
            F.col("IDPERE").alias("parent_idpere"),
            F.col("NIVEAU").alias("parent_niveau"),
            F.col("NOMCAT").alias("parent_nomcat")
        )

        df_hierarchy = (
            df_hierarchy
            .join(
                df_parent,
                (df_hierarchy["current_idpere"] == df_parent["parent_idcat"]) &
                (df_hierarchy["current_idpere"] > 0),
                how="left"
            )
            .withColumn(
                "categories",
                F.when(
                    F.col("parent_idcat").isNotNull(),
                    F.concat(
                        F.col("categories"),
                        F.array(F.struct(
                            F.col("parent_niveau").alias("niveau"),
                            F.col("parent_nomcat").alias("nomcat"),
                            F.col("parent_idcat").alias("idcat")
                        ))
                    )
                ).otherwise(F.col("categories"))
            )
            .withColumn("current_idcat", F.coalesce(F.col("parent_idcat"), F.col("current_idcat")))
            .withColumn("current_idpere", F.coalesce(F.col("parent_idpere"), F.lit(0)))
            .drop("parent_idcat", "parent_idpere", "parent_niveau", "parent_nomcat")
        )

    # Determine root category (NIVEAU=1) and derive division
    df_hierarchy = df_hierarchy.withColumn(
        "root_idcat",
        F.expr("get(filter(categories, x -> x.niveau = 1), 0).idcat")
    )

    # Derive division from root category name (dynamic, not hardcoded IDs)
    root_nomcat_col = F.expr("get(filter(categories, x -> x.niveau = 1), 0).nomcat")
    df_hierarchy = df_hierarchy.withColumn("_root_nomcat", root_nomcat_col)
    df_hierarchy = df_hierarchy.withColumn(
        "division",
        # PROCESSES has no "AS -" prefix in its raw name despite belonging to division AS.
        F.when(F.col("root_idcat") == IDCAT_PROCESSES, F.lit("AS"))
         .when(F.col("_root_nomcat").startswith("AS -"), F.lit("AS"))
         .when(F.col("_root_nomcat").startswith("IS -"), F.lit("IS"))
         .when(F.col("root_idcat") == IDCAT_DIRECTIVE, F.lit("DIRECTIVE"))
         .otherwise(F.lit("AUTRE"))
    ).drop("_root_nomcat")

    # Extract raw level names
    for niv in range(1, max_depth + 1):
        df_hierarchy = df_hierarchy.withColumn(
            f"raw_niv{niv}",
            F.expr(f"get(filter(categories, x -> x.niveau = {niv}), 0).nomcat")
        )

    # Format niveau_plus_1 per application logic
    df_hierarchy = df_hierarchy.withColumn(
        "niveau_plus_1",
        F.when(
            F.col("root_idcat") == IDCAT_PROCESSES,
            F.lit("AS - PROCESSES / PROCESSUS")
        ).when(
            F.col("root_idcat") == IDCAT_AS,
            F.concat(F.lit("AS - "), F.regexp_replace(F.col("raw_niv2"), r"^\d+\s*-\s*", ""))
        ).when(
            F.col("root_idcat") == IDCAT_IS,
            F.lit("IS - Interconnection Systems")
        ).when(
            F.col("root_idcat") == IDCAT_DIRECTIVE,
            F.col("raw_niv1")
        ).otherwise(F.col("raw_niv1"))
    )

    # Format deeper levels with offset for AS root
    for output_niv in range(2, max_depth):
        df_hierarchy = df_hierarchy.withColumn(
            f"niveau_plus_{output_niv}",
            F.when(
                F.col("root_idcat") == IDCAT_AS,
                F.col(f"raw_niv{output_niv + 1}")
            ).otherwise(
                F.col(f"raw_niv{output_niv}")
            )
        )

    # Final selection
    hierarchy_cols = ["IDDOC", "est_principale", "division"] + \
                     [f"niveau_plus_{niv}" for niv in range(1, max_depth)]
    raw_cols = [f"raw_niv{niv}" for niv in range(1, max_depth + 1)]

    return df_hierarchy.select(*hierarchy_cols).drop(*[c for c in raw_cols if c in df_hierarchy.columns])


def build_division_reference(spark, max_depth: int = MAX_HIERARCHY_DEPTH):
    """One row per IDDOC: division + niveau_plus_1..N.

    Primary: build_category_hierarchy(). Fallback: DIVISION_ARCHIVE_TABLE fills
    gaps only, never overrides a live-resolved row; empty/unset = disabled (default).
    """
    from config import DIVISION_ARCHIVE_TABLE

    niveau_cols = [f"niveau_plus_{niv}" for niv in range(1, max_depth)]

    df_live = (
        build_category_hierarchy(spark, max_depth=max_depth)
        .filter(F.col("est_principale") == True)
        .dropDuplicates(["IDDOC"])
        .select("IDDOC", "division", *niveau_cols)
        .withColumn("division_source", F.lit("live_hierarchy"))
    )
    if not (DIVISION_ARCHIVE_TABLE or "").strip():
        return df_live

    df_archive = (
        spark.table(DIVISION_ARCHIVE_TABLE)
        .filter(F.col("division").isNotNull())
        .select("IDDOC", "division", *niveau_cols)
        .dropDuplicates(["IDDOC"])
        .join(df_live.select("IDDOC"), on="IDDOC", how="left_anti")
        .withColumn("division_source", F.lit("archive_fallback"))
    )

    return df_live.unionByName(df_archive)


# ===========================================================================
# Step 0 — Qualibot perimeter (scope gate)
# ===========================================================================
def load_scope_docs(spark):
    """Return (IDDOC, ref, titre) for every document inside the Qualibot scope (DOC_SCOPE_FILTER, config.py)."""
    from config import GD_DOC_LATEST as _GD_DOC_LATEST, DOC_SCOPE_FILTER

    return (
        _normalize_cols_upper(spark.read.table(_GD_DOC_LATEST))
        .filter(F.expr(DOC_SCOPE_FILTER))
        .filter(F.col("IDDOC").isNotNull())
        .select(
            F.col("IDDOC"),
            F.col("REF").alias("ref"),
            F.col("TITRE").alias("titre"),
        )
        .dropDuplicates(["IDDOC"])
    )


# ===========================================================================
# Step 1 — business metadata (DB-driven)
# ===========================================================================
def load_business_metadata(spark):
    """Return (df_business_meta, df_doc_lookup, df_kb_lookup).

    df_business_meta = 1 row per current IDDOC (gd_doc.COURANT==1), enriched with
    category hierarchy (division, niveaux) and document metadata (ref, titre,
    type_document, langue, auteur, doc_date) derived from gd_doc + gd_typdoc + gd_utilisateur.

    doc_date = gd_doc.DATEDIFF (date de diffusion), used downstream to filter out
    documents predating DOC_DATE_CUTOFF.

    gd_knowledge_base is NO LONGER used — all metadata comes from the source tables.
    """
    # Build the full category hierarchy per IDDOC
    df_cat_hierarchy = build_category_hierarchy(spark)

    # Keep only the principal category per IDDOC for the main enrichment
    df_cat_principal = (
        df_cat_hierarchy
        .filter(F.col("est_principale") == True)
        .dropDuplicates(["IDDOC"])
    )

    # Also build a concatenated "document_prefixes" from niveau_plus_1 for
    # backward compatibility (replaces the old CATEGORY_PREFIX_MAP approach)
    df_doc_prefixes = (
        df_cat_hierarchy
        .filter(F.col("niveau_plus_1").isNotNull())
        .groupBy("IDDOC")
        .agg(F.concat_ws(",", F.collect_set("niveau_plus_1")).alias("document_prefixes"))
    )

    # Highest COURANT (>0) wins, not strict ==1 — some docs only have COURANT=2.
    from config import GD_DOC_LATEST as _GD_DOC_LATEST, GD_DOC_FALLBACK as _GD_DOC_FALLBACK
    # DOC_DATE_RAW must stay in this list or doc_date resolves to NULL for every document.
    _IDDOC_COL = ["IDDOC", "REF", "TITRE", "COURANT", "ETAT", "NONVISIBLE",
                  "DIFFTOTALE", "IDTYPDOC", "REDACTEUR", "DOC_DATE_RAW"]

    def _with_canonical_date(df):
        cols = {c.upper() for c in df.columns}
        src = "DTDIFF" if "DTDIFF" in cols else "DATEDIFF" if "DATEDIFF" in cols else None
        return df.withColumn(
            "DOC_DATE_RAW",
            F.to_date(F.col(src)) if src else F.lit(None).cast("date"),
        )

    df_doc_primary = _with_canonical_date(_normalize_cols_upper(spark.read.table(_GD_DOC_LATEST)))
    for c in _IDDOC_COL:
        if c not in df_doc_primary.columns:
            df_doc_primary = df_doc_primary.withColumn(c, F.lit(None))

    if (_GD_DOC_FALLBACK or "").strip():
        primary_iddocs = {r.IDDOC for r in df_doc_primary.select("IDDOC").filter(F.col("IDDOC").isNotNull()).collect()}
        df_doc_fallback = _with_canonical_date(
            _normalize_cols_upper(spark.read.table(_GD_DOC_FALLBACK))
        ).filter(F.col("IDDOC").isNotNull() & ~F.col("IDDOC").isin(list(primary_iddocs)))
        for c in _IDDOC_COL:
            if c not in df_doc_fallback.columns:
                df_doc_fallback = df_doc_fallback.withColumn(c, F.lit(None))
        df_doc_all = df_doc_primary.select(*_IDDOC_COL).unionByName(
            df_doc_fallback.select(*_IDDOC_COL), allowMissingColumns=True
        )
    else:
        df_doc_all = df_doc_primary.select(*_IDDOC_COL)
    _w_courant = Window.partitionBy("IDDOC").orderBy(F.desc("COURANT"))
    df_doc_current = (
        df_doc_all
        .filter(F.col("COURANT") > 0)
        .withColumn("_rn", F.row_number().over(_w_courant))
        .filter(F.col("_rn") == 1)
        .drop("_rn")
    )

    # Diffusion date — already normalised to DOC_DATE_RAW above.
    _date_col_expr = F.col("DOC_DATE_RAW")

    # type_document from gd_typdoc (IDLG=1 for French labels)
    df_typdoc = (
        _normalize_cols_upper(spark.read.table(GD_TYPDOC_TABLE))
        .filter(F.col("IDLG") == 1)
        .select(F.col("IDTYPDOC"), F.col("NOMTYPDOC").alias("type_document"))
        .dropDuplicates(["IDTYPDOC"])
    )

    # auteur from gd_utilisateur (join on REDACTEUR = NUMUTILISATEUR)
    df_utilisateur = (
        _normalize_cols_upper(spark.read.table(GD_UTILISATEUR_TABLE))
        .select(
            F.col("NUMUTILISATEUR"),
            F.concat_ws(" ", F.col("PRENOM"), F.col("NOM")).alias("auteur")
        )
    )

    # categorie: principal category name via gd_doc_cat (PRINCIPALE='oui') → gd_cat (NOMCAT, IDLG=1)
    df_categorie = (
        _normalize_cols_upper(spark.read.table(GD_DOC_CAT_TABLE))
        .filter(F.upper(F.col("PRINCIPALE")) == "OUI")
        .select("IDDOC", "IDCAT")
        .join(
            F.broadcast(
                _normalize_cols_upper(spark.read.table(GD_CAT_TABLE))
                .filter(F.col("IDLG") == 1)
                .select(F.col("IDCAT"), F.col("NOMCAT").alias("categorie"))
                .dropDuplicates(["IDCAT"])
            ),
            on="IDCAT", how="inner"
        )
        .select("IDDOC", "categorie")
        .dropDuplicates(["IDDOC"])
    )

    # Build the enriched doc reference (replaces gd_knowledge_base)
    df_doc_enriched = (
        df_doc_current
        .join(F.broadcast(df_typdoc), df_doc_current["IDTYPDOC"] == df_typdoc["IDTYPDOC"], "left")
        .drop(df_typdoc["IDTYPDOC"])
        .join(F.broadcast(df_utilisateur), df_doc_current["REDACTEUR"] == df_utilisateur["NUMUTILISATEUR"], "left")
        .drop("NUMUTILISATEUR")
        .join(F.broadcast(df_categorie), on="IDDOC", how="left")
        .select(
            F.col("IDDOC"),
            F.col("REF").alias("ref"),
            F.col("TITRE").alias("titre"),
            F.col("type_document"),
            F.col("categorie"),
            F.lit("fr-FR").alias("langue"),  # default; IDLG could refine this
            F.col("auteur"),
            # doc_date: publication date (DATEDIFF / dtdiff depending on source).
            # NULL when absent — the pipeline keeps these docs (not filtered out).
            _date_col_expr.alias("doc_date"),
        )
        .dropDuplicates(["IDDOC"])
    )

    df_business_meta = (
        df_doc_enriched
        .join(F.broadcast(df_cat_principal.drop("est_principale")), on="IDDOC", how="left")
        .join(F.broadcast(df_doc_prefixes), on="IDDOC", how="left")
    )

    df_doc_lookup = (
        df_doc_all
        .groupBy("IDDOC")
        .agg(
            F.max("COURANT").alias("_max_courant"),
            F.concat_ws(",", F.sort_array(F.collect_set(F.col("COURANT").cast("string")))).alias("_courant_values"),
            F.first("REF").alias("_doc_ref"),
            F.first("TITRE").alias("_doc_titre"),
        )
    )
    # df_kb_lookup kept for backward compatibility (same shape, derived from gd_doc)
    df_kb_lookup = df_doc_enriched.select(
        F.col("IDDOC"), F.col("ref").alias("_kb_ref"), F.col("titre").alias("_kb_titre")
    )
    return df_business_meta, df_doc_lookup, df_kb_lookup


# ===========================================================================
# Step 2 — distributed file scan (metadata only)
# ===========================================================================
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


def _build_meta_and_content(df_files_raw):
    """Shared post-processing for both scan_volume_files() and
    scan_volume_paths(): binaryFile raw listing -> (df_meta_raw, df_content)."""
    df_meta_raw = (
        df_files_raw
        .select(
            F.col("path").alias("source_path"),
            F.col("length").cast("long").alias("source_file_size_bytes"),
            F.col("modificationTime").alias("source_modification_time"),
        )
        .withColumn("source_file_name", F.element_at(F.split(F.col("source_path"), "/"), -1))
        .withColumn("source_file_extension", F.lower(F.regexp_extract(F.col("source_file_name"), r"\.([^.]+)$", 1)))
        .withColumn("source_folder_path", F.regexp_replace(F.col("source_path"), r"/[^/]+$", ""))
    )
    df_meta_raw = df_meta_raw.filter(
        ~F.lower(F.col("source_file_name")).isin([t.lower() for t in TRASH_FILES_BLACKLIST])
        & ~F.col("source_file_name").startswith("~")
    )
    df_meta_raw = df_meta_raw.withColumn("IDDOC", iddoc_column())

    df_content = df_files_raw.select(F.col("path").alias("source_path"), "content")
    return df_meta_raw, df_content


def scan_volume_files(spark, volume_root_path: str):
    """Return (df_meta_raw, df_content) read distributively via binaryFile.

    df_meta_raw : 1 row per file (path, size, mtime, ext, IDDOC) — NO content.
    df_content  : (source_path, content) for joining bytes back on selection.

    Scans the ENTIRE volume recursively — expensive on a large corpus. When
    only specific IDDOCs matter (e.g. incremental runs), prefer
    scan_volume_paths() scoped to just their folders.
    """
    df_files_raw = (
        spark.read.format("binaryFile")
        .option("recursiveFileLookup", "true")
        .load(volume_root_path)
    )
    return _build_meta_and_content(df_files_raw)


def scan_volume_paths(spark, paths: List[str]):
    """Same as scan_volume_files() but scoped to an explicit list of folder
    paths (typically one per target IDDOC), instead of the whole volume root.

    Avoids re-listing/re-scanning the entire volume (every IDDOC ever
    ingested) on every run when only a handful of IDDOCs actually need
    (re)parsing — the caller is expected to resolve target IDDOCs to their
    physical folder paths via ONE cheap non-recursive `dbutils.fs.ls` at the
    volume root (see 3_Parse_Pipeline_v2.py), then pass those paths here.

    Returns empty (but correctly-typed) DataFrames if `paths` is empty,
    since Spark's binaryFile source errors out on a zero-path load.
    """
    if not paths:
        return (
            spark.createDataFrame([], schema=_EMPTY_META_SCHEMA),
            spark.createDataFrame([], schema=_EMPTY_CONTENT_SCHEMA),
        )
    df_files_raw = (
        spark.read.format("binaryFile")
        .option("recursiveFileLookup", "true")
        .load(paths)
    )
    return _build_meta_and_content(df_files_raw)


# ===========================================================================
# Step 3 — ranking & prioritisation (all candidates kept, rank attached)
# ===========================================================================
def rank_candidates(df_meta_raw, df_business_meta=None):
    """Rank ALL files with a valid IDDOC on the volume.

    The volume is the source of truth: every physical file with a parseable IDDOC
    gets ranked. Business metadata (df_business_meta) is NOT used for filtering
    here — it is only joined later as enrichment in select_best_files().

    Returns df_matched_full with priority_rank + file count columns."""
    # Keep all files that have a valid (non-null) IDDOC — no business filter
    df_matched = df_meta_raw.filter(F.col("IDDOC").isNotNull())

    mapping_expr = F.create_map([F.lit(x) for x in chain(*FORMAT_PRIORITIES.items())])
    df_matched = (
        df_matched
        .withColumn("ext_priority", F.coalesce(mapping_expr[F.col("source_file_extension")], F.lit(99)))
        .withColumn("is_dm_file", F.when(F.col("source_file_name").rlike(r"^[dD]m_"), 1).otherwise(0))
    )

    # Final tie-break on source_path so row_number() stays stable across runs
    # on same-mtime ties.
    window = Window.partitionBy("IDDOC").orderBy(
        F.col("is_dm_file").asc(), F.col("ext_priority").asc(),
        F.col("source_modification_time").desc(), F.col("source_path").asc()
    )
    return (
        df_matched
        .withColumn("priority_rank", F.row_number().over(window))
        .withColumn("files_count_for_iddoc", F.count("*").over(Window.partitionBy("IDDOC")))
    )


def select_best_files(df_matched_full, df_business_meta, parse_filter=None):
    """Pick the best SUPPORTED file per IDDOC, enrich with business metadata, apply PARSE_FILTER.

    Re-ranks among ext_priority < 99 candidates only, rather than requiring
    priority_rank == 1 on the unfiltered ranking — an IDDOC whose overall
    rank-1 file is an unsupported format (e.g. a stray .msg/.zip) still gets
    its best supported candidate selected instead of being dropped entirely.

    parse_filter: None=all, int=random sample of N IDDOCs (seed 42), list=specific IDDOCs.
    Returns df_selected (metadata + ref/titre/hierarchy/doc_date, no content yet).
    """
    supported_rank = F.row_number().over(
        Window.partitionBy("IDDOC").orderBy(
            F.col("is_dm_file").asc(), F.col("ext_priority").asc(),
            F.col("source_modification_time").desc(), F.col("source_path").asc()
        )
    )
    df_sel = (
        df_matched_full.filter(F.col("ext_priority") < 99)
        .withColumn("_supported_rank", supported_rank)
        .filter(F.col("_supported_rank") == 1)
        .drop("_supported_rank")
    )

    if isinstance(parse_filter, (list, tuple, set)):
        df_sel = df_sel.filter(F.col("IDDOC").isin(list(parse_filter)))
    elif isinstance(parse_filter, int):
        ids = [r.IDDOC for r in df_sel.select("IDDOC").distinct().orderBy("IDDOC").limit(10_000_000).collect()]
        import random
        random.seed(42)
        sample = random.sample(ids, min(parse_filter, len(ids)))
        df_sel = df_sel.filter(F.col("IDDOC").isin(sample))

    df_sel = df_sel.drop(*_RANK_HELPER_COLS)

    # Enrich with business columns (ref, titre, hierarchy, doc_date, etc.).
    biz_cols = [c for c in ("ref", "titre", "type_document", "categorie", "langue", "auteur",
                            "document_prefixes", "division", "doc_date",
                            "niveau_plus_1", "niveau_plus_2", "niveau_plus_3",
                            "niveau_plus_4", "niveau_plus_5", "niveau_plus_6")
                if c in df_business_meta.columns]
    df_biz = df_business_meta.select("IDDOC", *biz_cols).dropDuplicates(["IDDOC"])
    df_sel = df_sel.join(F.broadcast(df_biz), on="IDDOC", how="left")

    # Exclude ORPHAN IDDOCs: folders that exist on the volume but whose IDDOC
    # cannot be resolved to any REF in gd_doc. These documents were created then deleted from Intraqual before the first
    # ingestion cycle captured them — no metadata available, parsing would fail.
    # Note: REF='-' is a placeholder used for legacy/untitled docs; keep those.
    df_sel = df_sel.filter(F.col("ref").isNotNull())

    # Guarantee the expected string columns exist even if absent from the KB.
    for c in ("ref", "titre", "type_document", "categorie", "langue", "auteur",
              "document_prefixes", "division",
              "niveau_plus_1", "niveau_plus_2", "niveau_plus_3",
              "niveau_plus_4", "niveau_plus_5", "niveau_plus_6"):
        if c not in df_sel.columns:
            df_sel = df_sel.withColumn(c, F.lit(None).cast("string"))
    # Guarantee doc_date (date type) exists
    if "doc_date" not in df_sel.columns:
        df_sel = df_sel.withColumn("doc_date", F.lit(None).cast("date"))
    return df_sel


def attach_content(df_selected, df_content, ingestion_run_id: str):
    """Join file bytes back to the selected metadata and add ingestion columns."""
    return (
        df_selected
        .join(df_content, on="source_path", how="inner")
        .withColumn("document_sha256", F.sha2(F.col("content"), 256))
        .withColumn("ingestion_run_id", F.lit(ingestion_run_id))
        .withColumn("ingestion_timestamp", F.current_timestamp())
    )


def get_retry_candidates(df_matched_full, df_content, failed_iddocs: List[int], ingestion_run_id: str):
    """For failed IDDOCs, return the next-best (rank 2) supported file with its
    content, joined from the scanned DataFrame."""
    if not failed_iddocs:
        return None
    # .ppt used to be excluded here because Docling always failed on it;
    # image_utils._fallback_parse_ppt_legacy now handles it (see
    # 3_Parse_Pipeline_v2.py), so it's a legitimate rank-2 candidate.
    df_alt = (
        df_matched_full
        .filter(F.col("IDDOC").isin(failed_iddocs))
        .filter((F.col("priority_rank") == 2) & (F.col("ext_priority") < 99))
        .drop(*_RANK_HELPER_COLS)
    )
    if df_alt.rdd.isEmpty():
        return None
    return (
        df_alt
        .join(df_content, on="source_path", how="inner")
        .withColumn("document_sha256", F.sha2(F.col("content"), 256))
        .withColumn("ingestion_run_id", F.lit(ingestion_run_id))
        .withColumn("ingestion_timestamp", F.current_timestamp())
    )


# ===========================================================================
# Step 4 — unified diagnostic audit (optional, business logic from Explore_Chunking)
# ===========================================================================
def build_unified_audit(spark, df_meta_raw, df_business_meta, df_matched_full,
                        df_doc_lookup, df_kb_lookup, ingestion_run_id: str,
                        target_table: Optional[str] = None):
    """Build (and optionally write) the unified diagnostic audit table:
    MISSING_FILE / ORPHANED_FILE / EDGE_CASE / VALID_FILE / DEPRIORITIZED_FILE.

    Returns the audit DataFrame. This is pure diagnostics — it does not affect
    which documents are parsed."""
    file_iddocs = F.broadcast(df_meta_raw.select("IDDOC").distinct())
    business_iddocs = F.broadcast(df_business_meta.select("IDDOC").distinct())

    # Selected file per IDDOC (for deprioritisation reasons).
    df_selected_info = (
        df_matched_full.filter(F.col("priority_rank") == 1)
        .select(
            "IDDOC",
            F.col("source_file_name").alias("selected_file_for_iddoc"),
            F.col("source_file_extension").alias("_sel_ext"),
            F.col("ext_priority").alias("_sel_ext_priority"),
            F.col("is_dm_file").alias("_sel_is_dm"),
        )
    )
    df_full = (
        df_matched_full.join(F.broadcast(df_selected_info), on="IDDOC", how="left")
        .withColumn(
            "depriority_reason",
            F.when(F.col("priority_rank") == 1, F.lit(None).cast("string"))
            .when(F.col("ext_priority") == 99,
                  F.concat(F.lit("UNSUPPORTED_FORMAT: '"), F.col("source_file_extension"), F.lit("'")))
            .when((F.col("is_dm_file") == 1) & (F.col("_sel_is_dm") == 0),
                  F.lit("PREFIX_LOWER_PRIORITY: Dm_ deprioritised vs D_"))
            .when(F.col("ext_priority") > F.col("_sel_ext_priority"),
                  F.concat(F.lit("FORMAT_LOWER_PRIORITY: "), F.col("source_file_extension"),
                           F.lit(" vs "), F.col("_sel_ext")))
            .otherwise(F.lit("OLDER_FILE: same prefix & format but older mtime")))
        .drop("_sel_ext", "_sel_ext_priority", "_sel_is_dm")
    )

    common = [
        "IDDOC", "audit_type", "root_cause", "diagnostic_message",
        "iddoc_in_business_db", "file_exists_on_disk",
        "priority_rank", "files_count_for_iddoc", "selected_file_for_iddoc", "depriority_reason",
        "source_path", "source_file_name", "source_file_extension", "source_folder_path",
        "source_file_size_bytes", "source_modification_time",
        "ref", "titre", "document_prefixes",
    ]

    def _pad(df):
        """Ensure all `common` columns exist on df (fill missing with NULL)."""
        for c in common:
            if c not in df.columns:
                df = df.withColumn(c, F.lit(None).cast("string"))
        return df.select(common)

    # A: business doc without a physical file.
    df_missing = (
        df_business_meta.join(file_iddocs, on="IDDOC", how="left_anti")
        .withColumn("audit_type", F.lit("MISSING_FILE"))
        .withColumn("root_cause", F.lit("FILE_ABSENT_FROM_STORAGE"))
        .withColumn("iddoc_in_business_db", F.lit(True))
        .withColumn("file_exists_on_disk", F.lit(False))
        .withColumn("diagnostic_message",
                    F.concat(F.lit("IDDOC="), F.col("IDDOC"),
                             F.lit(" in knowledge base but no physical file found.")))
    )

    # B: physical file without a business record (enriched root-cause).
    df_orphan = (
        df_meta_raw.join(business_iddocs, on="IDDOC", how="left_anti")
        .join(F.broadcast(df_doc_lookup), on="IDDOC", how="left")
        .join(F.broadcast(df_kb_lookup), on="IDDOC", how="left")
        .withColumn("audit_type", F.lit("ORPHANED_FILE"))
        .withColumn("ref", F.coalesce(F.col("_kb_ref"), F.col("_doc_ref")))
        .withColumn("titre", F.coalesce(F.col("_kb_titre"), F.col("_doc_titre")))
        .withColumn("iddoc_in_business_db", F.lit(False))
        .withColumn("file_exists_on_disk", F.lit(True))
        .withColumn(
            "root_cause",
            F.when(F.col("IDDOC").isNull(), F.lit("NON_CONFORMING_NAME_NO_IDDOC"))
            .when(F.col("_max_courant").isNull(), F.lit("IDDOC_NOT_IN_GD_DOC"))
            .when(F.col("_max_courant") < 1, F.lit("ARCHIVED_DOCUMENT_COURANT_0"))
            .when((F.col("_max_courant") >= 1) & F.col("_kb_ref").isNull(),
                  F.lit("IN_GD_DOC_BUT_NOT_IN_KNOWLEDGE_BASE"))
            .otherwise(F.lit("IDDOC_NOT_IN_BUSINESS_DB")))
        .withColumn("diagnostic_message",
                    F.concat(F.lit("File '"), F.col("source_file_name"), F.lit("' orphaned. gd_doc courant="),
                             F.coalesce(F.col("_courant_values"), F.lit("?"))))
        .drop("_max_courant", "_courant_values", "_doc_ref", "_doc_titre", "_kb_ref", "_kb_titre")
    )

    # C/D: rank-1 (edge case if non-standard name, else valid).
    df_rank1 = df_full.filter(F.col("priority_rank") == 1)
    df_edge = (
        df_rank1.filter(~F.col("source_file_name").rlike(r"^[dD]m?_"))
        .withColumn("audit_type", F.lit("EDGE_CASE"))
        .withColumn("root_cause", F.lit("NON_STANDARD_NAME"))
        .withColumn("iddoc_in_business_db", F.lit(True))
        .withColumn("file_exists_on_disk", F.lit(True))
        .withColumn("diagnostic_message",
                    F.concat(F.lit("Selected '"), F.col("source_file_name"),
                             F.lit("' for IDDOC="), F.col("IDDOC"), F.lit(" but name is non-standard.")))
    )
    df_valid = (
        df_rank1.filter(F.col("source_file_name").rlike(r"^[dD]m?_"))
        .withColumn("audit_type", F.lit("VALID_FILE"))
        .withColumn("root_cause", F.lit("NONE"))
        .withColumn("iddoc_in_business_db", F.lit(True))
        .withColumn("file_exists_on_disk", F.lit(True))
        .withColumn("diagnostic_message",
                    F.concat(F.lit("Selected '"), F.col("source_file_name"),
                             F.lit("' for IDDOC="), F.col("IDDOC"), F.lit(". No action required.")))
    )

    # E: deprioritised (rank > 1).
    df_depri = (
        df_full.filter(F.col("priority_rank") > 1)
        .withColumn("audit_type", F.lit("DEPRIORITIZED_FILE"))
        .withColumn("root_cause",
                    F.when(F.col("ext_priority") == 99, F.lit("UNSUPPORTED_FORMAT"))
                    .otherwise(F.lit("LOWER_PRIORITY")))
        .withColumn("iddoc_in_business_db", F.lit(True))
        .withColumn("file_exists_on_disk", F.lit(True))
        .withColumn("diagnostic_message",
                    F.concat(F.lit("Rank "), F.col("priority_rank"), F.lit(" for IDDOC="),
                             F.col("IDDOC"), F.lit(". "), F.coalesce(F.col("depriority_reason"), F.lit(""))))
    )

    df_audit = (
        _pad(df_missing)
        .unionByName(_pad(df_orphan))
        .unionByName(_pad(df_edge))
        .unionByName(_pad(df_valid))
        .unionByName(_pad(df_depri))
        .withColumn("audit_timestamp", F.current_timestamp())
        .withColumn("ingestion_run_id", F.lit(ingestion_run_id))
    )

    if target_table:
        (df_audit.write.format("delta").mode("overwrite")
         .option("overwriteSchema", "true").saveAsTable(target_table))
    return df_audit
