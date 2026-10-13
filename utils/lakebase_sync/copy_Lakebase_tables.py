"""Copie les tables Lakebase (UAT) vers Databricks DEV.

USAGE :
    1. (Par défaut) Lancez d'abord le job Databricks UAT 'lakebase_sync/export_lakebase_uat_to_volume.py'.
       Puis lancez ce script :
       python utils/lakebase_sync/copy_Lakebase_tables.py

    2. (Mode direct - si port 5432 ouvert) :
       python utils/lakebase_sync/copy_Lakebase_tables.py --direct
"""

import argparse
import os
import json
import subprocess
import sys
import time
import psycopg2
import psycopg2.extras
from decimal import Decimal
from pathlib import Path
import datetime
from databricks.sdk import WorkspaceClient
from databricks.sdk.service.sql import StatementState, Disposition, Format

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # utils/ — shared config.py
from ops_config import (
    lakebase_connect,
    COPY_SOURCE_ENV,
    COPY_TARGET_PROFILE,
    COPY_TARGET_CATALOG,
    COPY_TARGET_SCHEMA,
    COPY_STAGING_CATALOG,
    COPY_STAGING_SCHEMA,
    COPY_STAGING_VOLUME,
    COPY_TABLES_TO_SKIP,
    LAKEBASE,
    WAREHOUSE_ID,
)

STAGING_VOLUME_DBFS = f"dbfs:/Volumes/{COPY_STAGING_CATALOG}/{COPY_STAGING_SCHEMA}/{COPY_STAGING_VOLUME}"
STAGING_VOLUME_SQL  = f"/Volumes/{COPY_STAGING_CATALOG}/{COPY_STAGING_SCHEMA}/{COPY_STAGING_VOLUME}"

# Volume alimenté par export_lakebase_uat_to_volume.py (job Databricks, compute UAT).
UAT_SOURCE_PROFILE          = LAKEBASE[COPY_SOURCE_ENV]["profile"]
LAKEBASE_EXPORT_VOLUME_DBFS = "dbfs:/Volumes/uat_landingzone/qualibot/staging/lakebase_export"

OUTPUT_DIR = "lakebase_export"

# Fichier JSONL (hors Lakebase) à copier tel quel vers une table Delta,
# dans le même catalogue/schéma cible que les tables Lakebase.
INTRAQUAL_JSONL = os.path.join(os.path.dirname(__file__), "..", "intraqual_docs.jsonl")
INTRAQUAL_TABLE = "intraqual_docs"

TERMINAL_STATES = {
    StatementState.SUCCEEDED,
    StatementState.FAILED,
    StatementState.CANCELED,
    StatementState.CLOSED,
}


def json_serializer(obj):
    if isinstance(obj, (datetime.date, datetime.datetime)):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, (bytes, memoryview)):
        return obj.hex() if isinstance(obj, bytes) else bytes(obj).hex()
    return str(obj)


# ── Lakebase helpers ───────────────────────────────────────────────────────────

def list_tables(cursor, schemas: list) -> list:
    if schemas:
        cursor.execute(
            """
            SELECT table_schema, table_name
            FROM information_schema.tables
            WHERE table_schema = ANY(%s) AND table_type = 'BASE TABLE'
            ORDER BY table_schema, table_name
            """,
            (schemas,),
        )
    else:
        cursor.execute(
            """
            SELECT table_schema, table_name
            FROM information_schema.tables
            WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
              AND table_type = 'BASE TABLE'
            ORDER BY table_schema, table_name
            """
        )
    return cursor.fetchall()


