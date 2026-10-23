#!/usr/bin/env bash
# Installs Bun at the version pinned in utils/deploy/bun_version.txt, from the release archive (no installer script is run)
# and checks it against the release's SHA-256 list. Does nothing when bun is already on the PATH.
#
# USAGE: bash utils/deploy/install_bun.sh   (from the repository root or anywhere)
set -euo pipefail

cd "$(dirname "$0")/../.."

BUN_VERSION="$(tr -d '[:space:]' < utils/deploy/bun_version.txt)"
if [ -z "$BUN_VERSION" ]; then
    echo "ERROR: utils/deploy/bun_version.txt is empty." >&2
    exit 1
fi

if ! command -v bun >/dev/null; then
    apt-get update -qq && apt-get install -y -qq curl unzip >/dev/null
    base="https://github.com/oven-sh/bun/releases/download/bun-v${BUN_VERSION}"
    archive="bun-linux-x64.zip"
    curl -fsSL "${base}/${archive}" -o "/tmp/${archive}"
    curl -fsSL "${base}/SHASUMS256.txt" -o /tmp/bun_SHASUMS256.txt
    expected="$(grep " ${archive}\$" /tmp/bun_SHASUMS256.txt | cut -d' ' -f1)"
    actual="$(sha256sum "/tmp/${archive}" | cut -d' ' -f1)"
    if [ -z "$expected" ] || [ "$expected" != "$actual" ]; then
        echo "ERROR: checksum of ${archive} does not match the release list." >&2
        exit 1
    fi
    unzip -q -o "/tmp/${archive}" -d /tmp/bun_unzipped
    install -m 0755 /tmp/bun_unzipped/bun-linux-x64/bun /usr/local/bin/bun
fi
bun --version
