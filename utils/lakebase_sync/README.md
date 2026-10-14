# lakebase_sync/

Outils qui sortent les données de la base Lakebase de l'application
(export JSON, copies Delta, mise à jour du schéma) et y entrent. Les réglages
de connexion communs (profils CLI, projet Lakebase, bases par environnement)
sont dans [`utils/ops_config.py`](../ops_config.py).

## Fichiers

| Fichier | Rôle | Lancé par |
|---|---|---|
| `migrations.py` | Liste ordonnée et idempotente des ajouts au schéma (`ADD COLUMN IF NOT EXISTS`, `CREATE TABLE IF NOT EXISTS`) | `migrate_lakebase.py`, `migrate_lakebase_job.py` |
| `migrate_lakebase.py` | CLI : applique les ajouts de schéma à une base, `--env DEV \| UAT \| UAT-TEST \| PROD` (défaut `DEV`) | à la main, à partir d'un poste |
| `migrate_lakebase_job.py` | Notebook : même chose dans un job. Par défaut, la base `doccompare_test` ; la base `doccompare` de l'UAT est refusée | jobs `migrate_lakebase_*` (`resources/lakebase_sync.yml`) |
| `export_lakebase_uat_to_volume_job.py` | Notebook : une table = un fichier JSON Lines dans le volume de staging | job `lakebase_export_uat_to_volume` |
| `import_chatbot_tables_to_uat_job.py` | Notebook : charge les tables du chat (`chat_*`, `errors`) du staging vers des tables Delta de l'UAT (schéma `qualibot`, catalogue `TARGET_CATALOG`) | deuxième tâche du job d'export UAT |
| `import_lakebase_uat_volume_to_dev_job.py` | Notebook : charge le staging dans des tables Delta de DEV, schéma `dev_proj.qualibot` (`TARGET_CATALOG` et `TARGET_SCHEMA` dans `resources/lakebase_sync.yml`) | job `lakebase_import_uat_to_dev` |
| `copy_Lakebase_tables.py` | CLI : crée dans `dev_proj.qualibot` une table Delta par table Lakebase de l'UAT | à la main, à partir d'un poste |
| `export_lakebase_to_json.py` | CLI : exporte toutes les tables d'une base en JSON Lines (`--profile`, `--database`, `--out-dir`) | étape « Sync prod data (1/2) » de `bitbucket-pipelines.yml` |
| `import_lakebase_from_json.py` | CLI : importe ces fichiers dans la base cible (`--profile`, `--database`, `--in-dir`). Pour chaque table, `DELETE` puis `INSERT` dans une seule transaction : un échec annule tout | étape « Sync prod data (2/2) » de `bitbucket-pipelines.yml` |

Les jobs sont définis dans `resources/lakebase_sync.yml`.

## Copier les tables de l'UAT vers DEV à partir d'un poste

Le poste de travail n'atteint pas Lakebase directement (port 5432 bloqué sur
le réseau d'entreprise). Le chemin par défaut passe par le volume de staging :

1. Lancer le job d'export de l'UAT (`lakebase_export_uat_to_volume`). Les
   étapes sont dans [OPERATIONS.md](../../OPERATIONS.md).
2. À la racine du dépôt, dans le `.venv` du projet (avec `psycopg2-binary`) :

   ```
   python utils/lakebase_sync/copy_Lakebase_tables.py
   ```

   Le script lit les fichiers exportés, puis crée ou remplace chaque table
   Delta de `dev_proj.qualibot`. Avec le proxy d'entreprise, installer
   `psycopg2-binary` avec `uv pip install --system-certs psycopg2-binary`.

L'option `--direct` lit Lakebase directement, sans passer par le volume. Elle
n'est utilisable que si le port 5432 est joignable à partir du poste.

## Mise à jour du schéma

`migrate_lakebase.py` s'exécute à partir d'un poste. Il se connecte avec
`generate_database_credential`, qui donne les droits DDL :

```
python utils/lakebase_sync/migrate_lakebase.py --env DEV
```

Une colonne ou une table ajoutée au schéma se déclare dans `migrations.py`.
Les ajouts sont idempotents : les relancer ne touche pas aux données
existantes.

## Environnements Lakebase

| Environnement | Profil CLI | Base | Note |
|---|---|---|---|
| `UAT` | `UAT` | `doccompare` | Base de l'application UAT |
| `UAT-TEST` | `UAT` | `doccompare_test` | Base jetable de la cible `qualibot-uat-test` |
| `DEV` | `DEV` | `doccompare` | Base de l'application DEV |
| `PROD` | `qualibot-prod` | `doccompare` | Base de l'application PROD |

Le projet Lakebase est `qualibot`, branche `production`, endpoint `primary`
pour les quatre.

## Points à savoir

- Les scripts de copie écrivent des tables. La cible de `copy_Lakebase_tables.py`
  est `COPY_TARGET_CATALOG` dans `utils/ops_config.py` ; celle des jobs est
  `TARGET_CATALOG` dans `resources/lakebase_sync.yml`.
- `import_lakebase_from_json.py` remplace le contenu des tables de la base
  cible. Il ne sert qu'à la synchronisation UAT vers PROD.
- Les JSON exportés contiennent les données des utilisateurs (questions,
  réponses, identifiants). Ne pas les déposer hors des volumes et dossiers de
  staging prévus.
