# Runbooks

Dossier de ce qu'un opérateur lance à la main : les étapes de déploiement et d'exploitation par environnement, la liste des retests et les scripts ponctuels. Rien ici n'est déployé : le dossier est exclu du bundle (`databricks.yml`, `.databricksignore`, `utils/deploy/deploy_qualibot.ps1`). Ce qui crée une ressource à partir du dépôt (bundle, `resources/*.yml`, notebooks des jobs, scripts de `utils/deploy/`) reste dans le code.

## Runbooks

| Fichier | Contenu |
|---|---|
| `dev.md` | Environnement DEV : copie de l'UAT dans `dev_proj.qualibot`, déploiement, index, évaluation, notation des traces, pipeline de parsing |
| `uat.md` | Environnement UAT et cible `qualibot-uat-test` : migration vers `uat_proj.qualibot`, validation, mise en service, bascule du chatbot, alertes |
| `prod.md` | Environnement PROD : `prod_proj.qualibot`, volumes, index, droits, déploiement sur accord |
| `retest.md` | Ce qui reste à rejouer ou retester après les derniers changements de code, avec la commande et le statut |
| `scripts/` | Scripts ponctuels : migrations de schéma, copie de tables, empaquetage de LibreOffice, contrôle hors ligne d'une comparaison (voir ci-dessous) |

## Règles

- Chaque étape donne toutes ses commandes, dans l'ordre (PowerShell, `databricks bundle run`, SQL). Aucune étape ne renvoie à une autre étape sans recopier ses commandes.
- Une étape marquée « sur accord » ne se lance pas sans accord explicite.
- Statut : `[ ]` à faire ; `[x] (jj/mm/aaaa)` fait et confirmé à la date indiquée.
- Profils CLI : `DEV` (DEV), `UAT` (UAT et `qualibot-uat-test`), `qualibot-prod` (PROD). `migrate_lakebase.py` accepte les environnements `DEV`, `UAT`, `UAT-TEST` et `PROD`.
- Les objets de l'application et du pipeline sont dans `<env>_proj.qualibot` (`qualibot-uat-test` : `uat_proj.qualibot`, suffixe `_test`). Seule source hors de ce schéma : le catalogue Intraqual `prod_landingzone.intraqual`.

## Où trouver une opération ponctuelle

- Copie du corpus UAT vers DEV : `dev.md` 00.3, 00.4 et bloc C.
- Export Lakebase vers le volume, chargement des tables de chat : `uat.md` étape 0.5 (UAT) et `dev.md` 00.6 (DEV).
- Validation de l'UAT sur `qualibot-uat-test` : `uat.md` bloc A.
- Parsing, index et métadonnées : `uat.md` blocs B et D ; `dev.md` bloc Q.
- Notation des traces et expérience MLflow : `dev.md` 0.8, 0.9 et 0.14.
- Scripts : tableau ci-dessous.

## Scripts (`scripts/`)

Scripts ponctuels : migrations de schéma, copies de tables, empaquetage et contrôles lancés à la main, une fois ou rarement. Aucun job de bundle ne les lance, et ce dossier n'est jamais déployé. Les scripts utilisés par l'application, par un job ou par un déploiement sont dans [`utils/`](../utils/README.md).

Toutes les commandes se lancent depuis la racine du dépôt. Les scripts Python en ligne de commande importent `utils/ops_config.py` quand ils en ont besoin.

