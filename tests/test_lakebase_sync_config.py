from pathlib import Path

LAKEBASE_SYNC_PATH = Path(__file__).resolve().parents[1] / 'resources' / 'lakebase_sync.yml'


def test_lakebase_jobs_never_write_to_a_landingzone_catalog():
    # Each environment's Lakebase copy lands in its project catalog (<env>_proj).
    assert '_landingzone' not in LAKEBASE_SYNC_PATH.read_text(encoding='utf-8')


def test_stale_export_files_are_the_json_files_no_table_wrote():
    import os
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "utils"))
    from ops_config import stale_export_files
    existing = ["chat_messages.json", "users.json", "old_table.json", "notes.txt"]
    assert stale_export_files(existing, {"chat_messages", "users"}) == ["old_table.json"]
    assert stale_export_files(existing, {"chat_messages", "users", "old_table"}) == []
