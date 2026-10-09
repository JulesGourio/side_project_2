param(
    [string]$AppEnv = 'uat-test'
)

# Continuous local -> UAT push. Every local file save is uploaded within a
# few seconds via `databricks sync --watch` — no need to re-run
# deploy_qualibot.ps1 for every edit while iterating.
#
# IMPORTANT: this only pushes files. The running app process does NOT pick
# up new code by itself (Databricks Apps only reload on redeploy) — for
# Python/frontend changes to actually take effect in the app, still run
#   .\utils\deploy\deploy_qualibot.ps1 -AppEnv uat-test -SkipBuild
# This watch mode is mainly useful for iterating on notebooks/scripts you
# run manually in the workspace, or to stage code before that redeploy.
#
# Ctrl+C to stop.

$ErrorActionPreference = 'Stop'

$Targets = @{
    'uat-test' = @{ Target = 'qualibot-uat-test'; Profile = 'UAT' }
    'uat'      = @{ Target = 'qualibot-uat';      Profile = 'UAT' }
}

if (-not $Targets.ContainsKey($AppEnv)) {
    Write-Host "Unknown environment '$AppEnv'. Use: uat | uat-test" -ForegroundColor Red
    exit 1
}

$Target  = $Targets[$AppEnv].Target
$Profile = $Targets[$AppEnv].Profile
$SourceCodePath = "/Workspace/Shared/.bundle/qualibot/$Target/files"

$ProjectRoot = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
Push-Location $ProjectRoot

try {
    Write-Host ""
    Write-Host "=== Watching for local changes -> $SourceCodePath (profile=$Profile) ===" -ForegroundColor Cyan
    Write-Host "Ctrl+C to stop. Remember: redeploy the app to pick up code changes." -ForegroundColor DarkGray
    Write-Host ""

    & databricks sync . $SourceCodePath --profile $Profile --watch `
        --exclude 'client/src/**' `
        --exclude 'client/public/**' `
        --exclude 'client/node_modules/**' `
        --exclude 'files_to_compare/**' `
        --exclude 'debug_results/**' `
        --exclude 'debug_images/**' `
        --exclude '**/*.ipynb' `
        --exclude 'utils/dev/**' `
        --exclude '.databricks/**' `
        --exclude '.playwright-mcp/**' `
        --exclude '.uat_pull/**' `
        --exclude 'Intraqual_files/**' `
        --exclude 'intraqual_downloads/**' `
        --exclude 'intraqual_downloads_missing/**' `
        --exclude 'intraqual_missing_staged/**' `
        --exclude 'intraqual_extracted/**' `
        --exclude 'intraqual_*.csv' `
        --exclude 'intraqual_*.json' `
        --exclude 'utils/deploy/intraqual_*.csv' `
        --exclude 'utils/deploy/.intraqual_profile/**' `
        --exclude 'utils/deploy/download_intraqual.py' `
        --exclude 'utils/deploy/scrap_ids_intraqual.py' `
        --exclude 'utils/deploy/unzip_intraqual.py' `
        --exclude 'utils/deploy/upload_intraqual.py' `
        --exclude 'user_feedbacks/**' `
        --exclude 'test_prompt/**' `
        --exclude '**/*.xlsx' `
        --exclude '**/*.xls' `
        --exclude '**/*.zip'
} finally {
    Pop-Location
}
