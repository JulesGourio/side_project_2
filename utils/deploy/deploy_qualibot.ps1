param(
    [string]$AppEnv   = 'uat',
    [switch]$SkipBuild,         # Skip the frontend build (Python-only changes)
    [switch]$SyncOnly,          # Upload the code only: no frontend build, no app start or redeploy
                                # (notebooks reading the synced code)
    [switch]$Infra              # Full bundle deploy via terraform (app ACLs, UC volume bindings);
                                # needs MANAGE on the target catalog
)

$ErrorActionPreference = 'Stop'

# Bundle target, app name and CLI profile per environment. The app's env overrides
# are in target_env.json, rendered by render_target_config_env.py.
$Targets = @{
    dev        = @{ Target = 'dev';               AppName = 'qualibot';           Profile = 'DEV' }
    uat        = @{ Target = 'qualibot-uat';      AppName = 'qualibot';           Profile = 'UAT' }
    prod       = @{ Target = 'qualibot-prod';     AppName = 'qualibot';           Profile = 'qualibot-prod' }
    'uat-test' = @{ Target = 'qualibot-uat-test'; AppName = 'qualibot-uat-test';  Profile = 'UAT' }
}

if (-not $Targets.ContainsKey($AppEnv)) {
    Write-Host "Unknown environment '$AppEnv'. Use: dev | uat | prod | uat-test" -ForegroundColor Red
    exit 1
}

$Target  = $Targets[$AppEnv].Target
$AppName = $Targets[$AppEnv].AppName
$Profile = $Targets[$AppEnv].Profile

Write-Host ""
Write-Host "=== QualiBOT deploy  env=$AppEnv  target=$Target  profile=$Profile ===" -ForegroundColor Cyan
Write-Host ""

$ProjectRoot = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
Push-Location $ProjectRoot

try {
    # 1. Build frontend (skip with -SkipBuild for Python-only changes)
    if ($SkipBuild -or $SyncOnly) {
        Write-Host "[1/4] Frontend build skipped (-SkipBuild)" -ForegroundColor DarkGray
    } else {
        Write-Host "[1/4] Building frontend..." -ForegroundColor Yellow
        Push-Location client
        try {
            & bun install
            if ($LASTEXITCODE -ne 0) { throw "bun install failed (exit $LASTEXITCODE)" }
            & bun run build
            if ($LASTEXITCODE -ne 0) { throw "bun run build failed (exit $LASTEXITCODE)" }
        } finally {
            Pop-Location
        }
    }

    # 2. Render target_config.env from target_env.json (it overrides app.yaml's defaults,
    # is gitignored and is uploaded with the code) and push the source files.
    Write-Host "[2/4] Writing target_config.env and uploading source files..." -ForegroundColor Yellow
    & python utils/deploy/render_target_config_env.py $AppEnv target_config.env
    if ($LASTEXITCODE -ne 0) { throw "render_target_config_env.py failed (exit $LASTEXITCODE)" }

    # The prod bundle lives outside /Workspace/Shared: Databricks warns when a bundle root there is writable by all users.
    $BundleRoot = if ($AppEnv -eq 'prod') { '/Workspace/.bundle' } else { '/Workspace/Shared/.bundle' }
    $SourceCodePath = "$BundleRoot/qualibot/$Target/files"

    if ($Infra) {
        # Grants the app's service principal USE CATALOG, which requires MANAGE on the catalog for the deploying user.
        Write-Host "      -Infra: deploying bundle resources via terraform (needs catalog MANAGE)..." -ForegroundColor Yellow
        & databricks bundle deploy --target $Target --profile $Profile
        if ($LASTEXITCODE -ne 0) { throw "databricks bundle deploy failed (exit $LASTEXITCODE)" }
    } else {
        # Upload only: no terraform, so catalog and volume permissions stay untouched.
        # --include forces target_config.env (gitignored); the excludes match the bundle's sync excludes.
        & databricks sync . $SourceCodePath --profile $Profile `
            --include 'target_config.env' `
            --exclude 'client/src/**' `
            --exclude 'client/public/**' `
            --exclude 'client/node_modules/**' `
            --exclude 'runbooks/**' `
            --exclude 'archive/**' `
            --exclude 'files_to_compare/**' `
            --exclude 'debug_results/**' `
            --exclude 'debug_images/**' `
            --exclude 'lakebase_export/**' `
            --exclude '**/*.ipynb' `
            --exclude '.databricks/**' `
            --exclude '.playwright-mcp/**' `
            --exclude 'Intraqual_files/**' `
            --exclude 'intraqual_downloads/**' `
            --exclude 'intraqual_downloads_missing/**' `
            --exclude 'intraqual_missing_staged/**' `
            --exclude 'intraqual_extracted/**' `
            --exclude 'user_feedbacks/**' `
            --exclude 'test_prompt/**' `
            --exclude '**/*.xlsx' `
            --exclude '**/*.xls' `
            --exclude '**/*.zip'
        if ($LASTEXITCODE -ne 0) { throw "databricks sync failed (exit $LASTEXITCODE)" }
    }

    # 3. A deploy to a stopped app fails ("not in RUNNING state"); the nightly jobs of databricks.yml
    # stop the apps, so the compute state is checked first.
    # qualibot-uat-test is never auto-started: it is a disposable test app.
    if ($SyncOnly) {
        Write-Host ""
        Write-Host "Code synced to $SourceCodePath (-SyncOnly: app not restarted, frontend not rebuilt)." -ForegroundColor Green
        return
    }

    Write-Host "[3/4] Checking app compute state..." -ForegroundColor Yellow
    $appInfo = & databricks apps get $AppName --profile $Profile -o json | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0) { throw "databricks apps get failed (exit $LASTEXITCODE)" }
    $computeState = $appInfo.compute_status.state
    if ($computeState -in @('ACTIVE', 'STARTING')) {
        Write-Host "      $AppName compute is $computeState - no action needed." -ForegroundColor DarkGray
    } elseif ($AppEnv -eq 'uat-test') {
        throw "$AppName compute is $computeState. This app is never auto-started by a script - start it manually first: databricks apps start $AppName --profile $Profile"
    } else {
        Write-Host "      $AppName compute is $computeState - starting it (waits until active)..." -ForegroundColor Yellow
        & databricks apps start $AppName --profile $Profile
        if ($LASTEXITCODE -ne 0) { throw "databricks apps start failed (exit $LASTEXITCODE)" }
    }

    # Create a new app deployment so the app restarts and picks up the newly synced files.
    Write-Host "[4/4] Deploying app (waiting for restart to complete)..." -ForegroundColor Yellow
    & databricks apps deploy $AppName --source-code-path $SourceCodePath --auto-approve --profile $Profile
    if ($LASTEXITCODE -ne 0) { throw "databricks apps deploy failed (exit $LASTEXITCODE)" }

    Write-Host ""
    Write-Host "QualiBOT ($AppEnv / app=$AppName) deployed and running. See target_config.env for the env overrides applied." -ForegroundColor Green

} finally {
    Pop-Location
}
