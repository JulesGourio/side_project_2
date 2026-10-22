# leap_utils

Trimmed copy of the shared `leap_utils` library: only the modules the parsing pipeline (`utils/parsing_pipeline/`) reaches, directly or through their imports, are kept. The app imports none of it.

| Module | Used for |
|---|---|
| `common/logger.py` | `get_logger`, `setup_applevel_logger` |
| `common/retry.py` | retry helper of `table_utils` |
| `common/merge_into/merge_into_spark.py`, `merge_into_utils.py` | `merge_into` |
| `common/validation/gx_validation.py`, `validation_utils.py`, `event_logging.py` | Great Expectations checks |
| `config.py` | Spark session (imported lazily by the pipeline) |
| `data_asset/table_utils.py` | `save_table`, table existence, merges |
| `platform/freshness/freshness_spark.py`, `freshness_utils.py` | freshness helpers imported by `table_utils` |

The other modules of the shared library are not copied. A module needed later is copied from the shared library as it is.
