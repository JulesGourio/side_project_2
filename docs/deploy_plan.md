# Deploy plan — qualibot-custom

Target workspace: `https://dbc-c623749d-731b.cloud.databricks.com` (CLI profile `latecoere`)
App name: `qualibot-custom`
Source path: `/Workspace/Shared/qualibot-custom`

Status: **not deployed** — plan only.

## Preflight (2026-10-05)

| Item | State | Status |
|---|---|---|
| App `qualibot-custom` | does not exist | ✅ |
| Source folder `/Workspace/Shared/qualibot-custom` | does not exist | ✅ |
| Frontend build `client/out/` | present | ✅ |
| `databricks-claude-sonnet-4-6` | READY | ✅ |
| `databricks-gpt-5-6-luna` | READY | ✅ |
| KA `ka-df2b7829-endpoint` | READY, set as `CHAT_ENDPOINT` in `app.yaml` | ✅ |
| CAN_QUERY for the app SP on the KA | to grant after app creation (you have CAN_MANAGE) | ⚠️ |
| Vector Search index `uat_landingzone.qualibot.chunks_index_v1` | not accessible on this workspace | ignored |
| Lakebase project `qualibot` | absent | ignored |

## Expected behaviour

- Compare (diff, summary): works
- Compare impact search: fails (no index) — out of scope
- Chat: works through `ka-df2b7829-endpoint` (knowledge sources not verified)
- History / sessions / caches: none (no Lakebase; app falls back after a 5s timeout)
- Access gating: fail-open without Lakebase — anyone with CAN_USE on the app gets Compare + Chat

## Steps

```bash
P="-p latecoere"

# 1. Create the app
databricks $P apps create qualibot-custom

# 2. Upload source
databricks $P sync . /Workspace/Shared/qualibot-custom

# 3. Grant the app service principal CAN_QUERY on the KA endpoint
SP=$(databricks $P apps get qualibot-custom -o json | python3 -c 'import sys,json;print(json.load(sys.stdin)["service_principal_client_id"])')
databricks $P serving-endpoints update-permissions 830562edaffd4656965dfbefc3659bb4 --json \
  "{\"access_control_list\":[{\"service_principal_name\":\"$SP\",\"permission_level\":\"CAN_QUERY\"}]}"

# 4. Deploy
databricks $P apps deploy qualibot-custom --source-code-path /Workspace/Shared/qualibot-custom
```

## Verify

- `databricks $P apps get qualibot-custom` → `app_status.state = RUNNING`
- Open the app URL, run one Compare and one Chat question
- App logs: `Starting in production mode`, Lakebase warning expected
