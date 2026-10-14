from pyspark.sql import Row
from pyspark.sql.types import (
    StructType,
    TimestampType,
    StringType,
    BooleanType,
    IntegerType,
    DoubleType,
    ArrayType,
    StructField,
    MapType,
)
from pyspark.sql import SparkSession
from pyspark.sql import DataFrame as DataFrame
from datetime import datetime, timezone
from databricks.sdk.runtime import dbutils
from typing import Dict, List, Optional, NamedTuple
from delta.exceptions import ConcurrentAppendException, MetadataChangedException
import json
import requests

from leap_utils.common import retry
from leap_utils.data_asset.table_utils import UnityCatalogCheckTableExists


event_schema = StructType(
    [
        StructField("event_timestamp", TimestampType(), False),
        StructField("table_name", StringType(), True),
        StructField("expectation_type", StringType(), True),
        StructField("expectation_kwargs", StringType(), True),
        StructField("success", BooleanType(), True),
        StructField("unexpected_count", IntegerType(), True),
        StructField("unexpected_percent", DoubleType(), True),
        StructField("partial_unexpected_list", ArrayType(StringType()), True),
        StructField("severity_level", StringType(), True),
        StructField("raw_result", StringType(), True),
        StructField("pipeline_name", StringType(), True),
        StructField("job_id", StringType(), True),
        StructField("run_id", StringType(), True),
        StructField("notebook_name", StringType(), True),
        StructField("job_purpose", StringType(), True),
        StructField("domain", StringType(), True),
        StructField("tags", MapType(StringType(), StringType()), True),
    ]
)

QUALITY_EVENT_TABLE = "bronze.data_quality.quality_event_table"

# Secondary "datalakes" whose catalogs carry an explicit suffix after the zone (e.g.
# dev_bronze_hr). ALLOW-LIST, not a positional heuristic: main-lake catalogs like
# {env}_bronze_cleaned are multi-token but are NOT datalakes and must keep resolving to
# {env}_bronze.data_quality. Add new datalakes here as they appear.
_KNOWN_DATALAKES = ("hr",)


def resolve_quality_event_table_path(
    pipeline_write_env: str, dest_table: Optional[str] = None
) -> str:
    """
    Resolve the quality_event_table path in the ``data_quality`` schema of the **same datalake**
    as ``dest_table`` — so HR tables (``{env}_*_hr`` catalogs) log into
    ``{env}_bronze_hr.data_quality`` instead of the main ``{env}_bronze.data_quality`` (not
    reachable from the HR workspace).

    Backward compatible: when ``dest_table`` is None (or belongs to the main lake) the result is
    the historical ``{pipeline_write_env}_bronze.data_quality.quality_event_table``.

    :param pipeline_write_env: env prefix already resolved by the caller (e.g. "dev").
    :param dest_table: fully-qualified table being written (``catalog.schema.table``), used only
        to detect the datalake suffix from its catalog.
    :return: e.g. ``dev_bronze.data_quality.quality_event_table`` (main lake) or
        ``dev_bronze_hr.data_quality.quality_event_table`` (HR datalake).
    """
    suffix = ""
    if dest_table:
        catalog = dest_table.split(".")[0]
        for datalake in _KNOWN_DATALAKES:
            if catalog.endswith(f"_{datalake}"):
                suffix = datalake
                break
    _, schema_table = QUALITY_EVENT_TABLE.split(".", 1)  # drop the "bronze" zone
    zone_catalog = f"{pipeline_write_env}_bronze" + (f"_{suffix}" if suffix else "")
    return f"{zone_catalog}.{schema_table}"


class DatabricksContext(NamedTuple):
    """Execution context metadata from the Databricks notebook environment."""

    notebook_path: Optional[str]
    job_name: Optional[str]
    job_id: Optional[str]
    run_id: Optional[str]
    api_url: Optional[str]
    api_token: Optional[str]
    raw_context: Optional[Dict[str, str]]


class DatabricksJobTags(NamedTuple):
    """Execution context metadata from the Databricks job tags."""

    job_id: Optional[str]
    job_purpose: Optional[str]
    domain: Optional[str]
    tags: Optional[Dict[str, str]]


