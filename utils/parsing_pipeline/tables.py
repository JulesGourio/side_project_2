"""tables.py — the one place that writes a DataFrame to a Delta table."""


def write_table(df, table: str, mode: str = "append", *, merge_schema: bool = False,
                overwrite_schema: bool = False) -> None:
    """Write `df` as the Delta table `table` with `saveAsTable`.

    mode: "append", "overwrite", or "errorifexists" (fails when the table already exists).
    merge_schema: add the columns `df` has and the table lacks (append). overwrite_schema: replace the table schema
    (overwrite).
    """
    writer = df.write.format("delta").mode(mode)
    if merge_schema:
        writer = writer.option("mergeSchema", "true")
    if overwrite_schema:
        writer = writer.option("overwriteSchema", "true")
    writer.saveAsTable(table)


def add_missing_columns(df, table: str) -> None:
    """Add to the existing Delta table `table` the columns of `df` it lacks; nothing when it has them all.

    The only write that adds a column to a table that exists: an append of no rows with schema merge.
    """
    existing = set(df.sparkSession.table(table).columns)
    missing = [c for c in df.columns if c not in existing]
    if missing:
        write_table(df.select(*missing).limit(0), table, "append", merge_schema=True)
