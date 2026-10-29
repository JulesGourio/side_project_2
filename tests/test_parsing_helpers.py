"""Pure helpers of the parsing pipeline: image media type, index sync state, checkpoint snapshot retention."""
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "utils", "parsing_pipeline"))

PARSING_ENV = {"PIPELINE_WRITE_ENV": "dev", "REFERENCE_READ_ENV": "ref", "PARSING_IMAGES_VOLUME": "img",
               }
for _key, _value in PARSING_ENV.items():
    os.environ.setdefault(_key, _value)

from index_state import index_is_updating, sync_settled  # noqa: E402
from llm_describe import image_mime_type  # noqa: E402
from retention import snapshot_age_days, snapshots_to_drop  # noqa: E402


def test_image_media_type_comes_from_the_first_bytes() -> None:
    assert image_mime_type(b"\x89PNG\r\n\x1a\n....") == "image/png"
    assert image_mime_type(b"GIF89a....") == "image/gif"
    assert image_mime_type(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp"
    assert image_mime_type(b"\xff\xd8\xff\xe0....") == "image/jpeg"
    assert image_mime_type(b"") == "image/jpeg"


def test_an_index_is_updating_until_it_reports_no_pending_update() -> None:
    assert index_is_updating("ONLINE_TRIGGERED_UPDATE")
    assert index_is_updating("provisioning_endpoint")
    assert index_is_updating("ONLINE_UPDATING_PIPELINE_RESOURCES")
    assert not index_is_updating("ONLINE_NO_PENDING_UPDATE")
    assert not index_is_updating("ONLINE_CONTINUOUS_UPDATE")
    assert not index_is_updating("")


def test_snapshot_age_is_read_from_the_table_name() -> None:
    now = datetime(2026, 10, 21, 12, 0)
    assert snapshot_age_days("_pipeline_checkpoint_20260901_0300", now) == 50
    assert snapshot_age_days("parsing_checkpoint", now) is None
    assert snapshot_age_days("_pipeline_checkpoint_20269999_0300", now) is None


def test_old_snapshots_are_dropped_but_the_latest_is_kept() -> None:
    now = datetime(2026, 10, 21, 12, 0)
    names = ["parsing_checkpoint", "_pipeline_checkpoint_20260801_0300", "_pipeline_checkpoint_20260901_0300",
             "_pipeline_checkpoint_20261015_0300"]
    assert snapshots_to_drop(names, now, 30) == ["_pipeline_checkpoint_20260801_0300", "_pipeline_checkpoint_20260901_0300"]
    assert snapshots_to_drop(["_pipeline_checkpoint_20260101_0300"], now, 30) == []


def test_a_settled_state_right_after_the_trigger_is_not_the_end_of_the_sync() -> None:
    # The index still shows yesterday's state: not seen updating, rows differ from the table, grace not over.
    assert not sync_settled(True, "ONLINE_NO_PENDING_UPDATE", 68808, 104000, False, 30, 600)


def test_a_sync_is_over_once_its_update_was_seen_and_the_index_settled() -> None:
    assert not sync_settled(True, "ONLINE_TRIGGERED_UPDATE", 70000, 104000, True, 60, 600)
    assert sync_settled(True, "ONLINE_NO_PENDING_UPDATE", 104000, 104000, True, 900, 600)


def test_a_sync_with_nothing_to_apply_ends_when_the_rows_match_or_after_the_grace() -> None:
    assert sync_settled(True, "ONLINE_NO_PENDING_UPDATE", 68808, 68808, False, 30, 600)
    assert sync_settled(True, "ONLINE_NO_PENDING_UPDATE", 68800, 68808, False, 600, 600)
    assert not sync_settled(False, "ONLINE_NO_PENDING_UPDATE", 68808, 68808, True, 900, 600)
