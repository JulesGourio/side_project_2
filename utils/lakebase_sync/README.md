# Lancer copy_Lakebase_tables.py

Ce script n'est pas lié aux dépendances de l'app (pas dans le `pyproject.toml` racine).
Il utilise le `.venv` du projet (géré par `uv`) + `psycopg2-binary` installé à part.

## Setup (une fois)

```powershell
cd C:\Users\L0041770\Desktop\GenAI\Qualibot\latec-compare
uv pip install --python .venv/Scripts/python.exe --system-certs psycopg2-binary
```

`--system-certs` est nécessaire ici (proxy d'entreprise / certificat non reconnu par uv sinon).

## Run

```powershell
cd C:\Users\L0041770\Desktop\GenAI\Qualibot\latec-compare
.venv\Scripts\python.exe utils\lakebase_sync\copy_Lakebase_tables.py
```

Prérequis : avoir lancé avant le job Databricks UAT `lakebase_sync/export_lakebase_uat_to_volume.py`
(le script lit le JSON déjà exporté sur le volume UAT, pas de connexion directe à Lakebase par défaut).

Option `--direct` : connexion psycopg2 directe à Lakebase (nécessite l'accès réseau au port 5432, indisponible depuis le réseau entreprise).

## Job planifié `import_lakebase_uat_volume_to_dev_job.py` — cible et déploiement

Ce job (`D_1_Qualibot_Lakebase_Import_Uat_To_Dev`, target `dev` de `databricks.yml`)
écrit dans **`dev_landingzone.qualibot`** — même catalog/schema que
`copy_Lakebase_tables.py` ci-dessus (un seul "dev" canonique). Les valeurs
viennent des `spark_env_vars` du job dans `databricks.yml`
(`targets.dev.resources.jobs.lakebase_import_uat_to_dev`) ; les
`os.getenv(..., "dev_landingzone")` / `"qualibot"` dans le script ne sont que
des filets de sécurité si ces env vars n'étaient pas injectées.

**Piège d'architecture — `dev` n'est jamais déployé par un script** :
`utils/deploy/deploy_qualibot.ps1` ne connaît que `uat` / `uat-test` / `prod`
dans sa table `$Targets` (aucune app QualiBOT ne tourne sur `dev`). Résultat :
modifier les `spark_env_vars` (ou n'importe quelle autre ressource) sous
`targets.dev` dans `databricks.yml` **ne change rien tant que personne ne
lance manuellement** :

```powershell
databricks bundle deploy --target dev --profile DEV
```

Vérifier après coup que le job a bien pris le changement :

```powershell
databricks jobs get <job_id> --profile DEV --output json
```

(chercher `job_id` via `databricks jobs list --profile DEV` — nom
`D_1_Qualibot_Lakebase_Import_Uat_To_Dev`).
