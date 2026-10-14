# oneshot/

Scripts ponctuels : migrations de schéma, copies de tables, empaquetage et
outils de contrôle lancés à la main, une fois ou rarement. Aucun job de bundle
ne les lance, et ce dossier n'est jamais déployé : `.databricksignore`,
`databricks.yml` et `utils/deploy/deploy_qualibot.ps1` l'excluent (`oneshot/**`).
Les scripts utilisés par l'application, par un job ou par un déploiement sont
dans [`utils/`](../utils/README.md).

Toutes les commandes se lancent depuis la racine du dépôt. Les scripts Python
en ligne de commande importent `utils/ops_config.py` quand ils en ont besoin.

## Carte du dossier

| Fichier | Rôle | Quand | Comment le lancer |
|---|---|---|---|
| [`dev_copy/export_uat_to_staging.py`](dev_copy/export_uat_to_staging.py) | Notebook, workspace UAT : copie les tables `chunks` et l'état du parsing de `uat_proj.qualibot` vers le volume `staging/dev_copy`, avec l'archive LibreOffice. Lecture seule sur les tables UAT | Pour amorcer la copie du corpus dans DEV (la suite est `utils/dev_copy/import_staging_to_dev_job.py`) | Voir ci-dessous |
| [`dev_copy/export_uat_submit.json`](dev_copy/export_uat_submit.json) | Corps de la commande `jobs submit` qui lance le notebook précédent | Avec le notebook | Voir ci-dessous |
| [`lakebase/copy_Lakebase_tables.py`](lakebase/copy_Lakebase_tables.py) | CLI : crée ou remplace dans `dev_proj.qualibot` une table Delta par table Lakebase de l'UAT, à partir de l'export du volume (ou en direct avec `--direct`) | Après le job `lakebase_export_uat_to_volume` | `python oneshot/lakebase/copy_Lakebase_tables.py` ; `python oneshot/lakebase/copy_Lakebase_tables.py --direct` (port 5432 requis) |
| [`lakebase/migrate_lakebase.py`](lakebase/migrate_lakebase.py) | CLI : applique les changements de `migrations.py` à une base Lakebase (`--env DEV \| UAT \| UAT-TEST \| PROD`, défaut `DEV`) | Après l'ajout d'une colonne ou d'une table dans `migrations.py` | `python oneshot/lakebase/migrate_lakebase.py --env DEV` |
| [`lakebase/migrate_lakebase_job.py`](lakebase/migrate_lakebase_job.py) | Notebook : même chose dans le workspace. Par défaut la base `doccompare_test` ; la base `doccompare` de l'UAT est refusée | Quand la migration doit passer par le workspace | Voir ci-dessous |
| [`lakebase/migrations.py`](lakebase/migrations.py) | Liste ordonnée et idempotente des changements de schéma (`ADD COLUMN IF NOT EXISTS`, `CREATE TABLE IF NOT EXISTS`, `DROP TABLE IF EXISTS`), et la fonction `apply_migrations` | Importé par les deux scripts de migration | Aucune (module) |
| [`uat/setup_proj_schema.py`](uat/setup_proj_schema.py) | Notebook, workspace UAT, identité `job-runner-sa-uat` : crée le volume du pipeline, clone en profond les tables d'état du parsing `<source>.*_v1` (widget `source_catalog_schema`) vers `uat_proj.qualibot.*_test`, puis accorde la lecture | Pour la cible `qualibot-uat-test` (répétition du basculement UAT vers le nouveau chatbot) | Voir ci-dessous |
| [`soffice/package_libreoffice.py`](soffice/package_libreoffice.py) | CLI : construit l'archive LibreOffice portable (tar.gz) utilisée par l'aperçu « Exact (PDF) » à partir du bundle TDF déjà téléchargé | Pour reconstruire l'archive de LibreOffice | `python oneshot/soffice/package_libreoffice.py <bundle.tar.gz> <sortie.tar.gz>`, puis `databricks fs cp` vers le volume |
| [`soffice/scan_needed_libs.py`](soffice/scan_needed_libs.py) | CLI : liste les bibliothèques partagées qu'un arbre LibreOffice demande et qui ne sont ni dans l'arbre ni dans une image glibc de base | Pour vérifier la liste de `package_libreoffice.py` | `python oneshot/soffice/scan_needed_libs.py <dossier_extrait>` |
| [`compare/compare_preview.py`](compare/compare_preview.py) | CLI : rapport hors ligne d'une paire de documents (extraction, diff, images, découpage), sans appel au modèle | Pour contrôler une modification du moteur de comparaison sur de vrais documents | `python oneshot/compare/compare_preview.py old.docx new.docx` (options : `--method`, `--dump-diff`, `--threshold`) |

## Commandes des notebooks

Les notebooks ne se lancent pas depuis le poste : il faut d'abord les importer
dans le workspace, au chemin voulu, puis les lancer avec leurs paramètres.

**Copie UAT vers DEV** (depuis la racine du dépôt, profil `UAT`). Le chemin du notebook est celui de `export_uat_submit.json` :

```
databricks workspace import <chemin du notebook UAT, identique au notebook_path de export_uat_submit.json> --file oneshot/dev_copy/export_uat_to_staging.py --format SOURCE --language PYTHON --overwrite --profile UAT
databricks jobs submit --json @oneshot/dev_copy/export_uat_submit.json --profile UAT
```

**Migration Lakebase depuis le workspace** (le notebook lit ses paramètres dans des widgets) :

```
databricks workspace import <chemin workspace> --file oneshot/lakebase/migrate_lakebase_job.py --format SOURCE --language PYTHON --overwrite --profile <profil>
```

Lancer ensuite le notebook importé, avec `LAKEBASE_DATABASE` (défaut `doccompare_test`).

**Tables d'état du parsing pour `qualibot-uat-test`** (profil de l'identité `job-runner-sa-uat`) :

```
databricks workspace import <chemin workspace> --file oneshot/uat/setup_proj_schema.py --format SOURCE --language PYTHON --overwrite --profile job-runner-sa-uat
```

Lancer ensuite le notebook importé, avec ses widgets (`overwrite` vaut `false` par défaut : une table existante est gardée).

## Règles

- Rien ici n'est importé par `server/`, par `utils/` ni par un fichier de
  `resources/`. Un script qui doit être lancé par un job ou un déploiement
  reste dans `utils/`.
- Les notebooks de ce dossier portent la même convention de journalisation que
  `utils/` : `logger`, jamais `print`.
- Les valeurs par défaut (widgets, constantes) ne sont relues par aucun job :
  les vérifier avant de relancer un script.
