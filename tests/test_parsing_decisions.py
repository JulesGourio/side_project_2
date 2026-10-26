"""Pure decisions of the parsing pipeline: parse status, checkpoint row choice, superseded targets, scope guard,
filter guard."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "utils", "parsing_pipeline"))

from decisions import (  # noqa: E402
    exclude_statuses,
    filter_would_empty_tables,
    parse_status,
    pick_checkpoint_row,
    scope_dropped_too_much,
)

LIMIT = 50 * 1024


@pytest.mark.parametrize(
    "length,size,error,strategy,expected",
    [
        (0, 100, "boom", None, "ERROR"),
        (0, 100, "timeout", "docling", "ERROR"),
        (10, 100, "partial", None, "ERROR"),
        (0, 100, None, "pending_llm_ocr:pdf", "EMPTY_TEXT"),
        (0, LIMIT + 1, None, "pending_llm_ocr:pdf", "EMPTY_TEXT"),
        (0, 100, None, None, "SKIPPED_EMPTY_SMALL_FILE"),
        (0, LIMIT, None, "docling", "SKIPPED_EMPTY_SMALL_FILE"),
        (0, LIMIT + 1, None, "docling", "EMPTY_TEXT"),
        (5, LIMIT + 1, None, None, "SUCCESS"),
        (5, 10, None, "pending_llm_ocr:pdf", "SUCCESS"),
    ],
)
def test_parse_status_matrix(length, size, error, strategy, expected) -> None:
    assert parse_status(length, size, error, strategy, LIMIT) == expected


def _row(sha, status, ts):
    return {"document_sha256": sha, "parse_status": status, "ingestion_timestamp": ts}


def test_a_success_of_other_bytes_never_wins_over_the_current_bytes() -> None:
    rows = [_row("old", "SUCCESS", 1), _row("new", "ERROR", 2)]
    assert pick_checkpoint_row(rows)["document_sha256"] == "new"


def test_same_bytes_prefer_success_then_latest() -> None:
    rows = [_row("a", "SUCCESS", 1), _row("a", "ERROR", 2)]
    assert pick_checkpoint_row(rows)["parse_status"] == "SUCCESS"
    rows = [_row("a", "ERROR", 1), _row("a", "EMPTY_TEXT", 3), _row("a", "ERROR", 2)]
    assert pick_checkpoint_row(rows)["ingestion_timestamp"] == 3
    assert pick_checkpoint_row([]) is None


def test_a_retry_success_on_other_bytes_is_the_latest() -> None:
    rows = [_row("a", "ERROR", 1), _row("b", "SUCCESS", 2)]
    assert pick_checkpoint_row(rows)["document_sha256"] == "b"


def test_superseded_revisions_are_never_targets() -> None:
    status = {1: "SKIPPED_SUPERSEDED_REVISION", 2: "SUCCESS"}
    out = exclude_statuses({1, 2, 3}, status, {"SKIPPED_SUPERSEDED_REVISION"})
    assert out == {2, 3}


@pytest.mark.parametrize(
    "previous,current,pct,expected",
    [
        (1000, 700, 30, False),
        (1000, 699, 30, True),
        (1000, 0, 30, True),
        (1000, 0, 0, False),
        (99, 0, 30, False),
        (100, 69, 30, True),
        (1000, 1500, 30, False),
    ],
)
def test_scope_guard(previous, current, pct, expected) -> None:
    assert scope_dropped_too_much(previous, current, pct) is expected


@pytest.mark.parametrize(
    "mode,parse_filter,expected",
    [
        ("full", [123], True),
        ("rechunk", [123, 456], True),
        ("incremental", [123], False),
        ("full", None, False),
        ("rechunk", None, False),
        ("full", [], False),
    ],
)
def test_filter_guard(mode, parse_filter, expected) -> None:
    assert filter_would_empty_tables(mode, parse_filter) is expected