def _write_into_table(
    df_source: DataFrame,
    target_table: str,
    mode: str,
    overwrite_schema: bool = True,
    merge_schema: bool = False,
) -> None:
    """
    Write a DataFrame into a table using the Python / spark API.
    This function is called as a Callable by a retry.retry()

    :param df_source: DataFrame to merge
    :type df_source: DataFrame
    :param target_table: Table name of the table to MERGE INTO
    :type target_table: str
    :param mode: mode append|overwrite
    :type mode: str
    :param overwrite_schema: boolean to overwrite schema
    :type overwrite_schema: bool
    :param merge_schema: boolean to merge schema
    :type merge_schema: bool

    """
    writer = (
        df_source.write.mode(mode)
        .option("overwriteSchema", str(overwrite_schema).lower())
        .option("mergeSchema", str(merge_schema).lower())
    )
    writer.saveAsTable(target_table)

    return None


def _get_databricks_context(pipeline_name: Optional[str] = None) -> DatabricksContext:
    """
    Retrieves metadata about the current Databricks notebook execution context, including
    the notebook path, job name, job ID, and run ID.

    This function attempts to extract context information from the Databricks runtime environment.
    If some values are not available, such as when running outside of a scheduled job, the optional
    `pipeline_name` argument will be used as a fallback for the job name.

    :param pipeline_name: Optional name of the pipeline or workflow, used as a fallback if job context is missing.
    :type pipeline_name: Optional[str]

    :returns: An instance of DatabricksContext containing:
            - notebook_path (Optional[str]): Path to the current notebook.
            - job_name (Optional[str]): Name of the Databricks job or the provided pipeline name.
            - job_id (Optional[str]): Identifier of the job.
            - run_id (Optional[str]): Identifier of the job run.
            - api_url (Optional[str]): URL of API server.
            - api_token (Optional[str]): session token for API.
            - raw_context (Optional[Dict[str, str]]): dict with full context.
    :rtype: DatabricksContext
    """
    notebook_path = None
    job_name = pipeline_name
    job_id = None
    run_id = None
    api_url = None
    api_token = None
    context_info = None

    try:
        context = dbutils.notebook.entry_point.getDbutils().notebook().getContext()
        notebook_path = context.notebookPath().get()
        api_url = context.apiUrl().get()
        api_token = context.apiToken().get()
    except Exception as e:
        print(f"[WARN] Failed to get context: {e}")

    try:
        context_info = json.loads(context.toJson())
        tags = context_info.get("tags", {})
        job_name = pipeline_name or tags.get("jobName")
        job_id = tags.get("jobId")
        run_id = tags.get("jobRunId")
    except Exception as e:
        try:
            safe_json = json.loads(context.safeToJson())
            attributes = safe_json.get("attributes", {})
            job_id = attributes.get("jobId")
            run_id = attributes.get("multitaskParentRunId")
            context_info = safe_json
        except Exception as e:
            print(f"[WARN] Failed to get context tags: {e}")

    return DatabricksContext(
        notebook_path=notebook_path,
        job_name=job_name,
        job_id=job_id,
        run_id=run_id,
        api_url=api_url,
        api_token=api_token,
        raw_context=context_info,
    )


def _get_job_tags_and_domain(
    job_id: str, token: str, hostname: str
) -> DatabricksJobTags:
    """
    Retrieve Databricks job tags and derive job purpose and domain.

    This function calls the Databricks Jobs API to fetch metadata (tags) for a given job ID,
    then attempts to infer the job's purpose and domain based on specific tag values:
    - "job-purpose" or "dlt-purpose"
    - "data-asset-domain", "data-asset-group", "project-name", "platform-activity", "ingestion-system"

    The function returns a `DatabricksJobTags` named tuple containing the original tags along with
    the inferred `job_purpose` and `domain`.

    :param job_id: The Databricks job ID to retrieve tags for.
    :type job_id: str
    :param token: A valid Databricks API token.
    :type token: str
    :param hostname: The Databricks workspace URL (e.g., https://<instance>.cloud.databricks.com).
    :type hostname: str

    :return: A `DatabricksJobTags` object containing job ID, purpose, domain, and tag dictionary.
    :rtype: DatabricksJobTags

    :raises: None explicitly, but logs an error if the API call fails.
    """
    tags = {}
    job_purpose = None
    domain = None

    if job_id:
        headers = {"Authorization": f"Bearer {token}"}
        response = requests.get(
            f"{hostname}/api/2.1/jobs/get?job_id={job_id}", headers=headers
        )

        if response.status_code == 200:
            job_data = response.json()
            tags = job_data.get("settings", {}).get("tags", {})

            dlt_purpose = tags.get("dlt-purpose", "undefined_job_tag")
            job_purpose = tags.get("job-purpose", dlt_purpose)
            da_group = tags.get("data-asset-group", None)
            da_domain = tags.get("data-asset-domain", da_group)
            project_name = tags.get("project-name", None)
            platform_activity = tags.get("platform-activity", None)
            ingestion_system = tags.get("ingestion-system", None)

            if job_purpose not in ["data-asset", "project", "ingestion", "platform"]:
                job_purpose = "undefined_job_tag"

            if job_purpose == "data-asset" and da_domain:
                domain = da_domain
            elif job_purpose == "project" and project_name:
                domain = project_name
            elif job_purpose == "platform" and platform_activity:
                domain = platform_activity
            elif job_purpose == "ingestion" and ingestion_system:
                domain = ingestion_system
            else:
                domain = "undefined_job_tag"
        else:
            print(f"[ERREUR] API {response.status_code}: {response.text}")

    return DatabricksJobTags(
        job_id=job_id,
        job_purpose=job_purpose,
        domain=domain,
        tags=tags,
    )


