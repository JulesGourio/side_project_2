"""categories.py — category hierarchy, division reference and Qualibot scope of the Intraqual documents.

Driver side, returns Spark DataFrames. Reads the revisions table of the Intraqual data asset
(`document_revisions_intraqual`), which already carries each revision's main category, its root and the ordered
category ids of its path, and rebuilds the category display of the Intraqual application from them. Used by
`1_categories` (category_reference), `2_manifest` (scope gate) and indirectly by `3_parse` through the
category_reference table.
"""

from pyspark.sql import DataFrame
from pyspark.sql import Window
from pyspark.sql import functions as f

from config import (
    DATE_EXEMPT_ROOT_IDCATS,
    DIVISION_ARCHIVE_TABLE,
    DIVISION_REFERENCE_TABLE,
    DOC_DATE_CUTOFF,
    DOC_SCOPE_FILTER,
    INTRAQUAL_CATEGORY_ANCESTORS_TABLE,
    INTRAQUAL_REVISIONS_TABLE,
)

IDCAT_PROCESSES = 12  # "1 - PROCESSES / PROCESSUS" → belongs to AS
IDCAT_AS = 3346  # "AS - Aerostructures"
IDCAT_IS = 3284  # "IS - Interconnection Systems"
IDCAT_DIRECTIVE = 962  # "0 - DIRECTIVE GROUPE LATECOERE"
MAX_HIERARCHY_DEPTH = 7


# Category columns of the revisions table, carried over together from the same earlier revision.
_CATEGORY_COLUMNS = [
    "category_ID",
    "category_name",
    "category_path",
    "category_ID_path",
    "root_category_ID",
    "root_category_name",
    "is_leaf_category",
]


def inherit_missing_category(df: DataFrame) -> DataFrame:
    """Give a revision without category the one of the closest earlier revision of the same document that had one.

    A revision that has a category keeps it, even if the scope filters it out: the history is never searched for a
    better one. ``category_inherited`` flags the revisions whose category comes from an earlier revision.

    :param df: Revisions table, one row per ``document_revision_ID``.
    :return: ``df`` with the category columns filled where ``root_category_ID`` was null and an earlier revision had one.
    """
    columns = [c for c in _CATEGORY_COLUMNS if c in df.columns]
    earlier_revisions = (
        Window.partitionBy("document_reference")
        .orderBy(f.col("document_revision_ID").asc())
        .rowsBetween(Window.unboundedPreceding, -1)
    )
    last_categorised = f.last(
        f.when(f.col("root_category_ID").isNotNull(), f.struct(*columns)), ignorenulls=True
    ).over(earlier_revisions)
    # The flag is a column of its own: the loop below overwrites root_category_ID, which the condition reads.
    df = df.withColumn("_earlier_category", last_categorised).withColumn(
        "category_inherited", f.col("root_category_ID").isNull() & f.col("_earlier_category").isNotNull()
    )
    for column in columns:
        df = df.withColumn(
            column,
            f.when(f.col("category_inherited"), f.col(f"_earlier_category.{column}")).otherwise(f.col(column)),
        )
    return df.drop("_earlier_category")


def read_revisions(spark) -> DataFrame:
    """Every revision of every Intraqual document, one row per ``document_revision_ID`` (the IDDOC).

    A revision without category takes the one of its closest earlier revision that had one (`inherit_missing_category`).

    :param spark: Active Spark session.
    :return: the revisions table of the Intraqual data asset, with its own column names.
    """
    return inherit_missing_category(spark.read.table(INTRAQUAL_REVISIONS_TABLE))


def build_category_levels(spark, max_depth: int = MAX_HIERARCHY_DEPTH) -> DataFrame:
    """Name of the main category's ancestor at each level, one row per revision that has a main category.

    :param spark: Active Spark session.
    :param max_depth: Number of levels kept, from the root (level 1) down.
    :return: DataFrame of ``IDDOC``, ``root_idcat``, ``leaf_nomcat`` (name of the category itself) and ``raw_niv1`` ..
        ``raw_niv{max_depth}``; a level below the category's own depth is null.
    """
    df_path = (
        read_revisions(spark)
        .filter(f.col("category_ID").isNotNull() & f.col("category_ID_path").isNotNull())
        .select(
            f.col("document_revision_ID").alias("IDDOC"),
            f.col("root_category_ID").alias("root_idcat"),
            f.posexplode("category_ID_path").alias("pos", "idcat"),
        )
    )
    df_names = (
        spark.read.table(INTRAQUAL_CATEGORY_ANCESTORS_TABLE)
        .select(
            f.col("ancestor_category_ID").alias("idcat"),
            f.col("ancestor_category_name").alias("nomcat"),
        )
        .dropDuplicates(["idcat"])
    )
    return (
        df_path.join(f.broadcast(df_names), on="idcat", how="left")
        .groupBy("IDDOC", "root_idcat")
        .agg(
            f.max_by("nomcat", "pos").alias("leaf_nomcat"),
            *[
                f.max(f.when(f.col("pos") == niv - 1, f.col("nomcat"))).alias(f"raw_niv{niv}")
                for niv in range(1, max_depth + 1)
            ],
        )
    )


