# Audit des conventions — `utils/parsing_pipeline`, `resources/` et jobs Python

Date : 2026-10-21. Branche auditée : `claude/sharp-mayer-uqwx8x` (HEAD `6ca3df7`).

## 0. Périmètre, sources et méthode

**Sources de règles**

| Source | Version | Rôle |
|---|---|---|
| `CLAUDE.md` de ce dépôt (sections *Notebooks*, *Jobs*, *Conventions de code*, *Conventions opérationnelles*) | branche courante | règle de référence pour qualibot |
| `datab` (dépôt personnel LEAP), branche la plus récente = `main` | `816e584` (2026-10-15) | guidelines LEAP : `CLAUDE.md`, `templates/`, `README.md` |
| `archives/qualibot_archi.zip` de `datab` (dépôt de référence) | extrait dans un dossier de travail | notebooks `proj/` et `data_asset/`, `leap_utils/`, `platform/ci/check_notebook.py` (le Convention Checker) |

Les règles de qualibot priment quand elles diffèrent de LEAP. Chaque écart est signalé dans le rapport, avec la règle qui le fixe.

**Méthode**

1. Lecture intégrale de `datab/CLAUDE.md`, de `templates/notebook_proj_template.py`, des `templates/bundle_data_asset/` et de `platform/ci/check_notebook.py` (970 lignes, règles `S*`, `B*`, `N*`).
2. Convention Checker LEAP exécuté tel quel sur les 8 notebooks (`create_proj_*.py` et `update_proj_vector_indexes.py`), puis tri des résultats : vrais écarts, faux positifs, règles qui ne s'appliquent pas.
3. `runbooks/scripts/code_quality/check_docstrings.py` sur `utils/parsing_pipeline` : **0 problème**. Sur `server utils` : **0 problème**.
4. `ruff` (version 0.17.1, dans un venv de travail, `--no-fix`, `--isolated`) sur `utils/parsing_pipeline`, `utils/lakebase_sync`, `utils/app_mgmt`, `utils/evaluation`, pour les règles `F` et `E9`.
5. Scans ciblés (`grep`) : idiomes interdits, environnements en dur, widgets, commentaires d'historique, sections, cellules de code sans explication Markdown, écritures de tables.
6. Lecture des fichiers `resources/*.yml` et de `databricks.yml` (cibles, variables, `run_as`, clusters, tags, notifications).
7. Comparaison de `leap_utils/` avec le `leap_utils` de l'archive.

**Limites**

