#!/usr/bin/env bash
# One Bitbucket step: write the job-runner profile for an environment, then deploy the bundle.
#
# USAGE: bash utils/deploy/bitbucket_deploy.sh <dev|uat|prod> <app|jobs|setup>
#   app   : validate, deploy, then restart the app (bundle run doc-compare)
#   jobs  : validate, deploy; the app keeps running its current code
#   setup : install the Databricks CLI and write the profile only
#
# Needs DATABRICKS_TOKEN from the Bitbucket deployment environment of the step;
# DATABRICKS_HOST is optional and defaults to the workspace below.
set -euo pipefail

usage="usage: bitbucket_deploy.sh <dev|uat|prod> <app|jobs|setup>"
ENV_NAME="${1:?$usage}"
MODE="${2:?$usage}"

case "$MODE" in
    app | jobs | setup) ;;
    *) echo "ERROR: unknown mode '$MODE'. $usage" >&2; exit 1 ;;
esac

case "$ENV_NAME" in
    dev)
        BUNDLE_TARGET=dev
        PROFILE=job-runner-sa-dev
        HOST_DEFAULT=https://dbc-c623749d-731b.cloud.databricks.com
        DEPLOY_ENV_LABEL=dev
        ;;
    uat)
        BUNDLE_TARGET=uat
        PROFILE=job-runner-sa-uat
        HOST_DEFAULT=https://dbc-3a17bfce-9e88.cloud.databricks.com
        DEPLOY_ENV_LABEL=uat
        ;;
    prod)
        BUNDLE_TARGET=prod
        PROFILE=job-runner-sa-prod
        HOST_DEFAULT=https://dbc-77b87d5a-6bcb.cloud.databricks.com
        DEPLOY_ENV_LABEL=production
        ;;
    *) echo "ERROR: unknown environment '$ENV_NAME'. $usage" >&2; exit 1 ;;
esac

# Bitbucket runs this from the clone root; the bundle and the render script are relative to it.
cd "$(dirname "$0")/../.."

bash utils/deploy/install_databricks_cli.sh
databricks version

if [ -z "${DATABRICKS_TOKEN:-}" ]; then
    echo "ERROR: DATABRICKS_TOKEN is missing (Bitbucket deployment environment '$DEPLOY_ENV_LABEL')." >&2
    exit 1
fi

cat > "$HOME/.databrickscfg" <<EOF
[$PROFILE]
host  = ${DATABRICKS_HOST:-$HOST_DEFAULT}
token = ${DATABRICKS_TOKEN}
EOF
chmod 600 "$HOME/.databrickscfg"
databricks current-user me -p "$PROFILE"

if [ "$MODE" = setup ]; then
    exit 0
fi

# The frontend is not versioned: it is built here, and the bundle sync ships client/out (a deploy that lacked it would delete it from the app's files).
# Bun is pinned so that the build tool does not change between two deployments; the installer is downloaded to a file, then run.
BUN_VERSION="1.4.2"
if ! command -v bun >/dev/null; then
    curl -fsSL https://bun.sh/install -o /tmp/bun_install.sh
    bash /tmp/bun_install.sh "bun-v${BUN_VERSION}"
    export PATH="$HOME/.bun/bin:$PATH"
fi
(cd client && bun install --frozen-lockfile && bun run build)

python3 utils/deploy/render_target_config_env.py "$ENV_NAME"
databricks bundle validate -t "$BUNDLE_TARGET" -p "$PROFILE"
databricks bundle deploy   -t "$BUNDLE_TARGET" -p "$PROFILE"

if [ "$MODE" = app ]; then
    databricks bundle run doc-compare -t "$BUNDLE_TARGET" -p "$PROFILE"
fi
