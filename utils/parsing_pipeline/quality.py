"""quality.py: the data quality checks of the parsing pipeline.

Two levels, RED (the task stops, nothing is written) and AMBER (a warning is logged, the task goes on).

- `check_dataframe` computes every counter of a table in one Spark aggregation. It is called in the grouped
  `# Quality Checks` section of a notebook, on the DataFrame that is written.
- `check_rows` runs the same checks over Python rows, for the serverless task that has no Spark cluster.

    quality.check_dataframe(df_chunks, "chunks", [
        quality.not_null("chunk_id"), quality.unique("chunk_id"),
        quality.not_null("division", mostly=0.99, severity=quality.AMBER),
    ], logger)
"""

import logging
from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from leap_utils.common.logger import get_logger

_LOGGER = get_logger(__name__)

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


def spark_stats(df, checks: Sequence[Check]) -> Dict[str, int]:
    """The counters of `required_stats` for a DataFrame, from a single aggregation (one Spark action)."""
    from pyspark.sql import functions as F

    exprs = []
    for name, (kind, cols) in required_stats(checks).items():
        if kind == "rows":
            expr = F.count(F.lit(1))
        elif kind == "non_null":
            expr = F.count(F.col(cols[0]))
        elif kind == "all_non_null":
            cond = F.col(cols[0]).isNotNull()
            for c in cols[1:]:
                cond = cond & F.col(c).isNotNull()
            expr = F.count(F.when(cond, F.lit(1)))
        else:
            expr = F.count_distinct(*[F.col(c) for c in cols])   # skips rows with a null key, like all_non_null
        exprs.append(expr.alias(name))
    row = df.agg(*exprs).first()
    return {name: int(row[name]) for name in required_stats(checks)}


def check_dataframe(df, table: str, checks: Sequence[Check], logger: Optional[logging.Logger] = None) -> List[Result]:
    """Run the checks on a DataFrame and report; raises `QualityCheckError` on a RED failure, before the caller writes.

    Plain Spark, no Great Expectations: it is missing from the GPU cluster of `3_parse`, where the checks run only
    after hours of parsing, and one aggregation gives the same counters.
    """
    results = evaluate(checks, spark_stats(df, checks))
    report(table, results, logger)
    return results


def check_rows(rows: Sequence[Mapping], table: str, checks: Sequence[Check],
               logger: Optional[logging.Logger] = None) -> List[Result]:
    """Run the checks on a list of dict-like rows (no Spark) and report; raises on a RED failure."""
    results = evaluate(checks, stats_from_rows(rows, checks))
    report(table, results, logger)
    return results
