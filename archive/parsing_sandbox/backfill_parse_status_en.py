"""Backfill legacy French parse_status values to their English equivalents.

2_Cleanup_Volume.py used to write these French parse_status literals into
processed_files (NOT parse_manifest — parse_manifest has no parse_status
column at all; these statuses are only ever logged against processed_files):

    SKIPPED_REF_HORS_PERIMETRE  -> SKIPPED_REF_OUT_OF_SCOPE
    SKIPPED_IDDOC_INTROUVABLE   -> SKIPPED_IDDOC_NOT_FOUND
    SKIPPED_REF_MANUELLE        -> SKIPPED_REF_MANUAL

The pipeline code now only ever writes the new English values. This script
backfills any already-persisted rows still holding an old French value, on a
given catalog.schema's processed_files{suffix} table.

USAGE
-----
Dry run (default) — just prints counts of rows that would be updated:

    python utils/parsing_pipeline/backfill_parse_status_en.py \\
        --catalog-schema dev_lab.lab_jules --warehouse-id <id> --profile DEV

Apply the backfill for real:

    python utils/parsing_pipeline/backfill_parse_status_en.py \\
        --catalog-schema uat_landingzone.qualibot --table-suffix _test \\
        --warehouse-id <id> --profile UAT --apply
"""
from __future__ import annotations

import argparse

from databricks.sdk import WorkspaceClient

OLD_TO_NEW = {
    "SKIPPED_REF_HORS_PERIMETRE": "SKIPPED_REF_OUT_OF_SCOPE",
    "SKIPPED_IDDOC_INTROUVABLE": "SKIPPED_IDDOC_NOT_FOUND",
    "SKIPPED_REF_MANUELLE": "SKIPPED_REF_MANUAL",
}


def _run(w: WorkspaceClient, warehouse_id: str, statement: str):
    resp = w.statement_execution.execute_statement(
        warehouse_id=warehouse_id, statement=statement, wait_timeout="50s",
    )
    if resp.status and resp.status.state and resp.status.state.value != "SUCCEEDED":
        raise SystemExit(f"Query did not succeed: {resp.status.state} {getattr(resp.status, 'error', '')}")
    return (resp.result.data_array if resp.result else None) or []


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--catalog-schema", required=True,
                     help="e.g. dev_lab.lab_jules or uat_landingzone.qualibot")
    ap.add_argument("--table-suffix", default="", help="e.g. _test (default: empty)")
    ap.add_argument("--profile", default="DEV", help="Databricks CLI profile")
    ap.add_argument("--warehouse-id", required=True, help="SQL warehouse ID to run the statements on")
    ap.add_argument("--apply", action="store_true",
                     help="Actually run the UPDATEs. Without this flag, only a dry-run count is printed.")
    args = ap.parse_args()

    table = f"{args.catalog_schema}.processed_files{args.table_suffix}"
    w = WorkspaceClient(profile=args.profile)

    old_values = ", ".join(f"'{v}'" for v in OLD_TO_NEW)
    dry_run_sql = (
        f"SELECT parse_status, COUNT(*) AS n FROM {table} "
        f"WHERE parse_status IN ({old_values}) GROUP BY parse_status"
    )
    print(f"Table: {table}\n")
    print(dry_run_sql)
    rows = _run(w, args.warehouse_id, dry_run_sql)
    if not rows:
        print("\nNo rows found with a legacy French parse_status value.")
    else:
        print("\nparse_status                    count")
        for parse_status, n in rows:
            print(f"{parse_status:<32} {n}")

    if not args.apply:
        print("\nDry run only — pass --apply to run the UPDATEs.")
        return

    print("\nApplying backfill...")
    for old, new in OLD_TO_NEW.items():
        update_sql = f"UPDATE {table} SET parse_status = '{new}' WHERE parse_status = '{old}'"
        print(f"  {update_sql}")
        _run(w, args.warehouse_id, update_sql)
    print("\nDone.")


if __name__ == "__main__":
    main()
