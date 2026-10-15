# dev_copy

One-shot scripts, run by hand and never deployed: nothing in the app or in a bundle job imports or runs them. This folder copies the chunk tables and the parsing state of `uat_proj.qualibot` into a staging volume, from which the DEV environment is loaded. The index is then built on the DEV side, so the DEV parsing pipeline does not need a full run.
## Files

| File | Kind | What it does |
|---|---|---|
| `export_uat_to_staging.py` | Databricks notebook (import it into the UAT workspace) | Copies the tables listed in `TABLES` to `OUTPUT_DIR`, copies the LibreOffice archive and writes `manifest.json` |
| `export_uat_submit.json` | Data file (body of `jobs submit`) | One `notebook_task` that runs the notebook above; its `notebook_path` is the path the notebook is imported to |

## How to run

Run from the repository root, with the CLI profile `UAT`. The import path must be the `notebook_path` of `export_uat_submit.json`:

```powershell
databricks workspace import /Users/jules.gourio.external@latecoere.aero/qualibot_dev_copy/export_uat_to_staging --file runbooks/scripts/dev_copy/export_uat_to_staging.py --format SOURCE --language PYTHON --overwrite --profile UAT
databricks jobs submit --json @runbooks/scripts/dev_copy/export_uat_submit.json --profile UAT
```

## Inputs

- Widgets (defaults): `SOURCE_CATALOG_SCHEMA` = `uat_proj.qualibot`, `TABLE_SUFFIX` = empty, `TABLES` = `chunks,parsing_checkpoint,parsed_documents,image_metadata,parse_manifest,category_reference`, `OUTPUT_DIR` = `/Volumes/uat_proj/qualibot/staging/dev_copy`, `FORMAT` = `delta` (or `parquet`), `SOFFICE_ARCHIVE` = `/Volumes/uat_proj/qualibot/doc_compare/libreoffice/libreoffice-25.8.7-linux-x64-v3.tar.gz`.
- Tables `<SOURCE_CATALOG_SCHEMA>.<table><TABLE_SUFFIX>` for each name in `TABLES`. They are only read.
- The LibreOffice archive named by `SOFFICE_ARCHIVE`.

## Outputs

- `<OUTPUT_DIR>/tables/<table>`: one folder per exported table, with the suffix dropped.
- `<OUTPUT_DIR>/libreoffice/<archive file name>`.
- `<OUTPUT_DIR>/manifest.json`: source, format and row count of each table, read back by the DEV import.

Each run overwrites the tables and the manifest in `OUTPUT_DIR`.

## Before you run

- `OUTPUT_DIR` must be a Unity Catalog volume path (it must start with `/Volumes/`), otherwise the notebook stops.
- The table `chunks` must exist in the source schema, otherwise the notebook stops before the manifest is written.
- The import path must match `notebook_path` in `export_uat_submit.json`, otherwise the submitted run uses a different notebook.
