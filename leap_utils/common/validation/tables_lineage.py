from databricks.sdk import WorkspaceClient
from databricks.sdk.runtime import dbutils

from leap_utils.platform import deployment_environments

# API path
TABLE_LINEAGE_API_PATH = "/api/2.0/lineage-tracking/table-lineage"
# Lineage direction as defined in https://docs.databricks.com/en/data-governance/unity-catalog/data-lineage.html
LINEAGE_DIRECTIONS = ["upstreams", "downstreams"]
# secrets
SCOPE = "lineage_token"


def set_workspace_client(env: str = None) -> WorkspaceClient:
    """
    Creates and returns a WorkspaceClient based on the provided env.

    :param: env (str): The env of the workspace.
    :return:WorkspaceClient: An instance of the WorkspaceClient.

    """
    # If a token and host is provided, create a WorkspaceClient with the specified host
    # and token otherwise keep the cluster workspace
    if env:
        host = deployment_environments.WorkspaceHosts[env].value
        token = dbutils.secrets.get(scope=SCOPE, key=env)
        wc = WorkspaceClient(host=host, token=token)
    else:
        wc = WorkspaceClient()
    return wc


def get_table_lineage_json(table_path: str, wc: WorkspaceClient) -> dict:
    """
    Retrieve the JSON representation of the lineage tracking information for a specified
    table.

    :param: table_path (str) : table name for the lineage
    :param: wc (WorkspaceClient): DBX workspace
    :return: dict: dictionary with the upstream and down stream connected jobs
    """
    return wc.api_client.do(
        method="GET",
        path=TABLE_LINEAGE_API_PATH,
        query={
            "table_name": table_path,
            "include_entity_lineage": "true",
        },
    )


def get_jobs_after_filtering_workspace(
    direction: str, table_lineage_dict: dict, dbx_env: str = "prod"
) -> list:
    """
    Filter and retrieve job IDs from the lineage information for a specific direction
    (upstream or downstream) within a given workspace.
    """
    workspace_id = deployment_environments.WorkspaceIds[dbx_env].value
    return [
        j["job_id"]
        for d in filter(
            lambda x: ("tableInfo" in x or "fileInfo" in x) and "jobInfos" in x,
            table_lineage_dict.get(direction, {}),
        )
        for j in d["jobInfos"]
        if j["workspace_id"] == workspace_id and j["job_id"]
    ]


def get_upstream_downstream_lineage(
    table_path: str, wc: WorkspaceClient, dbx_env: str = "prod"
) -> dict:
    """
    Retrieve the upstream and downstream lineage jobs for a given table within a
    specific workspace.
    """
    table_lineage_dict = get_table_lineage_json(table_path, wc)
    dict_result = {
        key: list(
            set(get_jobs_after_filtering_workspace(key, table_lineage_dict, dbx_env))
        )
        for key in LINEAGE_DIRECTIONS
    }
    return dict_result