| Fichier | Rôle | Comment le lancer |
|---|---|---|
| [`scripts/dev_copy/export_uat_to_staging.py`](scripts/dev_copy/export_uat_to_staging.py) | Notebook, workspace UAT. Copie les tables `chunks` et l'état du parsing de `uat_proj.qualibot` vers le volume `staging/dev_copy`, avec l'archive LibreOffice. Lecture seule sur les tables UAT. Amorce la copie du corpus dans DEV | Voir « Commandes des notebooks » |
| [`scripts/dev_copy/export_uat_submit.json`](scripts/dev_copy/export_uat_submit.json) | Corps de la commande `jobs submit` qui lance le notebook précédent | `databricks jobs submit --json @runbooks/scripts/dev_copy/export_uat_submit.json --profile UAT` |
| [`scripts/lakebase/copy_Lakebase_tables.py`](scripts/lakebase/copy_Lakebase_tables.py) | CLI : crée ou remplace dans `dev_proj.qualibot` une table Delta par table Lakebase de l'UAT, à partir de l'export du volume (ou en direct avec `--direct`, port 5432 requis) | Après le job `lakebase_export_uat_to_volume` : `python runbooks/scripts/lakebase/copy_Lakebase_tables.py` ; `python runbooks/scripts/lakebase/copy_Lakebase_tables.py --direct` |
| [`scripts/lakebase/migrate_lakebase.py`](scripts/lakebase/migrate_lakebase.py) | CLI : applique les changements de `migrations.py` à une base Lakebase (`--env DEV \| UAT \| UAT-TEST \| PROD`, défaut `DEV`) | `python runbooks/scripts/lakebase/migrate_lakebase.py --env DEV` |
| [`scripts/lakebase/migrate_lakebase_job.py`](scripts/lakebase/migrate_lakebase_job.py) | Notebook : même migration, depuis le workspace. Par défaut la base `doccompare_test` ; la base `doccompare` est refusée sur l'hôte UAT | Voir « Commandes des notebooks » |
| [`scripts/lakebase/migrations.py`](scripts/lakebase/migrations.py) | Liste ordonnée et idempotente des changements de schéma (`ADD COLUMN IF NOT EXISTS`, `CREATE TABLE IF NOT EXISTS`, `DROP TABLE IF EXISTS`) et la fonction `apply_migrations` | Module, importé par les deux scripts de migration : aucune commande |
| [`scripts/uat/setup_proj_schema.py`](scripts/uat/setup_proj_schema.py) | Notebook, workspace UAT, identité `job-runner-sa-uat`. Crée le volume `images_test` s'il manque, clone en profond les tables d'état du parsing (`_pipeline_checkpoint`, `processed_files`, `image_metadata`, `parse_manifest`, `category_reference`) de l'ancien schéma UAT (widget `source_catalog_schema`, lecture seule) vers `uat_proj.qualibot.*_test`, puis accorde la lecture | Voir « Commandes des notebooks » |
| [`scripts/soffice/package_libreoffice.py`](scripts/soffice/package_libreoffice.py) | CLI : construit l'archive LibreOffice portable (tar.gz) utilisée par l'aperçu « Exact (PDF) », à partir du bundle TDF déjà téléchargé | `python runbooks/scripts/soffice/package_libreoffice.py <bundle.tar.gz> <sortie.tar.gz>`, puis `databricks fs cp <sortie.tar.gz> dbfs:/Volumes/<catalog>/<schema>/<volume>/libreoffice/libreoffice-linux-x64.tar.gz` |
| [`scripts/soffice/scan_needed_libs.py`](scripts/soffice/scan_needed_libs.py) | CLI : liste les bibliothèques partagées qu'un arbre LibreOffice demande et qu'il ne fournit pas (ni l'arbre ni une image glibc de base ne les contiennent) | `python runbooks/scripts/soffice/scan_needed_libs.py <dossier_extrait>` |
| [`scripts/compare/compare_preview.py`](scripts/compare/compare_preview.py) | CLI : rapport hors ligne d'une paire de documents (extraction, diff, images, découpage), sans appel au modèle | `python runbooks/scripts/compare/compare_preview.py old.docx new.docx` ; options : `--method {standard,structured,comparative}`, `--dump-diff <fichier>`, `--threshold <valeur>` |

### Commandes des notebooks

Les notebooks ne se lancent pas depuis le poste : il faut d'abord les importer dans le workspace, au chemin voulu, puis les lancer avec leurs paramètres.

**Copie UAT vers DEV** (profil `UAT`, depuis la racine du dépôt). Le chemin du notebook doit être identique au `notebook_path` de `export_uat_submit.json` :

```powershell
databricks workspace mkdirs /Users/jules.gourio.external@latecoere.aero/qualibot_dev_copy --profile UAT
databricks workspace import /Users/jules.gourio.external@latecoere.aero/qualibot_dev_copy/export_uat_to_staging --file runbooks/scripts/dev_copy/export_uat_to_staging.py --format SOURCE --language PYTHON --overwrite --profile UAT
databricks jobs submit --json @runbooks/scripts/dev_copy/export_uat_submit.json --profile UAT
```

**Migration Lakebase depuis le workspace** (le notebook lit ses paramètres dans des widgets) :

```powershell
databricks workspace import <chemin workspace> --file runbooks/scripts/lakebase/migrate_lakebase_job.py --format SOURCE --language PYTHON --overwrite --profile <profil>
```

Lancer ensuite le notebook importé. Widgets : `LAKEBASE_DATABASE` (défaut `doccompare_test` ; `doccompare` est refusée sur l'hôte UAT), `LAKEBASE_PROJECT_ID`, `LAKEBASE_BRANCH`, `LAKEBASE_ENDPOINT`.

**Tables d'état du parsing pour `qualibot-uat-test`** (profil de l'identité `job-runner-sa-uat`) :

```powershell
databricks workspace import <chemin workspace> --file runbooks/scripts/uat/setup_proj_schema.py --format SOURCE --language PYTHON --overwrite --profile job-runner-sa-uat
```

Lancer ensuite le notebook importé, avec ses widgets. Les valeurs par défaut sont celles de la répétition `qualibot-uat-test` ; `overwrite` vaut `false` (une table existante est gardée).

### Règles des scripts

- Rien ici n'est importé par `server/`, par `utils/` ni par un fichier de `resources/`. Un script qui doit être lancé par un job ou un déploiement reste dans `utils/`.
- Les notebooks de ce dossier journalisent avec `logger`, jamais `print`.
- Les valeurs par défaut (widgets, constantes) ne sont relues par aucun job : les vérifier avant de relancer un script.
