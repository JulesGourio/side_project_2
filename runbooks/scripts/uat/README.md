# uat

One-shot notebook, run by hand and never deployed: nothing in the app or in a bundle job imports or runs it. It creates the project schema of the `qualibot-uat-test` target: the volume `images_test` and the parsing state tables `*_test`, deep-cloned from the source schema, then grants read access. See the index in [`runbooks/README.md`](../../README.md).

## Files

| File | Kind | What it does |
|---|---|---|
| `setup_proj_schema.py` | Databricks notebook (import it into the UAT workspace) | Checks the identity and the target schema, creates the volumes, deep-clones the parsing state tables, grants `SELECT` and `READ VOLUME` |

## How to run

Run the import from the repository root, with the CLI profile of the identity `job-runner-sa-uat`, then run the imported notebook from the workspace with its widgets:

```
databricks workspace import <workspace path> --file runbooks/scripts/uat/setup_proj_schema.py --format SOURCE --language PYTHON --overwrite --profile job-runner-sa-uat
```

Widgets (defaults):

- `target_catalog_schema` = `uat_proj.qualibot`, `target_suffix` = `_test`
- `source_catalog_schema` = `uat_landingzone.qualibot`, `source_suffix` = `_v1` (read only)
- `tables` = `parsing_checkpoint,parsed_documents,image_metadata,parse_manifest,category_reference`
- `volumes` = `images_test`
- `reader_groups` = `Role-Project-LEAP-CoreAdmin,Role-Project-LEAP-CoreDev`
- `overwrite` = `false` (`true` replaces the existing target tables)

Two source tables have other names: `parsing_checkpoint` is read from `_pipeline_checkpoint` and `parsed_documents` from `processed_files`, in the source schema.

## Inputs and outputs

- Input: the source tables `<source_catalog_schema>.<name><source_suffix>`, read only. A missing source table is skipped.
- Output: the tables `<target_catalog_schema>.<name><target_suffix>`, deep clones of the source tables, with a 60-day retention of deleted files and logs. The row count of each clone must equal its source, otherwise the notebook stops.
- Output: the volumes `<target_catalog_schema>.<volume>` and the grants of `SELECT` on the tables and `READ VOLUME` on the volumes to each group in `reader_groups`. A failed grant is listed, and the notebook stops at the end; the tables and volumes stay in place.
- An existing target table is kept unless `overwrite` is `true`. A second run with the defaults changes nothing.

## Before you run

- The notebook runs as `job-runner-sa-uat`: the tables and volumes it creates are owned by that identity.
- The target schema `uat_proj.qualibot` exists: the notebook stops when it cannot describe it.
- The source tables are readable by that identity.
