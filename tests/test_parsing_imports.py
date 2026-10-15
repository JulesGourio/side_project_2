"""The parsing modules the tests use import leap_utils without its config: leap_utils.config starts a Spark session at
import time, so it is only imported inside the functions that save or merge a table (tables.py)."""
import os
import subprocess
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..")
PARSING = os.path.join(ROOT, "utils", "parsing_pipeline")

CHECK = """
import sys
import tables, llm_describe, quality, doc_status, chunking
loaded = [m for m in sys.modules if m == 'leap_utils.config' or m == 'leap_utils.data_asset.table_utils'
          or m.startswith('pyspark')]
print(','.join(loaded))
"""


def test_importing_the_driver_helpers_does_not_start_spark():
    defaults = {"PIPELINE_WRITE_ENV": "dev", "PARSING_INTRAQUAL_BRONZE": "bronze.sch",
                "PARSING_VOLUME_ROOT_PATH": "/Volumes/c/s/root", "PARSING_VOLUME_BASE_PATH": "/Volumes/c/s/img",
                "PARSING_OFFLINE_MODELS": "/models"}
    env = {**os.environ, **{k: v for k, v in defaults.items() if k not in os.environ},
           "PYTHONPATH": os.pathsep.join([PARSING, os.path.abspath(ROOT)])}
    out = subprocess.run([sys.executable, "-c", CHECK], env=env, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    assert out.stdout.strip() == "", f"loaded at import time: {out.stdout.strip()}"
