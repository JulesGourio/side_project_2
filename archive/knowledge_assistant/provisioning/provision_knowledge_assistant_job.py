# Databricks notebook source
# DBTITLE 1,Provision Knowledge Assistants (idempotent — safe to re-run)
from databricks.sdk import WorkspaceClient
from databricks.sdk.common.types.fieldmask import FieldMask
from databricks.sdk.service.serving import (
    ServingEndpointAccessControlRequest,
    ServingEndpointPermissionLevel,
)
from databricks.sdk.service.knowledgeassistants import (
    IndexSpec,
    KnowledgeAssistant,
    KnowledgeAssistantAccessControlRequest,
    KnowledgeAssistantPermissionLevel,
    KnowledgeSource,
)

# Same pattern as migrate_lakebase_job.py: serverless tasks have no cluster
# spec, so job parameters come through as notebook widgets.
dbutils.widgets.text("KA_PROFILES", "ALL,AS,IS")
dbutils.widgets.text("CATALOG", "uat_landingzone")
dbutils.widgets.text("SCHEMA", "qualibot")
dbutils.widgets.text("INDEX_SUFFIX", "_v1")
# Appended to display_name_base to get the live KA name -- "_v2" matches
# qualibot-uat's existing KAs by name (changes nothing there); qualibot-prod
# overrides this to "" for clean, unsuffixed names.
dbutils.widgets.text("DISPLAY_NAME_SUFFIX", "_v2")
dbutils.widgets.text("TEXT_COL", "chunk_text")
dbutils.widgets.text("DOC_URI_COL", "url")
# The app whose service principal gets CAN_QUERY on each KA (the app calls
# the KA endpoint with the end user's own forwarded token, but querying it
# still requires this app's own SP to hold at least CAN_QUERY — confirmed
# live 2026-08-24 against the qualibot_*_v2 KAs already in service).
dbutils.widgets.text("APP_NAME", "qualibot-uat-test")
# Comma-separated users granted CAN_MANAGE on top of the CoreAdmin/CoreDev
# groups — for a human who isn't in those groups but owns the environment
# (DEV copy, 2026-10-05: the KAs are created by the job's run_as SP, so their
# owner can't even view the endpoints otherwise). Empty = none.
dbutils.widgets.text("EXTRA_MANAGER_USERS", "")
# "true" also writes the grants straight onto each KA's serving endpoint
# (extra users CAN_MANAGE, app SP CAN_QUERY): in DEV (2026-10-05) the KA-level
# ACL reached neither the owner nor the app SP on the endpoint.
dbutils.widgets.dropdown("GRANT_ON_ENDPOINTS", "false", ["false", "true"])

KA_PROFILE_KEYS = [p.strip() for p in dbutils.widgets.get("KA_PROFILES").split(",") if p.strip()]
CATALOG = dbutils.widgets.get("CATALOG")
SCHEMA = dbutils.widgets.get("SCHEMA")
INDEX_SUFFIX = dbutils.widgets.get("INDEX_SUFFIX")
DISPLAY_NAME_SUFFIX = dbutils.widgets.get("DISPLAY_NAME_SUFFIX")
TEXT_COL = dbutils.widgets.get("TEXT_COL")
DOC_URI_COL = dbutils.widgets.get("DOC_URI_COL")
APP_NAME = dbutils.widgets.get("APP_NAME")
EXTRA_MANAGER_USERS = [u.strip() for u in dbutils.widgets.get("EXTRA_MANAGER_USERS").split(",") if u.strip()]
GRANT_ON_ENDPOINTS = dbutils.widgets.get("GRANT_ON_ENDPOINTS") == "true"

# Sibling module import (Databricks puts the notebook's own directory on
# sys.path, same as migrate_lakebase_job.py's `from migrations import ...`).
from ka_profiles import PROFILES

unknown = set(KA_PROFILE_KEYS) - set(PROFILES)
if unknown:
    raise ValueError(f"Unknown KA profile(s) {sorted(unknown)}. Known: {sorted(PROFILES)}")

w = WorkspaceClient()

try:
    app_sp_client_id = w.apps.get(name=APP_NAME).service_principal_client_id
except Exception as e:
    print(f"WARNING: could not resolve service principal for app '{APP_NAME}' ({e}) — its CAN_QUERY grant will be skipped.")
    app_sp_client_id = None

# COMMAND ----------

existing_by_name = {ka.display_name: ka for ka in w.knowledge_assistants.list_knowledge_assistants()}

results = []
errors = []