def _log_to_quality_event_table(
    quality_check_results: Dict[str, bool],
    validation_level: str,
    pipeline_name: Optional[str] = None,
    expectation_type: Optional[str] = None,
    spark_session: Optional[SparkSession] = None,
    quality_event_table_path: Optional[str] = None,
    only_failed: bool = True,
    raw_result: Optional[Dict[str, Dict]] = None,
):
    """
    Log quality checks results (failed or all) into the quality_event_table.

    :param quality_check_results: Dict of results (dict key,value where key=table_name and value=True/False).
    :param validation_level: RED / AMBER.
    :param pipeline_name: Optional Name of the pipeline / workflow / notebook.
    :param expectation_type: Name of the quality check run on the key.
    :param spark_session: SparkSession.
    :param quality_event_table_path: Hive or Delta path to log into.
    :param only_failed: If True, only logs failed results; otherwise logs all.
    :param raw_result: Dict with optional information to log by table_name
    """
    if spark_session is None and quality_event_table_path:
        raise ValueError("Cannot log to quality_event_table without a Spark session.")

    if only_failed:
        results_to_log = {k: v for k, v in quality_check_results.items() if not v}
    else:
        results_to_log = quality_check_results

    if not results_to_log:
        return  # Nothing to log

    rows = []
    for table_name, success in results_to_log.items():
        unexpected_count = None
        unexpected_percent = None
        partial_unexpected_list = []
        effective_expectation_type = expectation_type or f"Unknown check: {table_name}"
        if spark_session:
            context = _get_databricks_context(pipeline_name)
            notebook_path = context.notebook_path
            job_name = context.job_name
            job_id = context.job_id
            run_id = context.run_id
            api_url = context.api_url
            api_token = context.api_token
            job_tags_object = _get_job_tags_and_domain(
                job_id=job_id, token=api_token, hostname=api_url
            )
            job_purpose = job_tags_object.job_purpose
            domain = job_tags_object.domain
            tags = job_tags_object.tags
        else:
            notebook_path = None
            job_name = pipeline_name
            job_id = None
            run_id = None
            api_url = None
            api_token = None
            job_purpose = None
            domain = None
            tags = None

        effective_raw_result = (raw_result.get(table_name) if raw_result else None) or {
            "expectation_type": effective_expectation_type,
            "table_name": table_name,
            "success": success,
        }

        rows.append(
            Row(
                event_timestamp=datetime.now(timezone.utc),
                table_name=table_name,
                expectation_type=effective_expectation_type,
                expectation_kwargs=json.dumps({"table_name": table_name}),
                success=success,
                unexpected_count=unexpected_count,
                unexpected_percent=unexpected_percent,
                partial_unexpected_list=partial_unexpected_list,
                severity_level=validation_level,
                raw_result=json.dumps(effective_raw_result),
                pipeline_name=job_name,
                job_id=job_id,
                run_id=run_id,
                notebook_name=notebook_path,
                job_purpose=job_purpose,
                domain=domain,
                tags=tags or None,
            )
        )

    df = spark_session.createDataFrame(rows, schema=event_schema)

    if UnityCatalogCheckTableExists(DbxTablePath=quality_event_table_path):
        retry.retry(
            func=_write_into_table,
            exceptions=(ConcurrentAppendException, MetadataChangedException),
            df_source=df,
            target_table=quality_event_table_path,
            mode="append",
            overwrite_schema=False,
            merge_schema=True,
        )
    else:
        retry.retry(
            func=_write_into_table,
            exceptions=(ConcurrentAppendException, MetadataChangedException),
            df_source=df,
            target_table=quality_event_table_path,
            mode="overwrite",
            overwrite_schema=True,
            merge_schema=False,
        )


