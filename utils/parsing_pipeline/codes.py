"""Codes cited in the chunks (form, part, standard or document numbers), for the chat's exact code lookup.

The pattern and bounds are the chat's (``server/services/chat/engine/codes.py``): a code found here must be found the
same way in a question.
"""

from pyspark.sql import Column, DataFrame
from pyspark.sql import functions as f

CODE_PATTERN = r"(?<![A-Za-z0-9])(?:[A-Za-z0-9]+(?:[-_][A-Za-z0-9]+)+|[A-Za-z]{2,}[0-9]{3,})(?![A-Za-z0-9])"
CODE_MIN_CHARS = 5
CODE_MAX_CHARS = 40
# A code cited by more passages than this is too common to point at a passage (the chat takes 8 at most).
CODE_MAX_CHUNKS = 30
# Every chunk starts with its document's header line ([Source: REF | Title: ...]): its REF would cite every chunk.
_HEADER_PATTERN = r"^\[Source:[^\]]*\]"


def is_code(code: Column) -> Column:
    """Whether a match of ``CODE_PATTERN`` is kept as a code: a letter, a digit, a length within the bounds.

    :param code: column of one match
    :return: boolean column
    """
    return (
        (f.length(code) >= CODE_MIN_CHARS)
        & (f.length(code) <= CODE_MAX_CHARS)
        & code.rlike("[A-Za-z]")
        & code.rlike("[0-9]")
    )


def chunk_codes(df_chunks: DataFrame) -> DataFrame:
    """One row per code cited in the chunks, with the passages that cite it.

    :param df_chunks: chunks, with ``chunk_id`` and ``chunk_text``
    :return: ``code`` (upper case) and ``chunk_ids`` (sorted), for the codes cited by at most ``CODE_MAX_CHUNKS``
        passages
    """
    return (
        df_chunks.select(
            "chunk_id",
            f.explode(
                f.array_distinct(
                    f.regexp_extract_all(
                        f.regexp_replace(f.coalesce("chunk_text", f.lit("")), _HEADER_PATTERN, ""),
                        f.lit(CODE_PATTERN),
                        f.lit(0),
                    )
                )
            ).alias("match"),
        )
        .filter(is_code(f.col("match")))
        .select("chunk_id", f.upper("match").alias("code"))
        .groupBy("code")
        .agg(f.sort_array(f.collect_set("chunk_id")).alias("chunk_ids"))
        .filter(f.size("chunk_ids") <= CODE_MAX_CHUNKS)
    )