def export_table_to_json(conn, schema: str, table: str) -> tuple[str, int]:
    """Exporte une table Lakebase en JSON local. Retourne (chemin, nb_lignes)."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(f'SELECT * FROM "{schema}"."{table}"')
        rows = [dict(row) for row in cur.fetchall()]

    out_dir = os.path.join(OUTPUT_DIR, schema)
    os.makedirs(out_dir, exist_ok=True)
    out_file = os.path.join(out_dir, f"{table}.json")

    # Format JSON Lines (1 objet par ligne) requis par Databricks SQL json.`path`
    with open(out_file, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, default=json_serializer) + "\n")

    return out_file, len(rows)


def list_uat_export_files() -> list[str]:
    """Liste les JSON déjà exportés par export_lakebase_uat_to_volume.py sur le volume UAT."""
    result = subprocess.run(
        ["databricks", "fs", "ls", LAKEBASE_EXPORT_VOLUME_DBFS,
         "--profile", UAT_SOURCE_PROFILE, "--output", "json"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Listing du volume UAT échoué :\n{result.stderr.strip()}")
    return sorted(e["name"] for e in json.loads(result.stdout) if e["name"].endswith(".json"))


def download_from_uat_volume(fname: str) -> tuple[str, int]:
    """Télécharge un JSON déjà exporté par export_lakebase_uat_to_volume.py. Retourne (chemin, nb_lignes)."""
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    local_file = os.path.join(OUTPUT_DIR, fname)
    result = subprocess.run(
        ["databricks", "fs", "cp", "--profile", UAT_SOURCE_PROFILE, "--overwrite",
         f"{LAKEBASE_EXPORT_VOLUME_DBFS}/{fname}", local_file],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Téléchargement échoué pour {fname} :\n{result.stderr.strip()}")
    n_rows = sum(1 for _ in open(local_file, encoding="utf-8"))
    return local_file, n_rows


# ── Databricks helpers ─────────────────────────────────────────────────────────

def wait_for_statement(w: WorkspaceClient, response) -> None:
    while response.status.state not in TERMINAL_STATES:
        time.sleep(3)
        response = w.statement_execution.get_statement(response.statement_id)
    if response.status.state != StatementState.SUCCEEDED:
        err = response.status.error.message if response.status.error else "inconnu"
        raise RuntimeError(f"SQL échoué ({response.status.state}): {err}")


def upload_to_staging(local_file: str, table: str) -> str:
    """Upload le fichier JSON vers le volume de staging via la CLI Databricks."""
    staging_path = f"{STAGING_VOLUME_DBFS}/{table}.json"
    result = subprocess.run(
        [
            "databricks", "fs", "cp",
            "--profile", COPY_TARGET_PROFILE,
            "--overwrite",
            local_file,
            staging_path,
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Upload échoué pour {table}:\n{result.stderr.strip()}")
    return f"{STAGING_VOLUME_SQL}/{table}.json"


def create_or_replace_table(w: WorkspaceClient, table: str, volume_path: str) -> None:
    """Crée ou écrase la table Delta dans le catalogue cible depuis le JSON stagé."""
    target = f"`{COPY_TARGET_CATALOG}`.`{COPY_TARGET_SCHEMA}`.`{table}`"
    sql = f"CREATE OR REPLACE TABLE {target} USING DELTA AS SELECT * FROM json.`{volume_path}`"

    response = w.statement_execution.execute_statement(
        warehouse_id=WAREHOUSE_ID,
        statement=sql,
        wait_timeout="0s",
        disposition=Disposition.INLINE,
        format=Format.JSON_ARRAY,
    )
    wait_for_statement(w, response)


# ── Main ───────────────────────────────────────────────────────────────────────

import logging

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("copy_Lakebase_tables")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument(
        "--direct", action="store_true",
        help="Connexion psycopg2 directe à Lakebase (nécessite un accès réseau direct au port 5432, "
             "indisponible depuis le réseau entreprise). Par défaut, récupère le JSON déjà exporté "
             "par export_lakebase_uat_to_volume.py (job Databricks, à lancer avant ce script).",
    )
    args = ap.parse_args()

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    exported = []

    if args.direct:
        # ── Étape 1 (legacy) : export Lakebase en direct ───────────────────────
        logger.info(f"=== [1/2] Export depuis Lakebase {COPY_SOURCE_ENV} (connexion directe) ===")
        conn = lakebase_connect(COPY_SOURCE_ENV)
        try:
            with conn.cursor() as cur:
                tables = list_tables(cur, [])

            if not tables:
                logger.info("Aucune table trouvée dans Lakebase.")
                return

            logger.info(f"{len(tables)} table(s) trouvée(s) : {[f'{s}.{t}' for s, t in tables]}\n")

            for schema, table in tables:
                if table in COPY_TABLES_TO_SKIP:
                    logger.info(f"  Export {schema}.{table}... ignorée (COPY_TABLES_TO_SKIP)")
                    continue
                local_file, n_rows = export_table_to_json(conn, schema, table)
                exported.append((schema, table, local_file, n_rows))
                logger.info(f"  Export {schema}.{table}: {n_rows} lignes")
        finally:
            conn.close()
    else:
        # ── Étape 1 : récupération du JSON déjà exporté sur le volume UAT ──────
        logger.info(f"=== [1/2] Récupération de l'export Lakebase {COPY_SOURCE_ENV} depuis {LAKEBASE_EXPORT_VOLUME_DBFS} ===")
        files = list_uat_export_files()
        if not files:
            logger.info(f"Aucun fichier trouvé sur {LAKEBASE_EXPORT_VOLUME_DBFS}.\n"
                "Lancer d'abord le job Databricks export_lakebase_uat_to_volume.py sur le workspace UAT.")
            return

        logger.info(f"{len(files)} fichier(s) trouvé(s) : {files}\n")
        for fname in files:
            table = fname[:-len(".json")]
            if table in COPY_TABLES_TO_SKIP:
                logger.info(f"  {table}... ignorée (COPY_TABLES_TO_SKIP)")
                continue
            local_file, n_rows = download_from_uat_volume(fname)
            exported.append(("public", table, local_file, n_rows))
            logger.info(f"  {table}: {n_rows} lignes")

    # ── JSONL supplémentaire (Intraqual) ──────────────────────────────────────
    if os.path.exists(INTRAQUAL_JSONL):
        n_rows = sum(1 for _ in open(INTRAQUAL_JSONL, encoding="utf-8"))
        exported.append(("(jsonl)", INTRAQUAL_TABLE, INTRAQUAL_JSONL, n_rows))
        logger.info(f"  JSONL {INTRAQUAL_TABLE} depuis {INTRAQUAL_JSONL}: {n_rows} lignes")
    else:
        logger.info(f"  JSONL introuvable ({INTRAQUAL_JSONL}) — étape ignorée.")

    # ── Étape 2 : push vers Databricks DEV ────────────────────────────────────
    logger.info(f"=== [2/2] Push vers {COPY_TARGET_CATALOG}.{COPY_TARGET_SCHEMA} (profil {COPY_TARGET_PROFILE}) ===")
    w = WorkspaceClient(profile=COPY_TARGET_PROFILE)

    for schema, table, local_file, n_rows in exported:
        if n_rows == 0:
            logger.info(f"  {table}... ignorée (0 lignes)")
            continue
        volume_path = upload_to_staging(local_file, table)
        create_or_replace_table(w, table, volume_path)
        logger.info(f"OK ({n_rows} lignes)  ->  {COPY_TARGET_CATALOG}.{COPY_TARGET_SCHEMA}.{table}")

    logger.info(f"Terminé. {len(exported)} table(s) copiée(s) dans {COPY_TARGET_CATALOG}.{COPY_TARGET_SCHEMA}.")


if __name__ == "__main__":
    main()
