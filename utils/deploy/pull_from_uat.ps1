param(
    [string]$AppEnv = 'uat-test'
)

# On-demand UAT -> local pull. Brings down whatever is currently in the
# workspace source folder (e.g. edits made directly in the Databricks UI)
# into a local staging folder for review — it does NOT touch your live
# working tree directly, so nothing gets silently overwritten.
#
# After running, diff/copy what you actually want from .uat_pull/ into the
# real project files by hand.

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
$PullDir = Join-Path $ProjectRoot '.uat_pull'

Write-Host ""
Write-Host "=== Pulling $SourceCodePath -> $PullDir (profile=$Profile) ===" -ForegroundColor Cyan
Write-Host ""

& databricks workspace export-dir $SourceCodePath $PullDir --overwrite --profile $Profile
if ($LASTEXITCODE -ne 0) { throw "databricks workspace export-dir failed (exit $LASTEXITCODE)" }

Write-Host ""
Write-Host "Done. Review changes in $PullDir and copy over what you want to keep." -ForegroundColor Green
Write-Host "Diff example:  git diff --no-index server\routers\chat.py .uat_pull\server\routers\chat.py" -ForegroundColor DarkGray
