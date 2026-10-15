"""tables.py — the one place where the parsing code saves or merges a Delta table: the work is done by leap_utils.

`leap_utils.config` starts a Spark session when it is imported, so leap_utils is imported inside the functions: the
modules that only need the helpers, and the unit tests, do not start Spark. The source of a merge carries the columns
of the target: `align_to_table` fills the columns the source lacks with NULL (the merge_into of leap_utils requires the same
column sets), `add_missing_columns` adds the columns the source has and the table lacks.
"""

from typing import Sequence


def write_table(df, table: str, mode: str = "append", *, merge_schema: bool = False,
                overwrite_schema: bool = False) -> None:
    """Save `df` as the Delta table `table` (leap_utils save_table).

    mode: "append", "overwrite" or "errorifexists". merge_schema: add the columns `df` has and the table lacks (append).
    overwrite_schema: replace the table schema (overwrite).
    """
    from leap_utils.data_asset import table_utils

    table_utils.save_table(table, df, mode, overwrite_schema=overwrite_schema, merge_schema=merge_schema)


def add_missing_columns(df, table: str) -> None:
    """Add to the existing Delta table `table` the columns of `df` it lacks; nothing when it has them all.

    An empty DataFrame saved with schema merge: the only write that adds a column to a table that exists.
    """
    from leap_utils.data_asset import table_utils

    existing = set(df.sparkSession.table(table).columns)
    missing = [c for c in df.columns if c not in existing]
    if missing:
        table_utils.save_table(table, df.select(*missing).limit(0), "append", overwrite_schema=False,
                               merge_schema=True)


def table_exists(table: str) -> bool:
    """True when `table` (catalog.schema.name) exists as a table, not a view (leap_utils)."""
    from leap_utils.data_asset import table_utils

    return bool(table_utils.UnityCatalogCheckTableExists(table))


def align_to_table(df, table: str):
    """`df` with the columns of `table`, in its order: NULL (cast to the table's type) where `df` has none."""
    from pyspark.sql import functions as F

    target = df.sparkSession.table(table)
    return df.select(*[
        (F.col(f.name) if f.name in df.columns else F.lit(None)).try_cast(f.dataType).alias(f.name)
        for f in target.schema
    ])


def merge_into(df, table: str, keys, *, do_when_matched: bool = True, matched_clause=None,
               do_when_notmatched: bool = True, do_when_notmatchedbysource: bool = False) -> None:
    """MERGE `df` into `table` on `keys` (leap_utils merge_into), the source aligned on the table's columns.

    `df` must be a source that does not read `table` itself: a derived source is saved to a temporary table first.
    """
    from leap_utils.data_asset import table_utils

    table_utils.merge_into(
        align_to_table(df, table), table, list(keys),
        do_when_matched=do_when_matched, matched_clause=matched_clause,
        do_when_notmatched=do_when_notmatched, do_when_notmatchedbysource=do_when_notmatchedbysource,
    )


def ensure_primary_key(table: str, columns: Sequence[str]) -> None:
    """Declare `columns` the primary key of the Delta table `table`, when it has none.

    Each column is set NOT NULL first, as the constraint requires. An overwrite with overwriteSchema drops the key:
    call this after every overwrite.
    """
    from pyspark.sql import SparkSession
    from leap_utils.data_asset import table_utils

    catalog, schema, name = table.split(".")
    declared = SparkSession.getActiveSession().sql(
        f"SELECT count(*) AS n FROM {catalog}.information_schema.table_constraints "
        f"WHERE table_schema = '{schema}' AND table_name = '{name}' AND constraint_type = 'PRIMARY KEY'"
    ).first()["n"]
    if declared:
        return
    for column in columns:
        table_utils.set_table_column_not_null(table_path=table, column=column)
    table_utils.add_table_primary_key(table_path=table, pk_name=f"proj_{name}_PK", columns=",".join(columns))
