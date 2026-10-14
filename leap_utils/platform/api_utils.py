"""
This module centralizes functions related to Databricks REST API interactions.

It provides utilities for listing and filtering workspace objects (jobs, clusters, warehouses),
managing Unity Catalog permissions, and patching workspace entitlements via SCIM.
"""

import json
import time
import requests
from databricks.sdk.runtime import dbutils
from leap_utils.common import logger
import re
from typing import Any, Dict, List, Optional, Set, Union
from urllib.parse import quote

LOGGER = logger.get_logger(module_name=__name__)

# === Databricks credentials (from notebook context) ===
TOKEN = (
    dbutils.notebook.entry_point.getDbutils().notebook().getContext().apiToken().get()
)
LOGGER.info("Databricks Token retrieved.")

DATABRICKS_INSTANCE = (
    dbutils.notebook.entry_point.getDbutils().notebook().getContext().apiUrl().get()
)
LOGGER.info(f"Databricks Host: {DATABRICKS_INSTANCE}")

# Headers for authentication
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}


# === FUNCTIONS ===
def list_jobs() -> List[Dict[str, Any]]:
    """
    List all Databricks jobs in the workspace.

    :return: A list of job metadata dictionaries.
    :rtype: List[Dict[str, Any]]
    :raises RuntimeError: If the API request fails.
    """
    data = api_request("GET", "/api/2.2/jobs/list")
    return data.get("jobs", [])


def list_clusters() -> List[Dict[str, Any]]:
    """
    List all Databricks clusters in the workspace.

    :return: A list of cluster metadata dictionaries.
    :rtype: List[Dict[str, Any]]
    :raises RuntimeError: If the API request fails.
    """
    data = api_request("GET", "/api/2.0/clusters/list")
    return data.get("clusters", [])


def list_warehouses() -> List[Dict[str, Any]]:
    """
    List all SQL warehouses in the Databricks workspace.

    :return: A list of SQL warehouse metadata dictionaries.
    :rtype: List[Dict[str, Any]]
    :raises RuntimeError: If the API request fails.
    """
    data = api_request("GET", "/api/2.0/sql/warehouses")
    return data.get("warehouses", [])


def match_pattern(name: str, pattern: str) -> bool:
    """
    Match a name against a case-insensitive shell-style wildcard pattern.

    :param name: The target name to compare.
    :type name: str
    :param pattern: The wildcard pattern (e.g., '*Sigma*', 'Job-*').
    :type pattern: str
    :return: True if the name matches the pattern, False otherwise.
    :rtype: bool
    """
    regex = re.compile(
        "^" + re.escape(pattern).replace(r"\*", ".*") + "$", re.IGNORECASE
    )
    return bool(regex.match(name))


def find_jobs_by_pattern(pattern: str) -> List[Dict[str, Any]]:
    """
    Find Databricks jobs matching a given name pattern.

    :param pattern: Wildcard pattern to match job names.
    :type pattern: str
    :return: List of matching job dictionaries.
    :rtype: List[Dict[str, Any]]
    """
    jobs = list_jobs()
    return [
        j for j in jobs if match_pattern(j.get("settings", {}).get("name", ""), pattern)
    ]


def find_clusters_by_pattern(pattern: str) -> List[Dict[str, Any]]:
    """
    Find Databricks clusters matching a given name pattern.

    :param pattern: Wildcard pattern to match cluster names.
    :type pattern: str
    :return: List of matching cluster dictionaries.
    :rtype: List[Dict[str, Any]]
    """
    clusters = list_clusters()
    return [c for c in clusters if match_pattern(c.get("cluster_name", ""), pattern)]


def find_warehouses_by_pattern(pattern: str) -> List[Dict[str, Any]]:
    """
    Find SQL warehouses matching a given name pattern.

    :param pattern: Wildcard pattern to match warehouse names.
    :type pattern: str
    :return: List of matching warehouse dictionaries.
    :rtype: List[Dict[str, Any]]
    """
    whs = list_warehouses()
    return [w for w in whs if match_pattern(w.get("name", ""), pattern)]


def find_group_id_by_display_name(display_name: str) -> Optional[str]:
    """
    Retrieve the SCIM group ID for a given group display name.

    :param display_name: The display name of the Databricks group.
    :type display_name: str
    :return: The SCIM group ID if found, otherwise None.
    :rtype: Optional[str]
    :raises RuntimeError: If the SCIM API request fails.
    """
    filter_str = quote(f'displayName eq "{display_name}"')
    endpoint = f"/api/2.0/preview/scim/v2/Groups?filter={filter_str}"
    data = api_request("GET", endpoint)
    resources = data.get("Resources", [])
    if not resources:
        return None
    return resources[0].get("id")


