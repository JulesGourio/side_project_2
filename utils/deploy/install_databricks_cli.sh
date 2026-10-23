#!/usr/bin/env bash
# Installs the Databricks CLI at the version pinned in utils/deploy/databricks_cli_version.txt
# (one line, e.g. 0.280.0); an empty file installs the latest release. Does nothing when the CLI is present.
#
# USAGE: bash utils/deploy/install_databricks_cli.sh   (from the repository root or anywhere)
set -euo pipefail

cd "$(dirname "$0")/../.."

CLI_VERSION="$(tr -d '[:space:]' < utils/deploy/databricks_cli_version.txt 2>/dev/null || true)"
if ! command -v databricks >/dev/null; then
    apt-get update -qq && apt-get install -y -qq curl unzip >/dev/null
    if [ -n "$CLI_VERSION" ]; then
        curl -fsSL "https://github.com/databricks/cli/releases/download/v${CLI_VERSION}/databricks_cli_${CLI_VERSION}_linux_amd64.zip" -o /tmp/databricks_cli.zip
        unzip -q -o /tmp/databricks_cli.zip -d /usr/local/bin databricks
        chmod +x /usr/local/bin/databricks
    else
        echo "WARNING: no pinned Databricks CLI version (utils/deploy/databricks_cli_version.txt is empty): installing the latest." >&2
        curl -fsSL https://raw.githubusercontent.com/databricks/setup-cli/main/install.sh | sh
    fi
fi
