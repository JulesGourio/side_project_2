"""Document statuses of processed_files (utils/parsing_pipeline/doc_status.py): a status the parse leaves open is
retried by the next run or moved on by the description step, so nothing waits in a status nothing moves."""
import os
import sys

PARSING = os.path.join(os.path.dirname(__file__), "..", "utils", "parsing_pipeline")
sys.path.insert(0, PARSING)

import doc_status as ds


def test_images_pending_is_done_for_the_parse_and_moved_by_the_description_step():
    assert ds.IMAGES_PENDING in ds.TERMINAL_STATUSES
    assert ds.IMAGES_PENDING in ds.MOVED_BY_IMAGES


def test_empty_text_is_retried_by_the_parse_until_the_description_step_moves_it():
    assert ds.EMPTY_TEXT not in ds.TERMINAL_STATUSES
    assert ds.EMPTY_TEXT in ds.MOVED_BY_IMAGES


def test_the_statuses_the_parse_retries_are_not_terminal():
    for status in ("ERROR", "MISSING_CHUNKS"):
        assert status not in ds.TERMINAL_STATUSES


def test_pruning_covers_every_status_that_has_derived_rows():
    assert {"SUCCESS", "ERROR", ds.EMPTY_TEXT, ds.IMAGES_PENDING} <= set(ds.PRUNED_STATUSES)
    assert set(ds.MOVED_BY_IMAGES) <= set(ds.PRUNED_STATUSES)


def test_every_image_status_is_either_retried_or_final():
    image_statuses = {"DONE", "SKIPPED", "SKIPPED_DECORATIVE", "EXTRACTION_FAILED", "FAILED_LIMIT", "PENDING", "ERROR"}
    retried, final = set(ds.IMAGE_RETRIED_STATUSES), set(ds.IMAGE_TERMINAL_STATUSES)
    assert not retried & final
    assert retried | final == image_statuses
    assert "FAILED_LIMIT" in final and "FAILED_LIMIT" not in retried