def get_workspace_permissions(
    resource_path: str, resource_id: Union[str, int]
) -> Dict[str, Any]:
    """
    Retrieve workspace permissions for a specific Databricks resource.

    :param resource_path: API path of the resource (e.g., 'jobs', 'clusters').
    :type resource_path: str
    :param resource_id: The resource ID.
    :type resource_id: Union[str, int]
    :return: Permissions metadata for the resource.
    :rtype: Dict[str, Any]
    """
    endpoint = f"/api/2.0/permissions/{resource_path}/{resource_id}"
    return api_request("GET", endpoint)


def get_service_principal_id(sp_name: str) -> Optional[str]:
    """
    Retrieve the Application ID (UUID) for a given service principal by name.

    :param sp_name: Display name of the service principal.
    :type sp_name: str
    :return: The Application ID (UUID) or None if not found.
    :rtype: Optional[str]
    :raises RuntimeError: If the SCIM API request fails.
    """
    try:
        filter_str = quote(f'displayName eq "{sp_name}"')
        endpoint = f"/api/2.0/preview/scim/v2/ServicePrincipals?filter={filter_str}"
        data = api_request("GET", endpoint)
        resources = data.get("Resources", [])
        if not resources:
            print(f"⚠️ Service principal '{sp_name}' not found via SCIM")
            return None
        # Return the applicationId (UUID), not the SCIM id
        return resources[0].get("applicationId") or resources[0].get("id")
    except Exception as e:
        print(f"❌ Error looking up service principal '{sp_name}': {e}")
        return None


def resolve_principal_identifier(grantee: str, grantee_type: str) -> Optional[str]:
    """
    Resolve the appropriate principal identifier for a given grantee.

    :param grantee: Display name or email of the principal.
    :type grantee: str
    :param grantee_type: Type of principal ('group', 'user', 'service_principal').
    :type grantee_type: str
    :return: Identifier to be used in API calls (Application ID for service principals).
    :rtype: Optional[str]
    """
    if grantee_type == "service_principal":
        sp_id = get_service_principal_id(grantee)
        if sp_id:
            print(f"🔍 Resolved '{grantee}' to Application ID: {sp_id}")
        return sp_id
    else:
        # Groups and users use their display name / email directly
        return grantee


def patch_group_entitlements(
    group_id: str, entitlements: List[str], dry_run_flag: bool
) -> bool:
    """
    Patch workspace entitlements for a given group using the SCIM API.

    :param group_id: SCIM group ID.
    :type group_id: str
    :param entitlements: List of entitlements to assign (e.g., ['workspace-access'], ['databricks-sql-access']).
    :type entitlements: List[str]
    :param dry_run_flag: If True, simulate without applying.
    :type dry_run_flag: bool
    :return: True if successful or simulated, False otherwise.
    :rtype: bool
    """
    if dry_run_flag:
        print(
            f"[DRY-RUN] SCIM: would PATCH entitlements {entitlements} to group {group_id}"
        )
        return True

    # SCIM PATCH payload with proper schema
    payload = {
        "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
        "Operations": [
            {
                "op": "add",
                "path": "entitlements",
                "value": [{"value": e} for e in entitlements],
            }
        ],
    }

    endpoint = f"/api/2.0/preview/scim/v2/Groups/{group_id}"

    # SCIM requires application/scim+json content type
    custom_headers = {"Content-Type": "application/scim+json"}

    try:
        api_request("PATCH", endpoint, payload, custom_headers)
        print(f"✅ SCIM: patched entitlements {entitlements} on group_id {group_id}")
        return True
    except Exception as e:
        print(f"❌ SCIM error for group {group_id}: {str(e)}")
        return False


