# Databricks notebook source
# One-shot. Workspace PROD, serverless. Identity: the PROD job runner b82d3b5a-b633-4738-8dde-68bb2142cb02
# (Workflows > Create job > task notebook = this notebook > Run as = that service principal > Run now).
# Replaces the content of the PROD Lakebase app tables with the UAT export copied to
# /Volumes/prod_proj/qualibot/staging/lakebase_to_prod. DRY_RUN = True only prints the counts.

# COMMAND ----------

# MAGIC %pip install -q --upgrade databricks-sdk "protobuf>=5.26.1,<6" psycopg2-binary

# COMMAND ----------

dbutils.library.restartPython()

# COMMAND ----------

import json, psycopg2
from databricks.sdk import WorkspaceClient

DRY_RUN = True   # True : affiche seulement les comptes. False : écrase PROD avec UAT.
SRC = "/Volumes/prod_proj/qualibot/staging/lakebase_to_prod"
OLD_TABLES = ["messages", "feedbacks", "errors", "knowledge_base_metadata"]   # reliquats PROD, supprimés
TABLES = ["users", "chat_sessions", "chat_messages", "chat_feedbacks", "chat_turns", "chat_retrieved_chunks",
          "compare_messages", "compare_feedbacks", "impact_requests", "impact_document_results", "impact_feedbacks",
          "llm_requests", "app_errors"]
BATCH = 500

w = WorkspaceClient()
endpoint = list(w.postgres.list_endpoints(parent="projects/qualibot/branches/production"))[0]
conn = psycopg2.connect(host=endpoint.status.hosts.host, port=5432, dbname="doccompare", sslmode="require",
                        user=w.current_user.me().user_name,
                        password=w.postgres.generate_database_credential(endpoint=endpoint.name).token)
cur = conn.cursor()
manifest = json.load(open(f"{SRC}/manifest.json"))
cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE'")
existing = {r[0] for r in cur.fetchall()}
missing = [t for t in TABLES if t not in existing]
if missing:
    raise RuntimeError(f"Tables absentes en PROD : {missing}. Lancer d'abord deploy-prod.")
for t in TABLES:
    cur.execute(f'SELECT count(*) FROM "{t}"')
    print(f"{t:26} PROD {cur.fetchone()[0]:6} -> UAT {manifest[t]:6}")
print("Anciennes tables supprimées :", [t for t in OLD_TABLES if t in existing])
if DRY_RUN:
    conn.close()
    dbutils.notebook.exit("Simulation : rien écrit. Passer DRY_RUN = False.")


def insert(t, cols, rows):
    collist = ", ".join(f'"{c}"' for c in cols)
    cur.execute(f'INSERT INTO "{t}" ({collist}) SELECT {collist} FROM json_populate_recordset(NULL::"{t}", %s::json)',
                (json.dumps(rows),))


try:
    for t in OLD_TABLES:
        cur.execute(f'DROP TABLE IF EXISTS "{t}" CASCADE')
    cur.execute("TRUNCATE " + ", ".join(f'"{t}"' for t in TABLES) + " RESTART IDENTITY CASCADE")
    for t in TABLES:
        cur.execute("SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name=%s", (t,))
        prod_cols = {r[0] for r in cur.fetchall()}
        cols, rows, n = None, [], 0
        with open(f"{SRC}/{t}.jsonl", encoding="utf-8") as fh:
            for line in fh:
                rec = json.loads(line.replace("/Volumes/uat_proj/", "/Volumes/prod_proj/"))
                if cols is None:
                    cols = [c for c in rec if c in prod_cols]
                rows.append(rec)
                if len(rows) >= BATCH:
                    insert(t, cols, rows); n += len(rows); rows = []
        if rows:
            insert(t, cols, rows); n += len(rows)
        if n != manifest[t]:
            raise RuntimeError(f"{t} : {n} lignes, {manifest[t]} attendues")
        if "id" in prod_cols:
            cur.execute(f"SELECT setval(pg_get_serial_sequence('\"{t}\"', 'id'), GREATEST((SELECT max(id) FROM \"{t}\"), 1))")
        print(f"{t:26} {n} lignes écrites")
    conn.commit()
    print("Terminé : le Lakebase PROD contient les données d'UAT.")
except Exception:
    conn.rollback()
    print("Erreur : rien n'a été modifié.")
    raise
finally:
    conn.close()