def build_category_hierarchy(spark, max_depth: int = MAX_HIERARCHY_DEPTH) -> DataFrame:
    """Per-IDDOC division and displayed category levels, as the Intraqual application shows them.

    :param spark: Active Spark session.
    :param max_depth: Number of levels walked; the last output level is ``niveau_plus_{max_depth-1}``.
    :return: DataFrame of ``IDDOC``, ``division`` (``AS``, ``IS``, ``DIRECTIVE`` or ``GROUP``: shared by the divisions),
        ``niveau_plus_1`` .. ``niveau_plus_{max_depth-1}`` and ``date_exempt`` (true when the document is indexed
        whatever its date).
    """
    df = build_category_levels(spark, max_depth)

    df = df.withColumn(
        "division",
        # PROCESSES has no "AS -" prefix in its raw name despite belonging to division AS.
        f.when(f.col("root_idcat") == IDCAT_PROCESSES, f.lit("AS"))
        .when(f.col("raw_niv1").startswith("AS -"), f.lit("AS"))
        .when(f.col("raw_niv1").startswith("IS -"), f.lit("IS"))
        .when(f.col("root_idcat") == IDCAT_DIRECTIVE, f.lit("DIRECTIVE"))
        # Outside those roots, a leaf category named after the division marks the document.
        .when(f.upper(f.trim(f.col("leaf_nomcat"))) == "AS", f.lit("AS"))
        .when(f.upper(f.trim(f.col("leaf_nomcat"))) == "IS", f.lit("IS"))
        .otherwise(f.lit("GROUP")),
    )

    df = df.withColumn(
        "niveau_plus_1",
        f.when(f.col("root_idcat") == IDCAT_PROCESSES, f.lit("AS - PROCESSES / PROCESSUS"))
        .when(
            f.col("root_idcat") == IDCAT_AS,
            f.concat(f.lit("AS - "), f.regexp_replace(f.col("raw_niv2"), r"^\d+\s*-\s*", "")),
        )
        .when(f.col("root_idcat") == IDCAT_IS, f.lit("IS - Interconnection Systems"))
        .otherwise(f.col("raw_niv1")),
    )

    # Under the AS root the displayed levels are shifted by one: the "AS - Aerostructures" level is folded into niveau_plus_1.
    for output_niv in range(2, max_depth):
        df = df.withColumn(
            f"niveau_plus_{output_niv}",
            f.when(f.col("root_idcat") == IDCAT_AS, f.col(f"raw_niv{output_niv + 1}")).otherwise(
                f.col(f"raw_niv{output_niv}")
            ),
        )

    df = df.withColumn("date_exempt", f.col("root_idcat").isin(*sorted(DATE_EXEMPT_ROOT_IDCATS)))
    return df.select(
        "IDDOC", "division", *[f"niveau_plus_{niv}" for niv in range(1, max_depth)], "date_exempt"
    )


def build_division_reference(spark, max_depth: int = MAX_HIERARCHY_DEPTH) -> DataFrame:
    """One row per IDDOC: division + niveau_plus_1..N.

    Primary: build_category_hierarchy(). Fallback: DIVISION_ARCHIVE_TABLE fills
    gaps only, never overrides a live-resolved row; empty/unset = disabled (default).

    :param spark: Active Spark session.
    :param max_depth: Number of category levels, as in ``build_category_hierarchy``.
    :return: one row per IDDOC with its division, ``niveau_plus_*``, ``date_exempt`` and ``division_source``.
    """
    niveau_cols = [f"niveau_plus_{niv}" for niv in range(1, max_depth)]

    df_live = (
        build_category_hierarchy(spark, max_depth=max_depth)
        .dropDuplicates(["IDDOC"])
        .select("IDDOC", "division", *niveau_cols, "date_exempt")
        .withColumn("division_source", f.lit("live_hierarchy"))
    )
    if not (DIVISION_ARCHIVE_TABLE or "").strip():
        return df_live

    df_archive = (
        spark.table(DIVISION_ARCHIVE_TABLE)
        .filter(f.col("division").isNotNull())
        .select("IDDOC", "division", *niveau_cols)
        .dropDuplicates(["IDDOC"])
        .join(df_live.select("IDDOC"), on="IDDOC", how="left_anti")
        .withColumn("date_exempt", f.lit(False))
        .withColumn("division_source", f.lit("archive_fallback"))
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
        .filter(f.expr(DOC_SCOPE_FILTER))
        .filter(f.col("document_revision_ID").isNotNull())
        .select(
            f.col("document_revision_ID").alias("IDDOC"),
            f.col("document_reference").alias("ref"),
            f.col("document_title").alias("titre"),
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
            row = (
                spark.sql(f"DESCRIBE DETAIL {table}")
                .select(
                    f.round(
                        (f.unix_timestamp(f.current_timestamp()) - f.unix_timestamp("lastModified"))
                        / 3600.0,
                        1,
                    ).alias("age")
                )
                .first()
            )
            ages[table] = row["age"] if row else None
        except Exception:
            ages[table] = None
    return ages


def with_archive_flag(spark, df: DataFrame) -> DataFrame:
    """Add ``is_archive``: published before DOC_DATE_CUTOFF and not under a date-exempt root category.

    :param spark: Active Spark session.
    :param df: Rows with ``IDDOC`` and ``doc_date`` (null = recent).
    :return: the same rows with a boolean ``is_archive`` column.
    """
    df_exempt = spark.table(DIVISION_REFERENCE_TABLE).select(
        "IDDOC", f.col("date_exempt").alias("_date_exempt")
    )
    before_cutoff = f.col("doc_date").isNotNull() & (
        f.col("doc_date") < f.lit(DOC_DATE_CUTOFF).try_cast("date")
    )
    return (
        df.join(f.broadcast(df_exempt), on="IDDOC", how="left")
        .withColumn(
            "is_archive", before_cutoff & ~f.coalesce(f.col("_date_exempt"), f.lit(False))
        )
        .drop("_date_exempt")
    )
