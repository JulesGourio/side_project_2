"""Dry-run : prévisualise la réconciliation par contenu SANS écrire ni sync.

Réutilise le JSONL déjà uploadé dans le volume UAT (relance l'export si absent).
Affiche : chunk_id affectés, lignes supprimées (UAT) et réinsérées (DEV = à
ré-embedder), par id.
"""
import sys

from databricks.sdk import WorkspaceClient

import resync_uat_index as R

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

PATH = f"{R.UAT_STAGING_VOLUME}/{R.STAGING_FNAME}"


def main():
    w = WorkspaceClient(profile=R.UAT_PROFILE)

    # S'assure que le JSONL est présent dans le volume (sinon export + upload).
    try:
        R.run_sql(w, R.UAT_WAREHOUSE_ID, f"SELECT 1 FROM json.`{PATH}` LIMIT 1")
        print(f"JSONL déjà présent : {PATH}")
    except Exception:
        print("JSONL absent — export DEV + upload...")
        n = R.export_dev_chunks(R.EXPORT_FILE)
        print(f"  {n} chunks exportés.")
        R.ensure_staging_volume(w)
        R.upload_to_uat_volume(R.EXPORT_FILE)

    tgt = R.target_columns(w, R.UAT_WAREHOUSE_ID, R.UAT_CHUNKS_TABLE)
    common = [n for n, _ in tgt if n in R.COLUMNS]
    type_of = dict(tgt)
    source = R._source_subquery(common, type_of, PATH)
    h = R._row_hash(common)
    K = R.MERGE_KEY

    sql = f"""
    WITH s AS (SELECT `{K}` AS id, {h} AS h FROM {source}),
         t AS (SELECT `{K}` AS id, {h} AS h FROM {R.UAT_CHUNKS_TABLE}),
         affected AS (
           SELECT DISTINCT id FROM (
             (SELECT id, h FROM s EXCEPT ALL SELECT id, h FROM t)
             UNION ALL
             (SELECT id, h FROM t EXCEPT ALL SELECT id, h FROM s)
           )
         )
    SELECT
      (SELECT count(*) FROM affected) AS affected_ids,
      (SELECT count(*) FROM {R.UAT_CHUNKS_TABLE} WHERE `{K}` IN (SELECT id FROM affected)) AS rows_deleted_uat,
      (SELECT count(*) FROM {source} WHERE `{K}` IN (SELECT id FROM affected)) AS rows_inserted_dev
    """
    r = R.run_sql(w, R.UAT_WAREHOUSE_ID, sql)
    cols = [c.name for c in r.manifest.schema.columns]
    stats = dict(zip(cols, r.result.data_array[0]))
    print("\n=== Aperçu réconciliation (lecture seule) ===")
    for k in ("affected_ids", "rows_deleted_uat", "rows_inserted_dev"):
        print(f"    {k:18}: {stats[k]}")
    print(f"\n    → chunks À RÉ-EMBEDDER (= rows_inserted_dev) : {stats['rows_inserted_dev']}")
    print("    (Aucune écriture ni sync_index — dry-run.)")


if __name__ == "__main__":
    main()
