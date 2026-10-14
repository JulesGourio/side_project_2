"""
This module defines spark at the global level, either using Databricks Connect or a
local session.
"""

import os

from leap_utils.common.logger import LOGGER_MAPPING, setup_applevel_logger

log = setup_applevel_logger(log_level=LOGGER_MAPPING["info"])

# Either use DatabricksSession or SparkSession, which is agnostic of the remote/local
# platform, see last code example at:
# https://docs.databricks.com/en/dev-tools/databricks-connect/python/examples.html
try:
    from databricks.connect import DatabricksSession
    log.info("Using remote Databricks spark session")
    _session = DatabricksSession
except ModuleNotFoundError:
    from pyspark.sql import SparkSession
    log.info("Using local spark session")
    _session = SparkSession

_builder = _session.builder

if os.getenv("LEAP_USE_SERVERLESS", "False").lower() == "true":
    # N.B. : serverless is only available when using Databricks Connect
    log.info("Enabled serverless connection")
    _builder = _builder.serverless(True)

spark = _builder.getOrCreate()
"""Global Spark session for the LEAP project"""
