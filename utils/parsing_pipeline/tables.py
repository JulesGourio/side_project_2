"""tables.py: the Delta table layer of the parsing pipeline, the writes and the quality checks of the tables.

Writes: the work is done by leap_utils. `leap_utils.config` starts a Spark session when it is imported, so leap_utils is
imported inside the functions: the modules that only need the helpers, and the unit tests, do not start Spark.
The source of a merge carries the columns of the target: `align_to_table` fills the columns the source lacks with NULL
(the merge_into of leap_utils requires the same column sets), `add_missing_columns` adds the columns the source has and
the table lacks.

Quality checks, two levels: RED (the task stops, nothing is written) and AMBER (a warning is logged, the task goes on).

- `check_dataframe` runs the checks with Great Expectations, through `leap_utils` (`gx_validation`). It is called in the
  grouped `# Quality Checks` section of a notebook, on the DataFrame that is written. `require_gx` imports GX in the
  first cells of the notebook, so a cluster without it fails before any work.
- `check_rows` runs the same checks over Python rows, for the serverless task that has no Spark cluster (no GX there).

    tables.check_dataframe(df_chunks, "chunks", [
        tables.not_null("chunk_id"), tables.unique("chunk_id"),
        tables.not_null("division", mostly=0.99, severity=tables.AMBER),
    ], logger)
"""

import logging
from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from leap_utils.common.logger import get_logger

_LOGGER = get_logger(__name__)


