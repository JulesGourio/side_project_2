from pathlib import Path

LAKEBASE_SYNC_PATH = Path(__file__).resolve().parents[1] / 'resources' / 'lakebase_sync.yml'


def test_lakebase_jobs_never_write_to_a_landingzone_catalog():
    # Each environment's Lakebase copy lands in its project catalog (<env>_proj).
    assert '_landingzone' not in LAKEBASE_SYNC_PATH.read_text(encoding='utf-8')
