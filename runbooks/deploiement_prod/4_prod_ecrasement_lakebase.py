# Databricks notebook source
# PROD workspace, serverless, one cell. Overwrites the PROD Lakebase with the UAT export in one transaction.
# DRY_RUN = True runs every check and every write, then rolls back.
import json, os, subprocess, sys, datetime
try:
    import psycopg2
except ImportError:
    subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "psycopg2-binary"])
    import psycopg2
from databricks.sdk import WorkspaceClient

DRY_RUN = True
SRC = "/Volumes/prod_proj/qualibot/staging/lakebase_to_prod"
BRANCH = "projects/qualibot/branches/production"
DB = "doccompare"
TABLES = ["users", "chat_sessions", "chat_messages", "chat_feedbacks", "chat_turns", "chat_retrieved_chunks",
          "compare_messages", "compare_feedbacks", "impact_requests", "impact_document_results", "impact_feedbacks",
          "llm_requests", "app_errors"]
LEGACY = ["feedbacks", "messages", "errors"]
BATCH = 500
problems = []

def connect():
    w = WorkspaceClient()
    me = w.current_user.me().user_name
    if hasattr(w, "postgres"):
        ep = list(w.postgres.list_endpoints(parent=BRANCH))[0]
        host, token = ep.status.hosts.host, w.postgres.generate_database_credential(endpoint=ep.name).token
    else:
        ep = w.api_client.do("GET", f"/api/2.0/postgres/{BRANCH}/endpoints")["endpoints"][0]
        host = ep["status"]["hosts"]["host"]
        token = w.api_client.do("POST", "/api/2.0/postgres/credentials", body={"endpoint": ep["name"]})["token"]
    return me, psycopg2.connect(host=host, port=5432, dbname=DB, sslmode="require", user=me, password=token)

# ---- 1. Fichiers de l'export
manifest = json.load(open(f"{SRC}/manifest.json"))
print("Export du", datetime.datetime.fromtimestamp(os.path.getmtime(f"{SRC}/manifest.json")).isoformat(timespec="minutes"))
uat_keys = {}
for t in TABLES:
    keys, n = set(), 0
    with open(f"{SRC}/{t}.jsonl", encoding="utf-8") as fh:
        for line in fh:
            keys.update(json.loads(line)); n += 1
    uat_keys[t] = keys
    if n != manifest[t]:
        problems.append(f"{t}: fichier {n} lignes, manifest {manifest[t]}")

# ---- 2. Connexion, droits, colonnes
me, conn = connect()
cur = conn.cursor()
cur.execute("SELECT current_user, current_database()")
print("Connecté :", cur.fetchone(), "| mode :", "SIMULATION" if DRY_RUN else "ÉCRITURE")
cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE'")
existing = {r[0] for r in cur.fetchall()}
legacy = [t for t in LEGACY if t in existing]
prod_cols = {}
for t in TABLES:
    if t not in existing:
        problems.append(f"{t}: table absente en PROD"); continue
    cur.execute("SELECT column_name, data_type FROM information_schema.columns WHERE table_schema='public' AND table_name=%s", (t,))
    prod_cols[t] = dict(cur.fetchall())
    lost = sorted(uat_keys[t] - set(prod_cols[t]))
    if lost:
        problems.append(f"{t}: colonnes UAT absentes en PROD {lost}")
for t in TABLES + legacy:
    if t not in existing:
        continue
    cur.execute("SELECT has_table_privilege(%s,'SELECT') AND has_table_privilege(%s,'INSERT') AND has_table_privilege(%s,'DELETE')", (t, t, t))
    if not cur.fetchone()[0]:
        problems.append(f"{t}: droits SELECT/INSERT/DELETE manquants pour {me}")
    if t in prod_cols and prod_cols[t].get("id") in ("integer", "bigint"):
        cur.execute("SELECT pg_get_serial_sequence(%s, 'id')", (t,))
        seq = cur.fetchone()[0]
        if seq:
            cur.execute("SELECT has_sequence_privilege(%s, 'UPDATE')", (seq,))
            if not cur.fetchone()[0]:
                problems.append(f"{t}: droit UPDATE manquant sur {seq}")
if problems:
    conn.close()
    print("ARRÊT, rien n'a été touché :"); [print(" -", p) for p in problems]
    raise SystemExit(1)
print("Contrôles OK : fichiers complets, colonnes présentes, droits suffisants.")

# ---- 3. Écrasement (une transaction)
def insert(t, cols, rows):
    collist = ", ".join(f'"{c}"' for c in cols)
    cur.execute(f'INSERT INTO "{t}" ({collist}) SELECT {collist} FROM json_populate_recordset(NULL::"{t}", %s::json)', (json.dumps(rows),))

try:
    for t in legacy + list(reversed(TABLES)):
        cur.execute(f'DELETE FROM "{t}"')
    for t in TABLES:
        cols, rows = sorted(uat_keys[t]), []
        with open(f"{SRC}/{t}.jsonl", encoding="utf-8") as fh:
            for line in fh:
                rows.append(json.loads(line.replace("/Volumes/uat_proj/", "/Volumes/prod_proj/")))
                if len(rows) >= BATCH:
                    insert(t, cols, rows); rows = []
        if rows:
            insert(t, cols, rows)
        if prod_cols[t].get("id") in ("integer", "bigint"):
            cur.execute(f"SELECT setval(pg_get_serial_sequence('\"{t}\"', 'id'), GREATEST((SELECT max(id) FROM \"{t}\"), 1))")
    # ---- 4. Vérification avant validation
    print(f"{'table':24}{'PROD':>8}{'export':>8}  dernière ligne")
    for t in TABLES:
        date_col = "updated_at" if "updated_at" in prod_cols[t] else ("created_at" if "created_at" in prod_cols[t] else None)
        cur.execute(f'SELECT count(*), {f"max({date_col})::text" if date_col else "NULL"} FROM "{t}"')
        n, last = cur.fetchone()
        if n != manifest[t]:
            problems.append(f"{t}: {n} lignes après écriture, {manifest[t]} attendues")
        print(f"{t:24}{n:8}{manifest[t]:8}  {(last or '')[:16]}")
    for t in ("compare_messages", "chat_messages", "impact_requests"):
        cur.execute(f"SELECT count(*) FROM \"{t}\" x WHERE x::text LIKE '%%uat_proj%%'")
        n = cur.fetchone()[0]
        if n:
            problems.append(f"{t}: {n} lignes pointent encore vers uat_proj")
    for t in legacy:
        cur.execute(f'SELECT count(*) FROM "{t}"')
        print(f"ancienne table {t}: {cur.fetchone()[0]} ligne(s)")
    if problems:
        raise RuntimeError("; ".join(problems))
    if DRY_RUN:
        conn.rollback()
        print("SIMULATION OK : tout est annulé. Passer DRY_RUN = False et relancer.")
    else:
        conn.commit()
        print("TERMINÉ : Lakebase PROD = export UAT.")
except Exception:
    conn.rollback()
    print("ERREUR : tout est annulé, rien n'a changé.")
    raise
finally:
    conn.close()
