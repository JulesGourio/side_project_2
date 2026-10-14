"""categories.py — category hierarchy, division reference and Qualibot scope of the Intraqual documents.

Driver side, returns Spark DataFrames. Reproduces the category display of the Intraqual application by walking up
`gd_cat.IDPERE` from each document's categories (`gd_doc_cat_latest`). Used by `1_categories` (category_reference),
`2_manifest` (scope gate) and indirectly by `3_parse` through the category_reference table.
"""

from pyspark.sql import functions as F

from config import (
    DIVISION_ARCHIVE_TABLE,
    DOC_SCOPE_FILTER,
    GD_CAT_LATEST,
    GD_DOC_CAT_LATEST,
    GD_DOC_LATEST,
)

IDCAT_PROCESSES = 12       # "1 - PROCESSES / PROCESSUS" → belongs to AS
IDCAT_AS = 3346            # "AS - Aerostructures"
IDCAT_IS = 3284            # "IS - Interconnection Systems"
IDCAT_DIRECTIVE = 962      # "0 - DIRECTIVE GROUPE LATECOERE"
MAX_HIERARCHY_DEPTH = 7    # Levels 1 to 7 in gd_cat


def normalize_cols_upper(df):
    """Rename all DataFrame columns to UPPERCASE: the *_latest materialized views sometimes expose lowercase columns
    (gd_doc_cat_latest: iddoc, idcat, principale) while the code expects the original gd_doc* schema."""
    for c in df.columns:
        if c != c.upper():
            df = df.withColumnRenamed(c, c.upper())
    return df


# --- Category hierarchy builder (dynamic, from gd_cat + gd_doc_cat) ---
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

    df_doc_cat = normalize_cols_upper(spark.read.table(GD_DOC_CAT_LATEST))
    df_cat = normalize_cols_upper(spark.read.table(GD_CAT_LATEST)).filter(F.col("IDLG") == idlg)

    df_doc_cat = df_doc_cat.withColumn(
        "est_principale",
        F.when(F.lower(F.col("PRINCIPALE")) == "oui", True).otherwise(False)
    )

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

    hierarchy_cols = ["IDDOC", "est_principale", "division"] + \
                     [f"niveau_plus_{niv}" for niv in range(1, max_depth)]
    raw_cols = [f"raw_niv{niv}" for niv in range(1, max_depth + 1)]

    return df_hierarchy.select(*hierarchy_cols).drop(*[c for c in raw_cols if c in df_hierarchy.columns])


def build_division_reference(spark, max_depth: int = MAX_HIERARCHY_DEPTH):
    """One row per IDDOC: division + niveau_plus_1..N.

    Primary: build_category_hierarchy(). Fallback: DIVISION_ARCHIVE_TABLE fills
    gaps only, never overrides a live-resolved row; empty/unset = disabled (default).
    """
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


# --- Step 0 — Qualibot perimeter (scope gate) ---
def load_scope_docs(spark):
    """Return (IDDOC, ref, titre) for every document inside the Qualibot scope (DOC_SCOPE_FILTER, config.py)."""
    return (
        normalize_cols_upper(spark.read.table(GD_DOC_LATEST))
        .filter(F.expr(DOC_SCOPE_FILTER))
        .filter(F.col("IDDOC").isNotNull())
        .select(
            F.col("IDDOC"),
            F.col("REF").alias("ref"),
            F.col("TITRE").alias("titre"),
        )
        .dropDuplicates(["IDDOC"])
    )
