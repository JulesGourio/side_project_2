# Fusion DEV + UAT du parsing

Pour chaque document, le meilleur résultat de parsing de DEV et d'UAT est gardé des deux côtés : Docling avant XML, puis plus d'images, puis plus de texte. Pas de reparsing, pas d'appel LLM. Les notebooks ne touchent ni `chunks` ni l'index : le run incrémental final ne reconstruit que les documents changés. Chaque table modifiée est sauvegardée en `*_before_merge`.

Tout tourne avec ton compte. S'il manque un droit, le notebook s'arrête avant d'écrire et le dit. PowerShell : depuis la racine du dépôt.

## 1. Importer les 4 notebooks (DEV et UAT)

```powershell
foreach ($p in "DEV", "UAT") {
  databricks workspace mkdirs /Workspace/Shared/qualibot_fusion --profile $p
  Get-ChildItem runbooks\scripts\fusion_dev_uat -Filter "*_$p*.py" | ForEach-Object {
    databricks workspace import "/Workspace/Shared/qualibot_fusion/$($_.BaseName)" --file $_.FullName --format SOURCE --language PYTHON --overwrite --profile $p }
}
```

## 2. Exporter

- DEV : Run all [`1_DEV_export`](1_DEV_export.py)
- UAT : Run all [`1_UAT_export`](1_UAT_export.py)

## 3. Croiser les exports

```powershell
$w = "$env:TEMP\qualibot_fusion"; Remove-Item -Recurse -Force $w -ErrorAction SilentlyContinue
databricks fs cp -r dbfs:/Volumes/dev_proj/qualibot/staging/merge_export/dev "$w\dev" --profile DEV
databricks fs cp -r dbfs:/Volumes/uat_proj/qualibot/staging/merge_export/uat "$w\uat" --profile UAT
databricks fs rm -r dbfs:/Volumes/uat_proj/qualibot/staging/merge/dev --profile UAT 2>$null
databricks fs rm -r dbfs:/Volumes/dev_proj/qualibot/staging/merge/uat --profile DEV 2>$null
databricks fs cp -r "$w\dev" dbfs:/Volumes/uat_proj/qualibot/staging/merge/dev --profile UAT
databricks fs cp -r "$w\uat" dbfs:/Volumes/dev_proj/qualibot/staging/merge/uat --profile DEV
```

## 4. Fusionner

- DEV : Run all [`2_DEV_fusion`](2_DEV_fusion.py)
- UAT : Run all [`2_UAT_fusion`](2_UAT_fusion.py). Il affiche le plan sans rien écrire : envoie le tableau à Claude, puis mets `APPLY = True` en haut et relance.

## 5. Copier les images des documents changés (environ 1 h)

```powershell
$w = "$env:TEMP\qualibot_fusion\img"
foreach ($c in @(@("dev_proj","DEV","uat_proj","UAT","uat_images_from_dev"), @("uat_proj","UAT","dev_proj","DEV","dev_images_from_uat"))) {
  $from, $fp, $to, $tp, $list = $c
  $ids = (databricks fs cat "dbfs:/Volumes/$to/qualibot/staging/merge/$list.txt" --profile $tp) -split "`n" | ForEach-Object { $_.Trim() } | Where-Object { $_ }
  foreach ($id in $ids) {
    Remove-Item -Recurse -Force "$w\$id" -ErrorAction SilentlyContinue
    databricks fs cp -r "dbfs:/Volumes/$from/qualibot/images/$id" "$w\$id" --profile $fp
    databricks fs rm -r "dbfs:/Volumes/$to/qualibot/images/$id" --profile $tp 2>$null
    if (Test-Path "$w\$id") { databricks fs cp -r "$w\$id" "dbfs:/Volumes/$to/qualibot/images/$id" --profile $tp }
  }
  "OK $from -> $to : $($ids.Count) dossiers"
}
```

## 6. Reconstruire

Jobs > `D_1_Qualibot_Parsing_Pipeline` > Run now, paramètres par défaut (`incremental`) : DEV (job 362367007936662), puis UAT (job 762813135325407) hors heures d'usage.

## Retour arrière

`CREATE OR REPLACE TABLE <table> DEEP CLONE <table>_before_merge` pour `parsing_checkpoint`, `image_metadata` et `parsed_documents`.
