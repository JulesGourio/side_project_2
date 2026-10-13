"""parse_checks.py — checks on the content of the chunks that need another table (the manifest), for `3_parse`.

The generic checks (not null, unique, non empty) are in `quality.py`; the findings here are AMBER only: the incremental
staleness detection of the next run self-heals once the manifest is corrected, an early warning shortens the exposure.
"""
import logging
from typing import List

from pyspark.sql import functions as F

from config import PARSE_MANIFEST_TABLE
from quality import AMBER, Check, Result, report


def check_ref_mapping(spark, df_chunks, logger: logging.Logger) -> List[Result]:
    """Cross-check the REF of the chunks with the current parse_manifest, in one aggregation.

    Counts the IDDOCs whose chunks show: a REF different from the manifest's, no manifest row at all (orphan, ghost
    data in the index), a `[Source: X]` prefix in chunk_text different from the REF column, or a REF with leading or
    trailing spaces. When a count is not zero, a few examples are logged.
    """
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
