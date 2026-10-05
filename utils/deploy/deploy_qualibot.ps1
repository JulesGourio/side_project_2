param(
    [string]$AppEnv   = 'uat',
    [switch]$SkipBuild,         # Pass -SkipBuild to skip frontend rebuild (Python-only changes)
    [switch]$Infra              # Pass -Infra to (re)deploy bundle resources via terraform
                                # (app ACLs, UC volume bindings). Requires MANAGE on the
                                # target catalog. Only needed when resources/permissions
                                # change — routine code deploys do NOT need it.
)

$ErrorActionPreference = 'Stop'

# Deploy orchestration metadata only (bundle target / app name / CLI profile).
# App-facing env var overrides (COMPARE_VOLUME_PATH, CHAT_ENDPOINT*, model
# endpoints, etc.) live in utils/deploy/target_env.json — the single source
# of truth shared with bitbucket-pipelines.yml, rendered by
# render_target_config_env.py below. Don't hardcode them here too.
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
    if ($SkipBuild) {
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

    # 2. Generate per-env config (from target_env.json) and push source files
    # to the workspace. target_config.env overrides app.yaml's defaults; it is
    # gitignored and uploaded separately — not committed.
    Write-Host "[2/4] Writing target_config.env and uploading source files..." -ForegroundColor Yellow
    & python utils/deploy/render_target_config_env.py $AppEnv target_config.env
    if ($LASTEXITCODE -ne 0) { throw "render_target_config_env.py failed (exit $LASTEXITCODE)" }

    # qualibot-prod's root_path is /Workspace/.bundle (not /Workspace/Shared/.bundle
    # like the other targets) -- avoids the "writable by all workspace users" warning.
    $BundleRoot = if ($AppEnv -eq 'prod') { '/Workspace/.bundle' } else { '/Workspace/Shared/.bundle' }
    $SourceCodePath = "$BundleRoot/qualibot/$Target/files"

    if ($Infra) {
        # Full bundle deploy: (re)applies app ACLs + UC volume bindings via terraform.
        # This grants the app's service principal USE CATALOG on the target catalog,
        # which REQUIRES the deploying user to have MANAGE on that catalog. Reserve
        # for the first deploy or when resources/permissions actually change.
        Write-Host "      -Infra: deploying bundle resources via terraform (needs catalog MANAGE)..." -ForegroundColor Yellow
        & databricks bundle deploy --target $Target --profile $Profile
        if ($LASTEXITCODE -ne 0) { throw "databricks bundle deploy failed (exit $LASTEXITCODE)" }
    } else {
        # Routine code deploy: just upload the source files. No terraform, so it
        # never touches catalog/volume permissions and works without admin rights.
        # --include force-uploads target_config.env (gitignored); the --exclude set
        # mirrors the bundle's sync excludes so we skip frontend source and caches.
        & databricks sync . $SourceCodePath --profile $Profile `
            --include 'target_config.env' `
            --exclude 'client/src/**' `
            --exclude 'client/public/**' `
            --exclude 'client/node_modules/**' `
            --exclude 'files_to_compare/**' `
            --exclude 'debug_results/**' `
            --exclude 'debug_images/**' `
            --exclude 'lakebase_export/**' `
            --exclude '**/*.ipynb' `
            --exclude 'utils/dev/**' `
            --exclude '.databricks/**' `
            --exclude '.playwright-mcp/**' `
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
            --exclude 'utils/deploy/_test_direct_head.py' `
            --exclude 'utils/deploy/_test_docview_head.py' `
            --exclude 'user_feedbacks/**' `
            --exclude 'test_prompt/**' `
            --exclude '**/*.xlsx' `
            --exclude '**/*.xls' `
            --exclude '**/*.zip'
        if ($LASTEXITCODE -ne 0) { throw "databricks sync failed (exit $LASTEXITCODE)" }
    }

    # 3. Ensure the app compute is running before deploying (dev included: its
    # off-hours jobs exist but are PAUSED). Off-hours jobs
    # (databricks.yml: apps_stop_nightly_uat) stop qualibot-uat-test nightly
    # (21h Paris, never restarted by a job) and qualibot
    # over the weekend (Fri 21h -> Mon 7h). Deploying to a stopped app fails
    # outright ("Cannot deploy app ... as it is not in RUNNING state").
    #
    # qualibot-uat-test must NEVER be auto-started by a script (disposable
    # test app - being stopped is its normal/intended state outside of an
    # active manual test session). qualibot (uat) and prod are fine to
    # auto-start here, since they're expected to be up during business hours
    # anyway and a deploy shouldn't be blocked by the off-hours job's timing.
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