def grant_unity_catalog(
    object_type: str,
    object_name: str,
    grantee: str,
    privileges: List[str],
    dry_run_flag: bool,
) -> bool:
    """
    Grant Unity Catalog privileges to a principal using REST API

    :param object_type: Type of securable ('CATALOG', 'SCHEMA', or 'TABLE').
    :type object_type: str
    :param object_name: Fully qualified name of the object.
    :type object_name: str
    :param grantee: Principal name (group, service principal, or user).
    :type grantee: str
    :param privileges: List of privileges (e.g., ['SELECT', 'USE_SCHEMA']).
    :type privileges: List[str]
    :param dry_run_flag: If True, simulate the grant without applying.
    :type dry_run_flag: bool
    :return: True if successful or simulated, False otherwise.
    :rtype: bool
    """
    grantee_type = get_grantee_type(grantee)

    if dry_run_flag:
        print(
            f"[DRY-RUN] UNITY-CATALOG: would grant {privileges} on {object_type}:{object_name} to {grantee} ({grantee_type})"
        )
        return True

    try:
        # Determine the securable type for the API
        securable_type_map = {
            "CATALOG": "catalog",
            "SCHEMA": "schema",
            "TABLE": "table",
        }
        securable_type = securable_type_map.get(object_type, object_type.lower())

        # Resolve the principal identifier (important for service principals)
        principal = resolve_principal_identifier(grantee, grantee_type)
        if not principal:
            print(f"❌ UNITY-CATALOG: Could not resolve principal for {grantee}")
            return False

        # Unity Catalog grants endpoint
        endpoint = f"/api/2.1/unity-catalog/permissions/{securable_type}/{object_name}"

        # Build the payload - add grants for each privilege
        changes = []
        for priv in privileges:
            changes.append({"principal": principal, "add": [priv]})

        payload = {"changes": changes}

        # Make the API call
        resp = api_request("PATCH", endpoint, payload)

        print(
            f"✅ UNITY-CATALOG: granted {privileges} on {object_type}:{object_name} to {grantee} ({grantee_type}, principal={principal})"
        )
        return True

    except Exception as e:
        print(
            f"❌ UNITY-CATALOG error for {object_type}:{object_name} to {grantee}: {str(e)}"
        )
        return False


def get_grantee_type(grantee: str) -> str:
    """
    Determine the principal type for a given grantee.

    :param grantee: Principal identifier (group, service principal, or user).
    :type grantee: str
    :return: One of 'group', 'service_principal', or 'user'.
    :rtype: str
    """
    grantee_lower = grantee.lower()

    # Check groups first (most specific)
    if grantee.startswith(("Role-", "role-")):
        return "group"

    # Check service principals
    if grantee_lower.startswith(("spn-", "spn:")) or grantee_lower.endswith(
        "-service-principal"
    ):
        return "service_principal"

    # Check users
    if "@" in grantee:
        return "user"

    # Default to group for safety
    return "group"