def _log_expectations_to_table(
    quality_check_results: List[dict],
    validation_level: str,
    table_name: Optional[str] = None,
    pipeline_name: Optional[str] = None,
    spark_session: Optional[SparkSession] = None,
    quality_event_table_path: Optional[str] = None,
    only_failed: bool = True,
):
    """
    Log great expectations results (failed or all) into the quality_event_table.

    :param quality_check_results: List of Great Expectations results (dict).
    :param table_name: Name of the validated table or Dataframe.
    :param validation_level: RED / AMBER.
    :param pipeline_name: Name of the pipeline / workflow / notebook.
    :param spark_session: SparkSession.
    :param quality_event_table_path: Hive or Delta path to log into.
    :param only_failed: If True, only logs failed results; otherwise logs all.
    """
    if spark_session is None and quality_event_table_path:
        raise ValueError("Cannot log to quality_event_table without a Spark session.")

    results_to_log = (
        [r for r in quality_check_results if not r["success"]]
        if only_failed
        else quality_check_results
    )

    if not results_to_log:
        return  # Nothing to log

    rows = []
    for result in results_to_log:
        table_name = (
            result.get("meta", {}).get("table_path")
            or result.get("expectation_config", {}).get("meta", {}).get("table_path")
            or table_name
        )
        unexpected_count = result.get("result", {}).get("unexpected_count") or 0
        unexpected_percent = result.get("result", {}).get("unexpected_percent") or 0.0
        partial_unexpected_list = (
            result.get("result", {}).get("partial_unexpected_list") or []
        )
        observed_value = result.get("result", {}).get("observed_value") or []
        expected_columns = (
            result.get("expectation_config", {}).get("kwargs", {}).get("column_set", [])
        )
        unexpected_columns = []
        if observed_value and expected_columns:
            unexpected_columns = list(set(observed_value) - set(expected_columns))
            partial_unexpected_list = partial_unexpected_list + unexpected_columns
            unexpected_count = unexpected_count + len(partial_unexpected_list)

        if spark_session:
            context = _get_databricks_context(pipeline_name)
            notebook_path = context.notebook_path
            job_name = context.job_name
            job_id = context.job_id
            run_id = context.run_id
            api_url = context.api_url
            api_token = context.api_token
            job_tags_object = _get_job_tags_and_domain(
                job_id=job_id, token=api_token, hostname=api_url
            )
            job_purpose = job_tags_object.job_purpose
            domain = job_tags_object.domain
            tags = job_tags_object.tags
        else:
            notebook_path = None
            job_name = pipeline_name
            job_id = None
            run_id = None
            api_url = None
            api_token = None
            job_purpose = None
            domain = None
            tags = None

        rows.append(
            Row(
                event_timestamp=datetime.now(timezone.utc),
                table_name=table_name,
                expectation_type=result.get("expectation_config", {}).get(
                    "expectation_type"
                ),
                expectation_kwargs=json.dumps(
                    result.get("expectation_config", {}).get("kwargs")
                ),
                success=result.get("success"),
                unexpected_count=unexpected_count,
                unexpected_percent=unexpected_percent,
                partial_unexpected_list=partial_unexpected_list,
                severity_level=validation_level,
                raw_result=json.dumps(result),
                pipeline_name=job_name,
                job_id=job_id,
                run_id=run_id,
                notebook_name=notebook_path,
                job_purpose=job_purpose,
                domain=domain,
                tags=tags or None,
            )
        )

    df = spark_session.createDataFrame(rows, schema=event_schema)

    if UnityCatalogCheckTableExists(DbxTablePath=quality_event_table_path):
        retry.retry(
            func=_write_into_table,
            exceptions=(ConcurrentAppendException, MetadataChangedException),
            df_source=df,
            target_table=quality_event_table_path,
            mode="append",
            overwrite_schema=False,
            merge_schema=True,
        )
    else:
        retry.retry(
            func=_write_into_table,
            exceptions=(ConcurrentAppendException, MetadataChangedException),
            df_source=df,
            target_table=quality_event_table_path,
            mode="overwrite",
            overwrite_schema=True,
            merge_schema=False,
        )