# --- Writes ---
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

    An `ALTER TABLE ... ADD COLUMNS`, not an empty write: a write that lacks a NOT NULL column (a primary key) is
    refused by Delta even with no row.
    """
    existing = set(df.sparkSession.table(table).columns)
    missing = [f for f in df.schema.fields if f.name not in existing]
    if missing:
        columns = ", ".join(f"`{f.name}` {f.dataType.simpleString()}" for f in missing)
        df.sparkSession.sql(f"ALTER TABLE {table} ADD COLUMNS ({columns})")


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

    source = align_to_table(df, table)
    if do_when_matched and not do_when_notmatched and not do_when_notmatchedbysource and not matched_clause:
        # Update only. leap_utils refuses a MERGE whose only clause is a plain WHEN MATCHED; with the source reduced to
        # the keys the table already has, the INSERT clause it accepts never fires and the result is the same.
        source = source.join(df.sparkSession.table(table).select(*keys), on=list(keys), how="left_semi")
        do_when_notmatched = True
    table_utils.merge_into(
        source, table, list(keys),
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


# --- Quality checks ---
RED = "RED"
AMBER = "AMBER"

ROWS = "rows"


class QualityCheckError(RuntimeError):
    """At least one RED check failed."""


@dataclass(frozen=True)
class Check:
    kind: str                       # "not_null" | "unique" | "non_empty"
    columns: Tuple[str, ...] = ()
    severity: str = RED
    mostly: float = 1.0             # not_null: minimum share of non-null rows

    @property
    def label(self) -> str:
        return f"{self.kind}({', '.join(self.columns)})" if self.columns else self.kind


@dataclass(frozen=True)
class Result:
    check: Check
    passed: bool
    detail: str


def not_null(column: str, *, mostly: float = 1.0, severity: str = RED) -> Check:
    """No null in `column`, or at least `mostly` (0-1) of the rows non-null."""
    return Check("not_null", (column,), severity, mostly)


def unique(*columns: str, severity: str = RED) -> Check:
    """No duplicate among the rows where all `columns` are non-null (single or compound key)."""
    return Check("unique", tuple(columns), severity)


def non_empty(severity: str = RED) -> Check:
    """At least one row."""
    return Check("non_empty", (), severity)


# --- Counters (what a table has to be asked) ---
def _key(prefix: str, columns: Sequence[str]) -> str:
    return f"{prefix}:{','.join(columns)}"


def required_stats(checks: Sequence[Check]) -> Dict[str, Tuple[str, Tuple[str, ...]]]:
    """Counter name -> (kind, columns) for every counter the checks need. `rows` is always there.

    non_null:<cols>   rows where the column is non-null; all_non_null:<cols> rows where every column is non-null;
    distinct:<cols>   distinct value combinations among those rows.
    """
    stats: Dict[str, Tuple[str, Tuple[str, ...]]] = {ROWS: ("rows", ())}
    for c in checks:
        if c.kind == "not_null":
            stats[_key("non_null", c.columns)] = ("non_null", c.columns)
        elif c.kind == "unique":
            stats[_key("all_non_null", c.columns)] = ("all_non_null", c.columns)
            stats[_key("distinct", c.columns)] = ("distinct", c.columns)
        elif c.kind != "non_empty":
            raise ValueError(f"unknown check kind: {c.kind}")
    return stats


def stats_from_rows(rows: Sequence[Mapping], checks: Sequence[Check]) -> Dict[str, int]:
    """The counters of `required_stats`, computed in Python over a list of dict-like rows."""
    out = {ROWS: len(rows)}
    for name, (kind, cols) in required_stats(checks).items():
        if kind == "non_null":
            out[name] = sum(1 for r in rows if r[cols[0]] is not None)
        elif kind == "all_non_null":
            out[name] = sum(1 for r in rows if all(r[c] is not None for c in cols))
        elif kind == "distinct":
            out[name] = len({tuple(r[c] for c in cols) for r in rows if all(r[c] is not None for c in cols)})
    return out


# --- Evaluation (pure) ---
def evaluate(checks: Sequence[Check], stats: Mapping[str, int]) -> List[Result]:
    """One Result per check, from counters produced by `stats_from_rows` or `spark_stats`."""
    rows = stats[ROWS]
    results = []
    for c in checks:
        if c.kind == "non_empty":
            results.append(Result(c, rows > 0, f"{rows} row(s)"))
        elif c.kind == "not_null":
            non_null = stats[_key("non_null", c.columns)]
            share = non_null / rows if rows else 1.0
            ok = share >= c.mostly if c.mostly < 1.0 else non_null == rows
            results.append(Result(c, ok, f"{rows - non_null} null of {rows} rows ({share:.2%} populated"
                                         + (f", expected >= {c.mostly:.2%})" if c.mostly < 1.0 else ")")))
        else:
            candidates = stats[_key("all_non_null", c.columns)]
            duplicates = candidates - stats[_key("distinct", c.columns)]
            results.append(Result(c, duplicates == 0, f"{duplicates} duplicate row(s) among {candidates}"))
    return results


def report(table: str, results: Sequence[Result], logger: Optional[logging.Logger] = None) -> None:
    """Log every result (AMBER failures as warnings, RED failures as errors) then raise if a RED check failed."""
    log = logger or _LOGGER
    failed_red = []
    for r in results:
        text = f"{table}: {r.check.label} - {r.detail}"
        if r.passed:
            log.info(f"[QUALITY OK] {text}")
        elif r.check.severity == RED:
            log.error(f"[QUALITY RED] {text}")
            failed_red.append(text)
        else:
            log.warning(f"[QUALITY AMBER] {text}")
    if failed_red:
        raise QualityCheckError(f"{len(failed_red)} RED quality check(s) failed on {table}:\n  " + "\n  ".join(failed_red))


def require_gx() -> None:
    """Import Great Expectations now: a notebook calls it in its first cells, so a cluster without GX fails in a
    minute instead of after the parse, at the quality checks."""
    from great_expectations.dataset import SparkDFDataset  # noqa: F401
    from leap_utils.common.validation import gx_validation  # noqa: F401


def check_dataframe(df, table: str, checks: Sequence[Check], logger: Optional[logging.Logger] = None) -> None:
    """Run the checks on a DataFrame with Great Expectations (leap_utils) and log them.

    The RED checks run first: a failure stops the task before the table is written by the caller.
    """
    from great_expectations.dataset import SparkDFDataset
    from leap_utils.common.validation import gx_validation

    (logger or _LOGGER).info(f"{table}: {len(checks)} quality check(s) with Great Expectations")
    gx_df = SparkDFDataset(df, persist=False)
    for level in (RED, AMBER):
        expectations = [_gx_expectation(gx_df, c) for c in checks if c.severity == level]
        if expectations:
            gx_validation.validate_and_log_gx_results(
                quality_check_results=expectations,
                validation_level=level,
                info_message=f"{table}: quality checks passed ({level})",
                error_message=f"{table}: a {level} quality check failed",
                table_name=table,
            )


def _gx_expectation(gx_df, check: Check):
    if check.kind == "non_empty":
        return gx_df.expect_table_row_count_to_be_between(min_value=1)
    if check.kind == "not_null":
        return gx_df.expect_column_values_to_not_be_null(check.columns[0], mostly=check.mostly)
    if len(check.columns) == 1:
        return gx_df.expect_column_values_to_be_unique(check.columns[0])
    return gx_df.expect_compound_columns_to_be_unique(list(check.columns))


def check_rows(rows: Sequence[Mapping], table: str, checks: Sequence[Check],
               logger: Optional[logging.Logger] = None) -> List[Result]:
    """Run the checks on a list of dict-like rows (no Spark) and report; raises on a RED failure."""
    results = evaluate(checks, stats_from_rows(rows, checks))
    report(table, results, logger)
    return results


# --- Chunk REF checks (3_parse) ---
# AMBER only: the incremental staleness detection of the next run self-heals once the manifest is corrected.
def check_ref_mapping(spark, df_chunks, logger: logging.Logger) -> List[Result]:
    """Cross-check the REF of the chunks with the current parse_manifest, in one aggregation.

    Counts the IDDOCs whose chunks show: a REF different from the manifest's, no manifest row at all (orphan, ghost
    data in the index), a `[Source: X]` prefix in chunk_text different from the REF column, or a REF with leading or
    trailing spaces. When a count is not zero, a few examples are logged.
    """
    from pyspark.sql import functions as F
    from config import PARSE_MANIFEST_TABLE

    df_manifest = spark.table(PARSE_MANIFEST_TABLE).select("IDDOC", F.col("ref").alias("manifest_ref"))
    prefix_ref = F.regexp_extract("chunk_text", r"\[Source:\s*(\S+)\s", 1)
    conditions = {
        "ref_differs_from_manifest": F.col("manifest_ref").isNotNull() & (F.col("REF") != F.col("manifest_ref")),
        "iddoc_absent_from_manifest": F.col("manifest_ref").isNull(),
        "source_prefix_differs_from_ref": (prefix_ref != "") & (prefix_ref != F.col("REF"))
                                          & (prefix_ref != F.trim(F.col("REF"))),
        "ref_has_surrounding_spaces": F.col("REF") != F.trim(F.col("REF")),
    }
    df = df_chunks.join(F.broadcast(df_manifest), on="IDDOC", how="left")
    counts = df.agg(*[F.count_distinct(F.when(cond, F.col("IDDOC"))).alias(name)
                      for name, cond in conditions.items()]).first()

    results = [Result(Check(name, ("REF",), AMBER), counts[name] == 0, f"{counts[name]} IDDOC(s)")
               for name in conditions]
    report("chunks", results, logger)
    for name, cond in conditions.items():
        if counts[name]:
            examples = df.filter(cond).select("IDDOC", "REF", "manifest_ref").distinct().limit(10).collect()
            logger.warning(f"[QUALITY AMBER] chunks: {name}, examples: "
                           f"{[(r.IDDOC, r.REF, r.manifest_ref) for r in examples]}")
    return results
