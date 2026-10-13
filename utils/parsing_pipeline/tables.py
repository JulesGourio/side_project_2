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
