# Commandes CLI de la fusion DEV + UAT. Depuis la racine du depot :
#   .\runbooks\scripts\fusion_dev_uat\fusion.ps1 import      importe les notebooks dans DEV et UAT
#   .\runbooks\scripts\fusion_dev_uat\fusion.ps1 snapshots   copie les exports DEV <-> UAT
#   .\runbooks\scripts\fusion_dev_uat\fusion.ps1 images      copie les dossiers images des documents changes
param([Parameter(Mandatory)][ValidateSet("import", "snapshots", "images")][string]$Step)
$ErrorActionPreference = "Stop"
$Here = $PSScriptRoot
$Work = Join-Path $env:TEMP "qualibot_fusion"
$WorkspaceDir = "/Workspace/Shared/qualibot_fusion"

function Copy-Folder($From, $FromProfile, $To, $ToProfile, $Name) {
    $Local = Join-Path $Work $Name
    if (Test-Path $Local) { Remove-Item -Recurse -Force $Local }
    databricks fs cp -r $From $Local --profile $FromProfile
    if (-not (Test-Path $Local)) { return $false }
    $ErrorActionPreference = "Continue"
    databricks fs rm -r $To --profile $ToProfile 2>$null
    $ErrorActionPreference = "Stop"
    databricks fs cp -r $Local $To --profile $ToProfile
    return $true
}

New-Item -ItemType Directory -Force $Work | Out-Null

switch ($Step) {
    "import" {
        foreach ($p in "DEV", "UAT") {
            databricks workspace mkdirs $WorkspaceDir --profile $p
            Get-ChildItem (Join-Path $Here "notebooks") -Filter "*_$p*.py" | ForEach-Object {
                databricks workspace import "$WorkspaceDir/$($_.BaseName)" --file $_.FullName --format SOURCE --language PYTHON --overwrite --profile $p
            }
            Write-Host "OK $p : notebooks dans $WorkspaceDir"
        }
    }
    "snapshots" {
        Copy-Folder "dbfs:/Volumes/dev_proj/qualibot/staging/merge_export/dev" "DEV" "dbfs:/Volumes/uat_proj/qualibot/staging/merge/dev" "UAT" "snap_dev" | Out-Null
        Copy-Folder "dbfs:/Volumes/uat_proj/qualibot/staging/merge_export/uat" "UAT" "dbfs:/Volumes/dev_proj/qualibot/staging/merge/uat" "DEV" "snap_uat" | Out-Null
        databricks fs ls dbfs:/Volumes/uat_proj/qualibot/staging/merge/dev --profile UAT
        databricks fs ls dbfs:/Volumes/dev_proj/qualibot/staging/merge/uat --profile DEV
        Write-Host "OK : attendu 4 lignes par liste (2 tables, 2 fichiers rows_*.txt)"
    }
    "images" {
        $Plans = @(
            @{ List = "dbfs:/Volumes/uat_proj/qualibot/staging/merge/uat_images_from_dev.txt"; ListProfile = "UAT"; From = "dev_proj"; FromProfile = "DEV"; To = "uat_proj"; ToProfile = "UAT" },
            @{ List = "dbfs:/Volumes/dev_proj/qualibot/staging/merge/dev_images_from_uat.txt"; ListProfile = "DEV"; From = "uat_proj"; FromProfile = "UAT"; To = "dev_proj"; ToProfile = "DEV" }
        )
        foreach ($plan in $Plans) {
            $ids = (databricks fs cat $plan.List --profile $plan.ListProfile) -split "`n" | ForEach-Object { $_.Trim() } | Where-Object { $_ }
            $i = 0
            foreach ($id in $ids) {
                $i++
                Write-Progress -Activity "Images $($plan.From) -> $($plan.To)" -Status "$i / $($ids.Count)" -PercentComplete (100 * $i / $ids.Count)
                Copy-Folder "dbfs:/Volumes/$($plan.From)/qualibot/images/$id" $plan.FromProfile "dbfs:/Volumes/$($plan.To)/qualibot/images/$id" $plan.ToProfile "img_$($plan.To)_$id" | Out-Null
            }
            Write-Host "OK $($plan.From) -> $($plan.To) : $($ids.Count) dossiers"
        }
    }
}