for key in KA_PROFILE_KEYS:
    cfg = PROFILES[key]
    display_name = f"{cfg['display_name_base']}{DISPLAY_NAME_SUFFIX}"
    try:
        ka = existing_by_name.get(display_name)

        if ka is None:
            print(f"[{key}] Creating Knowledge Assistant '{display_name}'...")
            ka = w.knowledge_assistants.create_knowledge_assistant(
                KnowledgeAssistant(
                    display_name=display_name,
                    description=cfg["description"],
                    instructions=cfg["instructions"],
                )
            )
        else:
            changed = [
                field
                for field, value in (("description", cfg["description"]), ("instructions", cfg["instructions"]))
                if getattr(ka, field) != value
            ]
            if changed:
                print(f"[{key}] Updating {changed} on existing '{display_name}'...")
                ka = w.knowledge_assistants.update_knowledge_assistant(
                    ka.name,
                    KnowledgeAssistant(
                        display_name=display_name,
                        description=cfg["description"],
                        instructions=cfg["instructions"],
                    ),
                    FieldMask(changed),
                )
            else:
                print(f"[{key}] '{display_name}' already up to date.")

        index_full_name = f"{CATALOG}.{SCHEMA}.{cfg['index_base']}{INDEX_SUFFIX}"
        existing_sources = list(w.knowledge_assistants.list_knowledge_sources(ka.name))
        matching_source = next(
            (s for s in existing_sources if s.index and s.index.index_name == index_full_name), None
        )
        if matching_source is None:
            other_indexes = [s.index.index_name for s in existing_sources if s.index]
            if other_indexes:
                print(f"[{key}] WARNING: existing source(s) point elsewhere {other_indexes}; adding {index_full_name} alongside them (not removing — could be intentional multi-source).")
            print(f"[{key}] Attaching index source {index_full_name}...")
            w.knowledge_assistants.create_knowledge_source(
                ka.name,
                KnowledgeSource(
                    display_name=f"{cfg['index_base']}{INDEX_SUFFIX}",
                    description=cfg["source_description"],
                    source_type="index",
                    index=IndexSpec(index_name=index_full_name, text_col=TEXT_COL, doc_uri_col=DOC_URI_COL),
                ),
            )
        else:
            print(f"[{key}] Index source already attached ({index_full_name}).")

        # update_permissions (PATCH) merges into the existing ACL rather than
        # replacing it, unlike set_permissions — required here since the same
        # KA is shared across qualibot-uat and qualibot-uat-test today, each
        # granting its own app's service principal (confirmed live 2026-08-24).
        acl = [
            KnowledgeAssistantAccessControlRequest(
                group_name="Role-Project-LEAP-CoreAdmin", permission_level=KnowledgeAssistantPermissionLevel.CAN_MANAGE
            ),
            KnowledgeAssistantAccessControlRequest(
                group_name="Role-Project-LEAP-CoreDev", permission_level=KnowledgeAssistantPermissionLevel.CAN_MANAGE
            ),
        ]
        for user in EXTRA_MANAGER_USERS:
            acl.append(
                KnowledgeAssistantAccessControlRequest(
                    user_name=user, permission_level=KnowledgeAssistantPermissionLevel.CAN_MANAGE
                )
            )
        if app_sp_client_id:
            acl.append(
                KnowledgeAssistantAccessControlRequest(
                    service_principal_name=app_sp_client_id, permission_level=KnowledgeAssistantPermissionLevel.CAN_QUERY
                )
            )
        kaid = ka.name.split("/")[1]
        w.knowledge_assistants.update_permissions(kaid, access_control_list=acl)

        # The KA ACL above doesn't reach the KA's serving endpoint for a human
        # or the app SP ("User does not have permission 'View' on Endpoint
        # ka-…", DEV 2026-10-05): with GRANT_ON_ENDPOINTS, grant there directly (merge).
        if GRANT_ON_ENDPOINTS and ka.endpoint_name:
            ep_acl = [
                ServingEndpointAccessControlRequest(
                    user_name=user, permission_level=ServingEndpointPermissionLevel.CAN_MANAGE
                )
                for user in EXTRA_MANAGER_USERS
            ]
            if app_sp_client_id:
                ep_acl.append(
                    ServingEndpointAccessControlRequest(
                        service_principal_name=app_sp_client_id,
                        permission_level=ServingEndpointPermissionLevel.CAN_QUERY,
                    )
                )
            try:
                endpoint_id = w.serving_endpoints.get(ka.endpoint_name).id
                w.serving_endpoints.update_permissions(endpoint_id, access_control_list=ep_acl)
                print(f"[{key}] endpoint {ka.endpoint_name}: CAN_MANAGE {EXTRA_MANAGER_USERS}, CAN_QUERY app SP {app_sp_client_id}")
            except Exception as e:
                errors.append((key, display_name, f"endpoint grant failed: {e}"))

        results.append((key, display_name, ka.endpoint_name))
    except Exception as e:
        errors.append((key, display_name, str(e)))

print("\n--- Summary ---")
for key, display_name, endpoint_name in results:
    print(f"[{key}] {display_name} -> endpoint_name={endpoint_name}")
if results:
    print("\nIf any endpoint_name above is new/changed, update the matching CHAT_ENDPOINT_* key in utils/deploy/target_env.json and redeploy the app.")
if errors:
    print("\nFailed:")
    for key, display_name, err in errors:
        print(f"  [{key}] {display_name}: {err}")
    raise RuntimeError(f"{len(errors)}/{len(KA_PROFILE_KEYS)} profile(s) failed — see log above.")

# COMMAND ----------