- Rien n'a été exécuté sur Databricks ; l'audit est statique.
- Les templates Gold et Silver de `datab` ne sont lus que par leurs titres (le pipeline parsing n'est pas un Silver ou un Gold).
- Les tests unitaires de `tests/` n'ont pas été lancés.

## 1. Synthèse

**Verdict.** Le pipeline respecte les règles dures : pas d'environnement en dur dans le code, aucun `.cast()`, `.to_date()`, `.union()`, `.saveAsTable()` ni `print()`, pas de marqueur `DBTITLE`, GX regroupé dans `# Quality Checks`, une explication Markdown avant chaque bloc de code, docstrings complètes, commentaires « pourquoi » sans historique ni référence à `runbooks/`, et `leap_utils/` identique à la référence au flag près (voir §4).

**Aucun bloquant au sens du Convention Checker.** Les écarts se concentrent sur quatre points :

1. **Uniformité des notebooks** : noms de sous-sections et sections vides incohérents, un logger mal rangé, un README faux.
2. **Actions Spark hors garde `DEBUG`** : un `.show()` et huit `.count()` s'exécutent à chaque run en production.
3. **Configuration répartie** : widgets, variables d'environnement du cluster et valeurs écrites en dur dans le YAML, sans règle claire.
4. **Dépendances et ressources** : versions Python épinglées à deux endroits (dont une dérive OpenCV), UUID de service principal écrits en dur (28 occurrences), cluster GPU sans policy (dérogation déjà documentée).

Il n'existe **aucune porte de qualité sur le push** : `bitbucket-pipelines.yml` ne lance ni `pytest`, ni `check_docstrings.py`, ni lint.

### Tableau des écarts (priorité décroissante)

| # | Gravité | Écart | Où | Solution proposée |
|---|---|---|---|---|
| E1 | Important | Actions Spark hors `DEBUG` : `.show()` non gardé, huit `.count()` pour des logs | §3.2 | Garde `if DEBUG:` (widget `debug` ajouté, voir E4), ou log sans action |
| E2 | Important | Sept `.collect()` dans quatre notebooks, dont des listes d'identifiants | §2.3 | `quality_checks.collect_column_in_list()` (présent dans `leap_utils`) |
| E3 | Important | Dépendances épinglées à deux endroits, dérive d'OpenCV (4.11.0.86 en CPU, 4.12.0.88 en GPU) | §3.3 | Une seule liste de pins + test de cohérence |
| E4 | Important | Widgets standard absents (`pipeline_read_env`, `reference_read_env`, `debug`) ; environnement lu à deux endroits (variable du cluster dans 6 notebooks, widget dans 2) | §3.1 | Choisir un mécanisme et le documenter (§3.1) |
| E5 | Important | UUID de service principal écrits en dur dans les YAML (28 occurrences, DEV par défaut dans `run_as` racine) | §3.4 | Variable `dbx_service_principal` par cible dans `databricks.yml` |
| E6 | Important | Valeurs de pipeline écrites en dur dans le YAML (endpoint et modèle Vector Search, projet et branche Lakebase, endpoint LLM) | §3.4 | Variables dans `databricks.yml` |
| E7 | Important | Cluster GPU `parse_gpu_cluster` sans policy ; la règle LEAP impose une policy | §3.4 | Dérogation documentée ; demande d'une policy GPU-ML à l'administrateur |
| E8 | Important | Aucune porte de test ou de lint sur le push | §5 | Étape `pytest tests` et `check_docstrings.py` dans `bitbucket-pipelines.yml` |
| E9 | Mineur | Cinq notebooks : `## Config Project Imports` au lieu de `## Config Function Imports` | §2.1 | Renommer |
| E10 | Mineur | Trois notebooks sans sous-section `## Config logger` (logger créé dans `Config Widgets`) | §2.1 | Ajouter la sous-section |
| E11 | Mineur | Deux sections vides sans `#N/A` (`Data Preparation` de `doc_catalog`, `Data Transformations` de `update_proj_vector_indexes`) | §2.1 | Ajouter `#N/A` (règle LEAP §3.1) |
| E12 | Mineur | README : tableau « Files » attribue `create_proj_chunks.py` à `3_parse` (c'est `3c_chunks`) et omet les notebooks `parsing_checkpoint` et `_gpu` | §5 | Corriger le tableau (le tableau « Tasks » est juste) |
| E13 | Mineur | CLAUDE.md dit « chaque notebook appelle `quality.require_gx()` » ; le code appelle `tables.require_gx()`, et `doc_catalog` n'appelle rien (checks Python, serverless, voulu) | §5 | Corriger la règle et documenter l'exception |
| E14 | Mineur | `create_proj_chunks.py` et `create_proj_parsing_checkpoint.py` : `%pip` avec `--force-reinstall` et `restartPython()` dans le notebook, dépendances du GPU dans le YAML | §3.3 | Voir E3 |
| E15 | Mineur | `# /// script` (métadonnées d'environnement serverless) en tête de `create_proj_parse_manifest.py`, alors que la tâche tourne sur cluster classique | §2.2 | Supprimer, ou confirmer l'usage |
| E16 | Mineur | Pré-contrôle `raise RuntimeError` en `# Inputs` de `doc_catalog` (hors `# Quality Checks`) | §2.2 | Déplacer en `# Quality Checks` ou documenter comme pré-condition |
| E17 | Mineur | `.collect()` puis `row_number()` manuel dans `manifest_steps.py:151`, alors que `drop_duplicates_by_order()` existe | §2.3 | Remplacer si le sens « garder le plus récent » est identique |
| E18 | Mineur | `evaluation/pairwise_answers.py` importe des fonctions privées de `server` (`_strip_division`, `_trim_history`, `_with_today_date`, `_cost_eur`) | §6 | Exposer des noms publics dans `server.services.chat` |
| E19 | Mineur | Alertes en `Europe/Paris` ; la règle est `Europe/Brussels` (même décalage horaire, mais la règle est littérale) | §6 | Aligner |
| E20 | Mineur | Tags `project: Qualibot` et `project-name: qualibot` (doublon, casse différente) | §3.4 | Garder `project-name` |
| E21 | Mineur | `E501` : 839 lignes de plus de 100 caractères dans `utils/parsing_pipeline` (la config `ruff` du projet fixe 100, mais rien ne l'applique ; `server` en a 999) | §6 | Trancher : appliquer `ruff format` en CI, ou retirer le réglage |
| E22 | Mineur | Alias `from pyspark.sql import functions as F` (15 imports des fichiers Python du pipeline ; LEAP impose `f`) | §3.1 | Décision : garder `F` et l'écrire dans CLAUDE.md (le plus simple) |
| E23 | Mineur | Bloc `sys.path` copié-collé (8 lignes) dans 7 notebooks | §2.2 | Factoriser si le coût en vaut la peine ; sinon accepter |

## 2. Notebooks (`utils/parsing_pipeline/create_proj_*.py`, `update_proj_vector_indexes.py`)

### 2.1 Structure et titres

**Conforme**

- Les 8 notebooks commencent par `# Databricks notebook source`, puis un en-tête `# PROJ <ASSET>` avec les rubriques `Description`, `Highlighted complexities`, `Intended Pipeline`, `Inputs Data`, `Output Tables` (LEAP §3.2).
- L'ordre des sections est correct dans les 8 notebooks : Technical debt, Configuration, Inputs, Data Preparation, Data Transformations, Quality Checks, Outputs.
- Les blocs de préparation sont nommés `## Prep<N> - …`, ceux de transformation `## Tr. <N> - …` (LEAP §3.1 ; `Prep1` et `Tr. 1` existent dans les notebooks qui en ont).
- Pas de `# DBTITLE`.
- Le nom `update_proj_vector_indexes.py` déclenche `N01` du Convention Checker ; c'est une exception écrite dans CLAUDE.md. Pas d'écart.

**Écarts**

- **E9 — noms de sous-sections d'imports** (règle qualibot : `Config Function Imports`)
  - `Config Project Imports` : `create_proj_category_reference.py:55`, `create_proj_doc_catalog.py:56`, `create_proj_image_metadata.py:61`, `create_proj_parse_manifest.py:67`, `update_proj_vector_indexes.py:56`.
  - `Config Function Imports` : `create_proj_chunks.py:94`, `create_proj_parsing_checkpoint.py:97`, `create_proj_parsing_checkpoint_gpu.py:67`.
  - Les notebooks de référence utilisent surtout `Config LEAP Function Imports` (55 occurrences) ; `datab` impose cette forme. Choisir une seule forme et la renommer partout.
- **E10 — `## Config logger` absent** dans `create_proj_category_reference.py` (logger créé en ligne 84, dans `Config Widgets`), `create_proj_parse_manifest.py` (ligne 107) et `create_proj_image_metadata.py` (ligne 92). Le logger a sa propre sous-section dans les cinq autres.
- **E11 — sections vides sans `#N/A`** : `create_proj_doc_catalog.py:126-130` (`# Data Preparation`), `update_proj_vector_indexes.py:150-154` (`# Data Transformations`).
- **Technical debt** : rempli dans les 8 notebooks, avec des lignes concrètes (règle LEAP « une ligne par dette »).

### 2.2 Code et organisation

**Conforme**

- Toutes les cellules de code sont précédées d'une cellule Markdown qui explique le *pourquoi* (vérification par script : zéro cellule de code sans explication, sauf E15).
- Le contrôle `test_every_pipeline_notebook_explains_each_code_block` de `tests/test_code_conventions.py` couvre cette règle.
- `dbutils.widgets.removeAll()` est appelé avant la déclaration des widgets dans les 8 notebooks.
- Les écritures passent par `tables.py` (`write_table`, `merge_into`, `overwrite_rows`), qui délègue à `leap_utils` (`save_table`, `merge_into`). `overwrite_rows` utilise `INSERT OVERWRITE` pour conserver la clé primaire ; le commentaire le justifie (`tables.py:109`). Le Convention Checker (`B10`, `B11`, `S21`) ne voit pas ces wrappers : faux positifs (§4).
- Les contrôles GX passent par `tables.check_dataframe` / `check_rows` (`leap_utils` `gx_validation` en dessous), dans `# Quality Checks`. Chaque contrôle porte une sévérité `RED` ou `AMBER`.
- Aucun `try_cast` manquant : aucun `.cast()`, `.to_date()`, `.union()`, `.saveAsTable()`, `.toDF(*[…])` ni `.lstrip('0')` dans le notebook.
- Les commentaires des notebooks et des modules sont des rappels de comportement ; aucun n'en raconte l'historique ni ne renvoie à `runbooks/`, à une date ou à un incident (grep sur `2025|2026|incident|fixed|regression|runbook|no longer|used to|PR`).

**Écarts**

- **E15** : `create_proj_parse_manifest.py:2-5` contient un bloc `# /// script` avec `environment_version = "5"`. C'est la forme serverless. Le notebook tourne sur `meta_cpu_cluster` (classique) ; le bloc n'a donc pas d'effet, ou il est résiduel.
- **E16** : `create_proj_doc_catalog.py:118-122` lève `RuntimeError` si une table manque, en `# Inputs`. Le contrôle est correct mais hors de la section groupée. Solution : le déplacer en `# Quality Checks` ou le nommer pré-condition dans l'en-tête.
- **E17** : `manifest_steps.py:151` utilise `row_number().over(newest_first)` puis `filter(_rn > 1)` ; c'est une déduplication « garder le plus récent » que `table_utils.drop_duplicates_by_order()` (`leap_utils/data_asset/table_utils.py:818`) couvre.
- **E23** : le même bloc de 8 lignes (`_ctx = dbutils.notebook.entry_point…` puis deux `sys.path.insert`) se trouve dans 7 notebooks. Factorisation possible dans un petit module chargé par `%run`, ou acceptation documentée.

### 2.3 Actions Spark et collecte

- **E1, `.show()` non gardé** : `create_proj_category_reference.py:151`, `.show(truncate=False)` s'exécute dans la tâche `1_categories` à chaque run. Solution : supprimer, ou le remplacer par un log et le garder hors de la production.
- **E1, `.count()` pour des logs** (hors GX et hors `DEBUG`) : `create_proj_category_reference.py:148`, `create_proj_image_metadata.py:138,184`, `create_proj_parsing_checkpoint.py:194,222,236`, `create_proj_parsing_checkpoint_gpu.py:173`, `update_proj_vector_indexes.py:201`. Chaque `.count()` lance un job Spark. Solution : un widget `debug` (absent aujourd'hui), puis `if DEBUG:` ; ou compter une seule fois ce qui est déjà matérialisé.
- **E2, `.collect()`** : `create_proj_category_reference.py:166`, `create_proj_doc_catalog.py:149`, `create_proj_image_metadata.py:140,158`, `create_proj_parse_manifest.py:152,228,229`. Pour une colonne, `quality_checks.collect_column_in_list(df, col)` (`leap_utils/common/validation/quality_checks.py:42`) remplace ces listes. Pour les `dict` (lignes 228-229), garder le `.collect()`.

### 2.4 Widgets et environnement (E4)

- Aucun notebook ne déclare `pipeline_read_env`, `reference_read_env` ni `debug` (les trois sont obligatoires chez LEAP, `S07`).
- L'environnement vient de deux sources :
  - la variable de cluster `PIPELINE_WRITE_ENV` lue dans `config.py:36` (`_require_env`), pour les six notebooks du pipeline ;
  - le widget `pipeline_write_env` dans `create_proj_doc_catalog.py:88` (défaut `dev`) et `update_proj_vector_indexes.py:91` (défaut `dev`).
- Le défaut `dev` est conforme à la règle LEAP (`S08`) ; il est dangereux pour un job, car un paramètre manquant écrit en DEV sans erreur.
- **Solution** : choisir la variable de cluster pour tout le pipeline (elle est déjà la source de `config.py`), supprimer les deux widgets d'environnement ou les faire lire la variable, et écrire la règle dans CLAUDE.md (« le pipeline reçoit son environnement par `PIPELINE_WRITE_ENV` ; `pipeline_write_env` n'existe que pour les lancements à la main »).

### 2.5 Dépendances (E3, E14)

- `create_proj_chunks.py:55` et `create_proj_parsing_checkpoint.py:58` installent `numpy<2`, `pydantic>=2.10.3,<3`, `docling==2.140.0`, `docling-core==2.102.0` et d'autres en `%pip`.
- La tâche `3b_parse_pdf` (`resources/parsing_pipeline.job.yml`) déclare les mêmes paquets dans `libraries`, avec les mêmes versions ; un changement à un seul endroit crée une dérive.
- OpenCV : `opencv-python-headless==4.11.0.86` dans les deux notebooks CPU (`create_proj_chunks.py:65`, `create_proj_parsing_checkpoint.py:68`), `==4.12.0.88` dans le notebook GPU (`create_proj_parsing_checkpoint_gpu.py:45`). Le README explique que la version 4.13 échoue sur le runtime ; le choix 4.11 contre 4.12 entre CPU et GPU n'est pas expliqué dans le code.
- **Solution** : une liste de pins unique (par exemple `utils/parsing_pipeline/requirements.txt`), lue par les `%pip -r` des notebooks, et un test qui compare cette liste aux `libraries` du YAML. Décider si OpenCV doit être identique partout ; si non, l'écrire dans le README.
- Un test de cohérence entre pins et YAML est à ajouter dans `tests/test_code_conventions.py`, sur le modèle des tests existants (il n'en existe pas aujourd'hui).
- LEAP impose `libraries` sur les tâches, jamais sur le cluster : c'est respecté. Le pipeline utilise en plus des `%pip` dans le notebook (3_parse et 3c_chunks), ce qui est une variante acceptée par qualibot ; documenter la variante dans CLAUDE.md.

## 3. Jobs (`resources/*.yml`, `databricks.yml`)

### 3.1 Règles de job (LEAP §11 et `CLAUDE.md` qualibot)

| Règle | `parsing_pipeline.job.yml` | `alerts.yml` | `app_schedules.yml` | `chat_mlflow.yml` | `lakebase_sync.yml` |
|---|---|---|---|---|---|
| `queue.enabled: true` | oui | oui | oui | oui | oui |
| `email_notifications.on_failure` = `${var.notification_email}` | oui | — (alertes SQL) | oui | oui | oui |
| `no_alert_for_canceled_runs: true` | oui | — | oui | oui | oui |
| tags `activity-type`, `job-purpose`, `project-name` | oui | — | oui | oui | oui |
| `run_as` | UUID DEV en dur, puis cibles UAT/PROD | — | UUID DEV en dur | UUID DEV en dur | UUID DEV en dur |
| cluster avec `policy_id` | CPU oui ; **GPU non** (E7) | — | — | — | — |
| `data_security_mode: SINGLE_USER` | oui | — | — | — | — |
| fuseau `Europe/Brussels` | oui | **non** (`Europe/Paris`, E19) | oui | oui | oui |
| nom `<D\|W\|M\|F\|Z>_<niveau>_<domaine>_<objet>` | `D_1_Qualibot_Parsing_Pipeline` | — | oui | oui | oui |

**Conforme** : `max_concurrent_runs: 1` là où le passage de relais le demande ; `timezone_id` ; `pause_status` lu d'une variable ; la tâche `3c_chunks` avec `run_if: AT_LEAST_ONE_SUCCESS` ; `condition_task` sur `pdfs_for_gpu`.

**Écarts**

- **E5 — UUID en dur** : 28 occurrences de UUID dans `resources/*.yml`. `parsing_pipeline.job.yml` met le DEV dans `run_as` racine (`fde6ff28-…`), puis UAT et PROD dans leurs cibles ; `app_schedules.yml`, `chat_mlflow.yml` et `lakebase_sync.yml` répètent le DEV dans `run_as` et `permissions`. `databricks.yml` ne déclare pas de variable `dbx_service_principal` (contrairement au template LEAP, `templates/bundle_data_asset/databricks.yml`).
  - **Solution** : une variable `dbx_service_principal` par cible dans `databricks.yml` (DEV, UAT, PROD), et `run_as: service_principal_name: ${var.dbx_service_principal}` partout.
- **E6 — valeurs en dur dans les tâches** : `5_sync_index` (`vector_search_endpoint: "qualibot"`, `embedding_model: "databricks-qwen3-embedding-0-6b"`), `6_update_kb_metadata` (`lakebase_project_id: "qualibot"`, `lakebase_branch: "production"`, `lakebase_endpoint: "primary"`), `4_describe_images` (`llm_fallback_endpoints: "databricks-gpt-5-6-luna"`, qui duplique le modèle de `config.py`).
  - **Solution** : variables dans `databricks.yml` par cible, et une seule source pour le modèle LLM.
- **E7 — cluster GPU sans policy** : `parse_gpu_cluster` n'a pas de `policy_id`. LEAP §11 impose une policy pour tout cluster. La raison est écrite dans le YAML (pas de version GPU-ML dans la liste autorisée de la policy S). Solution : demander une policy GPU-ML à l'administrateur ; en attendant, garder la dérogation et la noter dans le README du pipeline.
- **E20** : `project: Qualibot` et `project-name: qualibot` coexistent dans les tags ; garder `project-name` (règle LEAP).
- **E19** : `alerts.yml` utilise `Europe/Paris` (lignes 42, 79, 127, 166, 208, 252) ; `Europe/Brussels` est la règle.
- **Variables prod** : la notification de la cible PROD n'est pas surchargée et hérite de `LEAP_Databricks_Alert@latecoere.aero` (défaut racine, `databricks.yml:132-135`), ce qui respecte LEAP §11 (prod).

### 3.2 Points de vigilance (non écarts)

- `intraqual_bronze_catalog_schema: "prod_bronze.intraqual"` est identique dans les trois cibles : c'est la source voulue (le flux DEV ne se rafraîchit plus). Le nom porte `prod_` ; il est documenté dans CLAUDE.md. Pas d'action.
- `D_1` pour un pipeline quotidien : cohérent avec le préfixe `D_1` des jobs partagés (commentaire en tête du YAML).
- Les chemins `notebook_path: ../utils/parsing_pipeline/…` pointent vers le dépôt déployé (`source: WORKSPACE`), et non vers Git (`git_source`) comme dans le template LEAP. Le modèle de déploiement de qualibot est différent ; la règle LEAP « code depuis `main` en prod » ne s'applique pas telle quelle. À documenter.

## 4. Convention Checker LEAP : tri des résultats

Le checker LEAP a été lancé sur les 8 notebooks. Il remonte des écarts réels et des faux positifs, car il vise les notebooks Silver, Gold et Proj de LEAP.

| Règle | Notebooks | Verdict |
|---|---|---|
| `B07` (pas de check RED) | 8 | **Faux positif** : `tables.check_dataframe` passe `validation_level=level` (`tables.py:344`) ; la regex attend le littéral `"RED"`. |
| `S21` (pas de `save_table`/`merge_into`) | 7 | **Faux positif** : les écritures passent par les wrappers `tables.write_table` / `merge_into` (§2.2). |
| `S22` (logger) | 8 | **Faux positif** : `log_utils.setup_applevel_logger` (alias). Écart de nom seulement. |
| `S23` (pas de `concat_ws`) | 8 | **Non applicable** : les clés sont construites en amont (chunks, `parsed_documents`). |
| `N01` (nom de fichier) | 1 | **Exception documentée** (`update_proj_vector_indexes.py`). |
| `S07` / `S09` (widgets standard, widgets non standard) | 8 / 7 | **À trancher** : qualibot n'impose pas les cinq widgets ; voir E4. |
| `S10` (alias `f`) | 5 | **Écart de nom** : qualibot utilise `F` partout (15 imports) ; voir E22. |
| `S12` (`.show`) | 1 | **Écart réel** (E1). |
| `S14` (`.count` hors GX / `DEBUG`) | 5 | **Écart réel** (E1). |
| `S17` / `S31` (`.collect`) | 4 / 2 | **Écart réel** (E2). |

**Ruff (règles `F` et `E9`)** : aucune importation inutilisée (`F401`) ni variable inutilisée (`F841`) dans `utils/parsing_pipeline`. Restent les `F821` sur `spark`, `dbutils`, `display` (globales de notebook, attendues) et les erreurs de syntaxe sur les lignes `%pip` (magic Databricks, attendues).

**Docstrings** : `check_docstrings.py utils/parsing_pipeline` et `server utils` : 0 problème.

## 5. Documentation et tests

- **E12 — README du pipeline** (`utils/parsing_pipeline/README.md`) : le tableau « Files » associe `create_proj_chunks.py` à `3_parse` ; c'est `3c_chunks`. Les notebooks `create_proj_parsing_checkpoint.py` (`3_parse`) et `create_proj_parsing_checkpoint_gpu.py` (`3b_parse_pdf`) n'y figurent pas. Le tableau « Tasks » du même README est juste.
- **E13 — CLAUDE.md** (section *Notebooks*) : « chaque notebook appelle `quality.require_gx()` dans sa cellule d'import ». Le code appelle `tables.require_gx()` (7 notebooks) ; `create_proj_doc_catalog.py` n'appelle rien (serverless, checks en Python par `tables.check_rows`, voulu). Corriger la règle et documenter l'exception.
- **E8 — CI** : `bitbucket-pipelines.yml` ne contient que des étapes de déploiement et de synchronisation (`Deploy…`, `Plan…`, `Sync prod data…`). Les tests (`tests/test_code_conventions.py`, `tests/test_bundle_paths.py`, `tests/test_parsing_*.py`…) et les vérifications (`check_docstrings.py`) ne tournent que par lancement manuel. **Solution** : ajouter une étape `pytest tests` et une étape `python3 runbooks/scripts/code_quality/check_docstrings.py server utils` avant les déploiements.

## 6. Autres jobs Python

| Fichier | Constat | Gravité |
|---|---|---|
| `utils/lakebase_sync/*_job.py`, `utils/app_mgmt/stop_start_app_job.py`, `utils/evaluation/*.py` (notebooks) | en-tête Databricks, `logger` présent, `print()` absent, `removeAll()` avant les widgets, `DBTITLE` absent, pas de commentaire d'historique | conforme |
| `utils/evaluation/pairwise_answers.py:156-160` | importe `_strip_division`, `_trim_history`, `_with_today_date`, `_cost_eur` (fonctions privées de `server`) : un changement dans l'app casse le job sans erreur de compilation | Mineur (E18) |
| `utils/evaluation/*.py` (notebooks, `load_test_*`) | les `dbutils`, `spark` et `display` ne sont pas importés, normal pour des notebooks | — |
| `resources/alerts.yml` | fuseau `Europe/Paris` | Mineur (E19) |
| `resources/*.yml` | UUID en dur | Important (E5) |

Les jobs Python ne déclarent pas de `Technical debt` : hors périmètre LEAP (qui ne concerne que les notebooks de pipeline), donc pas un écart.

## 7. `leap_utils/` : dérive par rapport à l'archive

- Les deux arborescences ont les mêmes fichiers.
- Une seule différence de contenu : `leap_utils/data_asset/table_utils.py`. Le flag `FRESHNESS_PILOT_ENABLED = False` (commentaire : le pilote écrit dans `{env}_bronze`, catalogue non géré par qualibot) désactive l'écriture du pilote de fraîcheur. C'est voulu et documenté dans CLAUDE.md.
- Les autres différences sont des fins de ligne : l'archive est en CRLF, la copie en LF.
- **Action** : aucune. Garder le flag à jour si la référence change.

## 8. Lacunes des guidelines LEAP (`datab`) — à compléter, sans modifier le dépôt

Ces points ne sont pas couverts par `datab/CLAUDE.md` ; ils relèvent d'un pipeline de parsing avec GPU et de configuration par variables de cluster. Propositions à valider avant d'écrire dans `datab` :

1. **Environnement d'un pipeline** : pour un pipeline multi-tâches, une variable de cluster (`PIPELINE_WRITE_ENV`) est-elle acceptable à la place des widgets ?
2. **Cluster GPU** : une dérogation à « policy obligatoire » quand l'organisation n'a pas de policy GPU, avec une limite de durée.
3. **Dépendances** : une seule liste de pins, lue par les notebooks et le YAML.
4. **Alias pyspark** : `F` ou `f` (LEAP impose `f` ; qualibot utilise `F`).
5. **Écritures** : un wrapper local de `leap_utils` (`tables.py`) est accepté si chaque écriture délègue à `save_table` ou `merge_into` ; l'exception `INSERT OVERWRITE` (clé primaire) est à inscrire.
6. **Faux positifs du Convention Checker** : `B07`, `S21`, `S22`, `S23` quand les checks passent par un wrapper.
7. **Pré-conditions** : un contrôle qui vérifie l'existence d'une table en entrée est-il un check (groupé) ou une pré-condition ?

## 9. Actions proposées (ordre de travail)

**Lot A — mécanique, sans risque (une seule PR)**
1. Renommer les 5 sous-sections `Config Project Imports` (E9).
2. Ajouter `## Config logger` dans 3 notebooks (E10).
3. Ajouter `#N/A` dans 2 sections vides (E11).
4. Corriger le tableau « Files » du README (E12) et la règle `quality.require_gx()` de CLAUDE.md (E13).
5. Supprimer le bloc `# /// script` s'il est inutile (E15).

**Lot B — exécution Spark (validation sur DEV requise)**
6. Ajouter le widget `debug` ; garder le `.show()` et les `.count()` derrière `if DEBUG:` (E1).
7. Remplacer les listes de `.collect()` par `collect_column_in_list` (E2).
8. Déplacer le `RuntimeError` de `doc_catalog` en `# Quality Checks` (E16).

**Lot C — configuration et ressources (validation sur DEV requise)**
9. Une seule source d'environnement (E4) : décision, puis documentation.
10. Variables `dbx_service_principal` et valeurs de pipeline dans `databricks.yml` (E5, E6).
11. Une seule liste de pins et un test de cohérence (E3, E14).

**Lot D — gouvernance**
12. Étape de tests et de vérification de docstrings dans `bitbucket-pipelines.yml` (E8).
13. Demande de policy GPU-ML (E7).
14. Décisions sur E21 (longueur de ligne) et E22 (alias `F`), à écrire dans CLAUDE.md.

**Lot E — à la prochaine modification du fichier concerné**
15. E17, E18, E19, E20, E23.

## 10. Conforme (points vérifiés)

- En-tête `# PROJ <ASSET>` et rubriques Description, Highlighted complexities, Intended Pipeline, Inputs Data, Output Tables.
- Ordre des sections des 8 notebooks.
- Une explication Markdown avant chaque cellule de code.
- Pas d'environnement en dur (sauf la valeur par défaut `dev` des deux widgets, voulue).
- Pas de `print`, `DBTITLE`, `.cast`, `.to_date`, `.union`, `.saveAsTable`, `.toDF(*…)`, ni `.lstrip('0')`.
- Déduplication et jointures : pas de `.union()`, pas de jointure non filtrée identifiée.
- GX : tous les contrôles sont dans `# Quality Checks` (sauf E16), sévérité `RED` ou `AMBER` explicite.
- Commentaires : rappels de comportement ; aucun historique, aucune date, aucune référence à `runbooks/`.
- Docstrings : 0 problème (`check_docstrings.py`).
- Imports ruff `F401`/`F841` : aucun.
- Job : `queue`, `email_notifications`, `no_alert_for_canceled_runs`, tags obligatoires, `run_as` par cible, `SINGLE_USER`, fuseau `Europe/Brussels` (sauf alertes, E19).
- Notification prod : `LEAP_Databricks_Alert@latecoere.aero`.
- Schedules : `PAUSED` par défaut, lus d'une variable.
- Tests de convention présents (`tests/test_code_conventions.py`).

---

_Rapport produit à partir de `datab` `main` (`816e584`) et de `claude/sharp-mayer-uqwx8x` (`6ca3df7`). Aucune modification de `datab`. Aucune exécution Databricks._
