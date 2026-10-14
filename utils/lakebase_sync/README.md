# lakebase_sync/

Scripts qui sortent les données de la base Lakebase de l'application (export
vers un volume, export JSON) et qui les chargent ailleurs (tables Delta de
DEV, base PROD). Les réglages de connexion communs (profils CLI, projet
Lakebase, bases par environnement) sont dans
[`utils/ops_config.py`](../ops_config.py).

Les migrations du schéma et les copies ponctuelles de tables ne sont pas ici :
elles sont dans [`runbooks/scripts/lakebase/`](../../runbooks/scripts/README.md).

## Fichiers

| Fichier | Rôle | Lancé par |
|---|---|---|
| `export_lakebase_uat_to_volume_job.py` | Notebook : une table = un fichier JSON Lines dans le volume de staging. Paramètres (projet, branche, base, volume) passés par `base_parameters` | job `lakebase_export_uat_to_volume` (cible `qualibot-uat`) ; job `lakebase_export_dev_to_volume` (cible `dev`) |
| `import_chatbot_tables_to_uat_job.py` | Notebook : charge les tables du chat (`chat_*`, `errors`) du staging dans des tables Delta de l'UAT (schéma `qualibot`, catalogue `TARGET_CATALOG`) | deuxième tâche du job `lakebase_export_uat_to_volume` |
| `import_lakebase_uat_volume_to_dev_job.py` | Notebook : charge le staging dans des tables Delta de DEV, schéma `dev_proj.qualibot` (`TARGET_CATALOG` et `TARGET_SCHEMA` dans `resources/lakebase_sync.yml`) | job `lakebase_import_uat_to_dev` (cible `dev`) |
| `export_lakebase_to_json.py` | CLI : exporte toutes les tables d'une base en JSON Lines (`--profile`, `--database`, `--out-dir`) | étape « Sync prod data (1/2): export from UAT » de `bitbucket-pipelines.yml` |
| `import_lakebase_from_json.py` | CLI : importe ces fichiers dans la base cible (`--profile`, `--database`, `--in-dir`). Pour chaque table, `DELETE` puis `INSERT` dans une seule transaction : un échec annule tout | étape « Sync prod data (2/2): import to PROD » de `bitbucket-pipelines.yml` |

Les jobs sont définis dans `resources/lakebase_sync.yml`.

## Environnements Lakebase

Les environnements connus de `ops_config.py` (lus par `lakebase_connect`) :

| Environnement | Profil CLI | Base | Note |
|---|---|---|---|
| `UAT` | `UAT` | `doccompare` | Base de l'application UAT |
| `UAT-TEST` | `UAT` | `doccompare_test` | Base jetable de la cible `qualibot-uat-test` |
| `DEV` | `DEV` | `doccompare` | Base de l'application DEV |
| `PROD` | `qualibot-prod` | `doccompare` | Base de l'application PROD |

Le projet Lakebase est `qualibot`, branche `production`, endpoint `primary`
pour les quatre.

## Points à savoir

- `import_lakebase_from_json.py` remplace le contenu des tables de la base
  cible. Il ne sert qu'à la synchronisation UAT vers PROD.
- L'ordre d'import des tables est `MIGRATE_TO_PROD_TABLE_ORDER` dans
  `ops_config.py` (parents avant enfants). Les tables absentes de cette liste
  sont importées ensuite, dans l'ordre de l'export.
- Les JSON exportés contiennent les données des utilisateurs (questions,
  réponses, identifiants). Ne pas les déposer hors des volumes et dossiers de
  staging prévus.