def api_request(
    method: str,
    endpoint: str,
    payload: Optional[Dict[str, Any]] = None,
    custom_headers: Optional[Dict[str, str]] = None,
    params: Optional[Dict[str, Any]] = None,
    host: Optional[str] = None,
    token: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Perform a generic Databricks API request.

    :param method: HTTP method (e.g., 'GET', 'POST', 'PATCH').
    :type method: str
    :param endpoint: REST API endpoint path.
    :type endpoint: str
    :param payload: Optional request payload for POST/PATCH calls (sent as JSON body).
    :type payload: Optional[Dict[str, Any]]
    :param custom_headers: Optional custom headers to merge with defaults.
    :type custom_headers: Optional[Dict[str, str]]
    :param params: Optional query string parameters for GET requests.
    :type params: Optional[Dict[str, Any]]
    :param host: Override workspace host URL (default: module-level DATABRICKS_INSTANCE).
    :type host: Optional[str]
    :param token: Override authentication token (default: module-level TOKEN).
    :type token: Optional[str]
    :return: Parsed JSON response as a dictionary.
    :rtype: Dict[str, Any]
    :raises RuntimeError: If the API response is not OK.
    """
    _host = (host or DATABRICKS_INSTANCE).rstrip("/")
    if not _host.startswith("http"):
        _host = f"https://{_host}"
    _token = token or TOKEN
    url = f"{_host}{endpoint}"
    headers = {"Authorization": f"Bearer {_token}"}

    # Merge custom headers if provided
    if custom_headers:
        headers.update(custom_headers)

    resp = requests.request(method, url, headers=headers, json=payload, params=params)
    if not resp.ok:
        raise RuntimeError(f"API error {resp.status_code}: {resp.text}")
    return resp.json() if resp.text else {}


def list_job_runs(
    from_ms: int,
    to_ms: int,
    completed_only: bool = False,
    expand_tasks: bool = True,
    host: Optional[str] = None,
    token: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    List Databricks job runs within a start_time window, with automatic pagination.

    Calls GET /api/2.1/jobs/runs/list and follows page_token until has_more=False.

    :param from_ms: Window start as epoch milliseconds (inclusive).
    :type from_ms: int
    :param to_ms: Window end as epoch milliseconds (inclusive).
    :type to_ms: int
    :param completed_only: If True, only return completed runs. Default: False.
    :type completed_only: bool
    :param expand_tasks: If True, include the tasks array in each run (needed to
        detect trigger_job_id from run_job_task entries). Default: True.
    :type expand_tasks: bool
    :param host: Override workspace host URL (cross-workspace calls).
    :type host: Optional[str]
    :param token: Override authentication token (cross-workspace calls).
    :type token: Optional[str]
    :return: List of run dictionaries as returned by the API.
    :rtype: List[Dict[str, Any]]
    :raises RuntimeError: If any API request fails.
    """
    params = {
        "completed_only": completed_only,
        "expand_tasks": expand_tasks,
        "limit": 25,
        "start_time_from": from_ms,
        "start_time_to": to_ms,
    }
    runs = []
    page = 0
    while True:
        data = api_request("GET", "/api/2.1/jobs/runs/list", params=params, host=host, token=token)
        batch = data.get("runs", [])
        runs.extend(batch)
        page += 1
        LOGGER.debug(f"list_job_runs page {page}: {len(batch)} runs (total: {len(runs)})")
        if not data.get("has_more", False):
            break
        params["page_token"] = data["next_page_token"]
        time.sleep(0.1)  # respectful throttling
    return runs


def get_job(
    job_id: int,
    host: Optional[str] = None,
    token: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Retrieve the full settings and metadata of a single Databricks job.

    Calls GET /api/2.1/jobs/get.

    :param job_id: The Databricks job ID.
    :type job_id: int
    :param host: Override workspace host URL (cross-workspace calls).
    :type host: Optional[str]
    :param token: Override authentication token (cross-workspace calls).
    :type token: Optional[str]
    :return: Job object as returned by the API (settings, creator_user_name, etc.).
    :rtype: Dict[str, Any]
    :raises RuntimeError: If the API request fails.
    """
    return api_request("GET", "/api/2.1/jobs/get", params={"job_id": job_id}, host=host, token=token)


def get_jobs_metadata(
    job_ids: Set[int],
    host: Optional[str] = None,
    token: Optional[str] = None,
) -> Dict[int, Dict[str, Any]]:
    """
    Fetch flattened job metadata for a set of job IDs (one API call per job).

    For each job, extracts: job_name, owner, run_as_user, run_as_sp, tags,
    git_url, git_branch.

    Jobs that cannot be fetched (deleted, insufficient permissions) receive a
    fallback dict with None values so callers don't need to handle missing keys.

    :param job_ids: Set of Databricks job IDs to fetch.
    :type job_ids: Set[int]
    :param host: Override workspace host URL (cross-workspace calls).
    :type host: Optional[str]
    :param token: Override authentication token (cross-workspace calls).
    :type token: Optional[str]
    :return: Dict mapping job_id -> metadata dict.
    :rtype: Dict[int, Dict[str, Any]]
    """
    cache: Dict[int, Dict[str, Any]] = {}
    for job_id in sorted(job_ids):
        try:
            job = get_job(job_id, host=host, token=token)
            settings = job.get("settings") or {}
            git = settings.get("git_source") or {}
            run_as = settings.get("run_as") or {}
            owner = job.get("creator_user_name")

            # run_as explicite (settings.run_as) : priorité absolue
            run_as_user = run_as.get("user_name")
            run_as_sp = run_as.get("service_principal_name")

            # Fallback Databricks : si run_as absent, le job tourne sous son créateur
            if not run_as_user and not run_as_sp and owner:
                if "@" in owner:
                    run_as_user = owner
                else:
                    run_as_sp = owner

            # Extract child job IDs from run_job_task entries
            child_job_ids = []
            for task in settings.get("tasks") or []:
                rjt = task.get("run_job_task") or {}
                child_jid = rjt.get("job_id")
                if child_jid:
                    child_job_ids.append(int(child_jid))

            cache[job_id] = {
                "job_name": settings.get("name", f"job_{job_id}"),
                "owner": owner,
                "run_as_user": run_as_user,
                "run_as_sp": run_as_sp,
                "tags": json.dumps(settings["tags"]) if settings.get("tags") else None,
                "git_url": git.get("git_url"),
                "git_branch": git.get("git_branch"),
                "child_job_ids": child_job_ids,
            }
        except Exception as e:
            err_str = str(e)
            if "does not exist" in err_str or "INVALID_PARAMETER_VALUE" in err_str:
                # Job deleted but runs still in history — expected, not an error
                LOGGER.debug(f"Job {job_id} no longer exists (deleted), using fallback metadata")
            else:
                LOGGER.warning(f"Could not fetch metadata for job_id={job_id}: {e}")
            cache[job_id] = {
                "job_name": f"deleted_job_{job_id}",
                "owner": None,
                "run_as_user": None,
                "run_as_sp": None,
                "tags": None,
                "git_url": None,
                "git_branch": None,
                "child_job_ids": [],
            }
        time.sleep(0.05)
    return cache


def build_sp_cache(
    host: Optional[str] = None,
    token: Optional[str] = None,
) -> Dict[str, str]:
    """
    Build a cache of service principals mapping application_id (UUID) to display_name.

    Calls GET /api/2.0/preview/scim/v2/ServicePrincipals (paginated).
    Used to resolve UUID identifiers (creator_user_name, run_as_sp) to human-readable names.

    :param host: Override workspace host URL (cross-workspace calls).
    :type host: Optional[str]
    :param token: Override authentication token (cross-workspace calls).
    :type token: Optional[str]
    :return: Dict mapping str(applicationId) -> displayName.
    :rtype: Dict[str, str]
    """
    cache: Dict[str, str] = {}
    params: Dict[str, Any] = {"count": 100, "startIndex": 1}
    while True:
        data = api_request(
            "GET", "/api/2.0/preview/scim/v2/ServicePrincipals",
            params=params, host=host, token=token,
        )
        resources = data.get("Resources", [])
        for sp in resources:
            app_id = str(sp.get("applicationId", ""))
            display_name = sp.get("displayName", app_id)
            if app_id:
                cache[app_id] = display_name
        total = data.get("totalResults", 0)
        start = data.get("startIndex", 1)
        if start + len(resources) - 1 >= total or not resources:
            break
        params["startIndex"] = start + len(resources)
    LOGGER.info(f"build_sp_cache: {len(cache)} service principals loaded")
    return cache


def patch_workspace_permission(
    resource_path: str,
    resource_id: Union[str, int],
    grantee: str,
    permission_level: str,
    dry_run_flag: bool,
) -> bool:
    """
    Patch workspace permissions for a given resource.

    :param resource_path: Resource category (e.g., 'jobs', 'clusters', 'warehouses', 'apps').
    :type resource_path: str
    :param resource_id: The ID or name of the resource.
    :type resource_id: Union[str, int]
    :param grantee: Principal name (group, user, or service principal).
    :type grantee: str
    :param permission_level: Permission level to grant (e.g., 'CAN_USE').
    :type permission_level: str
    :param dry_run_flag: If True, simulate the permission update without applying.
    :type dry_run_flag: bool
    :return: True if successful or simulated, False otherwise.
    :rtype: bool
    """
    grantee_type = get_grantee_type(grantee)

    if dry_run_flag:
        print(
            f"[DRY-RUN] WORKSPACE: would PATCH permission {permission_level} on {resource_path}/{resource_id} to {grantee} ({grantee_type})"
        )
        return True

    # Resolve the principal identifier (critical for service principals)
    principal = resolve_principal_identifier(grantee, grantee_type)
    if not principal:
        print(f"❌ WORKSPACE: Could not resolve principal for {grantee}")
        return False

    endpoint = f"/api/2.0/permissions/{resource_path}/{resource_id}"

    # Build entry based on grantee type
    entry = {"permission_level": permission_level}
    if grantee_type == "service_principal":
        # For service principals, use the resolved Application ID
        entry["service_principal_name"] = principal
        print(f"🔍 Using Application ID for '{grantee}': {principal}")
    elif grantee_type == "user":
        entry["user_name"] = principal
    else:  # group
        entry["group_name"] = principal

    payload = {"access_control_list": [entry]}

    try:
        resp = api_request("PATCH", endpoint, payload)
        print(
            f"✅ WORKSPACE: patched permission {permission_level} on {resource_path}/{resource_id} to {grantee} ({grantee_type})"
        )
        return True
    except Exception as e:
        print(
            f"❌ WORKSPACE error for {resource_path}/{resource_id} to {grantee}: {str(e)}"
        )
        return False
