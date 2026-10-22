"""categories.py — category hierarchy, division reference and Qualibot scope of the Intraqual documents.

Driver side, returns Spark DataFrames. Reads the revisions table of the Intraqual data asset
(`document_revisions_intraqual`), which already carries each revision's main category, its root and the ordered
category ids of its path, and rebuilds the category display of the Intraqual application from them. Used by
`1_categories` (category_reference), `2_manifest` (scope gate) and indirectly by `3_parse` through the
category_reference table.
"""

from pyspark.sql import DataFrame
from pyspark.sql import functions as F

from config import (
    DIVISION_ARCHIVE_TABLE,
    DOC_SCOPE_FILTER,
    INTRAQUAL_CATEGORY_ANCESTORS_TABLE,
    INTRAQUAL_REVISIONS_TABLE,
)

IDCAT_PROCESSES = 12       # "1 - PROCESSES / PROCESSUS" → belongs to AS
IDCAT_AS = 3346            # "AS - Aerostructures"
IDCAT_IS = 3284            # "IS - Interconnection Systems"
IDCAT_DIRECTIVE = 962      # "0 - DIRECTIVE GROUPE LATECOERE"
IDCAT_PROCESS_TREE = 3870  # "00-LATECOERE PROCESSES_One QMS": processes shared by the divisions
MAX_HIERARCHY_DEPTH = 7




def read_revisions(spark) -> DataFrame:
    """Every revision of every Intraqual document, one row per ``document_revision_ID`` (the IDDOC).

    :param spark: Active Spark session.
    :return: the revisions table of the Intraqual data asset, with its own column names.
    """
    return spark.read.table(INTRAQUAL_REVISIONS_TABLE)


def build_category_levels(spark, max_depth: int = MAX_HIERARCHY_DEPTH) -> DataFrame:
    """Name of the main category's ancestor at each level, one row per revision that has a main category.

    :param spark: Active Spark session.
    :param max_depth: Number of levels kept, from the root (level 1) down.
    :return: DataFrame of ``IDDOC``, ``root_idcat``, ``leaf_nomcat`` (name of the category itself) and ``raw_niv1`` ..
        ``raw_niv{max_depth}``; a level below the category's own depth is null.
    """
    df_path = (
        read_revisions(spark)
        .filter(F.col("category_ID").isNotNull() & F.col("category_ID_path").isNotNull())
        .select(
            F.col("document_revision_ID").alias("IDDOC"),
            F.col("root_category_ID").alias("root_idcat"),
            F.posexplode("category_ID_path").alias("pos", "idcat"),
        )
    )
    df_names = (
        spark.read.table(INTRAQUAL_CATEGORY_ANCESTORS_TABLE)
        .select(F.col("ancestor_category_ID").alias("idcat"), F.col("ancestor_category_name").alias("nomcat"))
        .dropDuplicates(["idcat"])
    )
    return (
        df_path.join(F.broadcast(df_names), on="idcat", how="left")
        .groupBy("IDDOC", "root_idcat")
        .agg(
            F.max_by("nomcat", "pos").alias("leaf_nomcat"),
            *[F.max(F.when(F.col("pos") == niv - 1, F.col("nomcat"))).alias(f"raw_niv{niv}")
              for niv in range(1, max_depth + 1)],
        )
    )


def build_category_hierarchy(spark, max_depth: int = MAX_HIERARCHY_DEPTH) -> DataFrame:
    """Per-IDDOC division and displayed category levels, as the Intraqual application shows them.

    :param spark: Active Spark session.
    :param max_depth: Number of levels walked; the last output level is ``niveau_plus_{max_depth-1}``.
    :return: DataFrame of ``IDDOC``, ``division`` and ``niveau_plus_1`` .. ``niveau_plus_{max_depth-1}``.
    """
    df = build_category_levels(spark, max_depth)

    df = df.withColumn(
        "division",
        # PROCESSES has no "AS -" prefix in its raw name despite belonging to division AS.
        F.when(F.col("root_idcat") == IDCAT_PROCESSES, F.lit("AS"))
         .when(F.col("raw_niv1").startswith("AS -"), F.lit("AS"))
         .when(F.col("raw_niv1").startswith("IS -"), F.lit("IS"))
         .when(F.col("root_idcat") == IDCAT_DIRECTIVE, F.lit("DIRECTIVE"))
         # Under the process tree a leaf category named after the division marks the document.
         .when(F.upper(F.trim(F.col("leaf_nomcat"))) == "AS", F.lit("AS"))
         .when(F.upper(F.trim(F.col("leaf_nomcat"))) == "IS", F.lit("IS"))
         .when(F.col("root_idcat") == IDCAT_PROCESS_TREE, F.lit("COMMUN"))
         .otherwise(F.lit("AUTRE"))
    )

    df = df.withColumn(
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
        ).otherwise(F.col("raw_niv1"))
    )

    # Under the AS root the displayed levels are shifted by one: the "AS - Aerostructures" level is folded into niveau_plus_1.
    for output_niv in range(2, max_depth):
        df = df.withColumn(
            f"niveau_plus_{output_niv}",
            F.when(
                F.col("root_idcat") == IDCAT_AS,
                F.col(f"raw_niv{output_niv + 1}")
            ).otherwise(
                F.col(f"raw_niv{output_niv}")
            )
        )

    return df.select("IDDOC", "division", *[f"niveau_plus_{niv}" for niv in range(1, max_depth)])


def build_division_reference(spark, max_depth: int = MAX_HIERARCHY_DEPTH) -> DataFrame:
    """One row per IDDOC: division + niveau_plus_1..N.

    Primary: build_category_hierarchy(). Fallback: DIVISION_ARCHIVE_TABLE fills
    gaps only, never overrides a live-resolved row; empty/unset = disabled (default).

    :param spark: Active Spark session.
    :param max_depth: Number of category levels, as in ``build_category_hierarchy``.
    :return: one row per IDDOC with its division and ``niveau_plus_*`` columns.
    """
    niveau_cols = [f"niveau_plus_{niv}" for niv in range(1, max_depth)]

    df_live = (
        build_category_hierarchy(spark, max_depth=max_depth)
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
def load_scope_docs(spark) -> DataFrame:
    """Return (IDDOC, ref, titre) for every document inside the Qualibot scope (DOC_SCOPE_FILTER, config.py).

    :param spark: Active Spark session.
    :return: DataFrame of IDDOC, ref and titre, one row per document in scope.
    """
    return (
        read_revisions(spark)
        .filter(F.expr(DOC_SCOPE_FILTER))
        .filter(F.col("document_revision_ID").isNotNull())
        .select(
            F.col("document_revision_ID").alias("IDDOC"),
            F.col("document_reference").alias("ref"),
            F.col("document_title").alias("titre"),
        )
        .dropDuplicates(["IDDOC"])
    )


def source_ages_hours(spark, tables) -> dict[str, float | None]:
    """Hours since each table was last written.

    :param spark: Active Spark session.
    :param tables: Full names of the tables to check.
    :return: table name to its age in hours; ``None`` when the table is missing or has no write time.
    """
    ages = {}
    for table in tables:
        try:
            row = spark.sql(f"DESCRIBE DETAIL {table}").select(
                F.round((F.unix_timestamp(F.current_timestamp()) - F.unix_timestamp("lastModified")) / 3600.0, 1).alias("age")
            ).first()
            ages[table] = row["age"] if row else None
        except Exception:
            ages[table] = None
    return ages
