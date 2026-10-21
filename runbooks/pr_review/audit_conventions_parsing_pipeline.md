# Audit des conventions — `utils/parsing_pipeline`, `resources/` et jobs Python

Date : 2026-10-21. Branche auditée : `claude/sharp-mayer-uqwx8x`, commit `49237be`.

Historique de l'audit : une première version portait sur `6ca3df7`. La branche a avancé de 25 fichiers du périmètre entre les deux commits ; cette version relit tout sur `49237be` et remplace la précédente. Les écarts résolus entre-temps sont signalés comme tels.

## 0. Périmètre, sources et méthode

**Sources de règles**

| Source | Version | Rôle |
|---|---|---|
| `CLAUDE.md` de ce dépôt (sections *Notebooks*, *Jobs*, *Conventions de code*, *Conventions opérationnelles*) | branche courante | règle de référence pour qualibot |
| `datab` (dépôt personnel LEAP), branche la plus récente = `main` | `816e584` (2026-10-15, inchangé depuis le premier audit) | guidelines LEAP : `CLAUDE.md`, `templates/`, `README.md` |
| `archives/qualibot_archi.zip` de `datab` (dépôt de référence) | extrait dans un dossier de travail | notebooks `proj/` et `data_asset/`, `leap_utils/`, `platform/ci/check_notebook.py` (le Convention Checker) |

Les règles de qualibot priment quand elles diffèrent de LEAP. Chaque écart est signalé avec la règle qui le fixe.

**Méthode**

1. Lecture intégrale de `datab/CLAUDE.md`, de `templates/notebook_proj_template.py`, des `templates/bundle_data_asset/` et de `platform/ci/check_notebook.py` (970 lignes, règles `S*`, `B*`, `N*`).
2. Convention Checker LEAP exécuté tel quel sur les 8 notebooks (`create_proj_*.py` et `update_proj_vector_indexes.py`), puis tri : vrais écarts, faux positifs, règles sans objet.
3. `runbooks/scripts/code_quality/check_docstrings.py` sur `utils/parsing_pipeline` : **0 problème** ; sur `server utils` : **0 problème**.
4. `ruff` 0.17.1 (venv de travail, `--no-fix`, `--isolated`) : règles `F401`, `F841`, `F811` et `E501`.
5. Scans `grep` ciblés : idiomes interdits, environnements en dur, widgets, commentaires d'historique, sections, cellules de code sans Markdown, écritures de tables, sections vides.
6. Lecture des `resources/*.yml`, de `databricks.yml`, de `bitbucket-pipelines.yml`.
7. Comparaison de `leap_utils/` avec celui de l'archive.

**Limites**

- Rien n'a été exécuté sur Databricks ; l'audit est statique.
- Les templates Gold et Silver de `datab` ne sont lus que par leurs titres (le pipeline parsing n'est ni un Silver ni un Gold).
- Les tests de `tests/` n'ont pas été lancés.

## 1. Synthèse

**Verdict.** Le pipeline respecte les règles dures : pas d'environnement en dur dans le code, aucun `.cast()`, `.to_date()`, `.union()`, `.saveAsTable()` ni `print()`, pas de `DBTITLE`, GX regroupé dans `# Quality Checks`, une explication Markdown avant chaque bloc de code, docstrings complètes, commentaires « pourquoi » sans historique ni référence à `runbooks/`, aucune importation inutilisée (`F401`), timeout posé sur le job.

**Aucun bloquant au sens du Convention Checker.** Les écarts se concentrent sur quatre points :

1. **Uniformité des notebooks** : noms de sous-sections, sections vides, logger mal rangé, README faux.
2. **Actions Spark hors garde `DEBUG`** : un `.show()` et huit `.count()` s'exécutent à chaque run en production.
3. **Configuration répartie** : widgets, variables de cluster et valeurs écrites en dur dans le YAML, sans règle claire.
4. **Dépendances et ressources** : versions épinglées à deux endroits (dérive d'OpenCV), UUID de service principal écrits en dur dans les cibles, cluster GPU sans policy (dérogation non écrite dans le YAML), et trois tâches lourdes sans `timeout_seconds` (E7 bis).

**Garde-fous en CI** : une étape `pytest` existe désormais (avant chaque déploiement PROD, et seule sur demande). Il manque encore la vérification des docstrings et le lint.

### Tableau des écarts (priorité décroissante)

| # | Gravité | Écart | Où (commit `49237be`) | Solution proposée |
|---|---|---|---|---|
| E1 | Important | Actions Spark hors `DEBUG` : `.show()` non gardé, huit `.count()` pour des logs | §2.3 | Widget `debug` et garde `if DEBUG:`, ou log sans action |
| E2 | Important | Cinq `.collect()` dans trois notebooks, dont des listes d'identifiants | §2.3 | `quality_checks.collect_column_in_list()` (présent dans `leap_utils`) |
| E3 | Important | Dépendances épinglées à deux endroits (notebooks `%pip` et `libraries` du YAML), dérive d'OpenCV 4.11.0.86 (CPU) / 4.12.0.88 (GPU) | §2.5 | Une seule liste de pins et un test de cohérence (modèle : `tests/test_requirements_pinned.py`) |
| E4 | Important | Widgets standard absents (`pipeline_read_env`, `reference_read_env`, `debug`) ; environnement lu à deux endroits (variable de cluster dans 6 notebooks, widget dans 2) | §2.4 | Choisir un mécanisme et le documenter |
| E5 | Important | UUID de service principal écrits en dur dans les cibles : 28 dans `resources/*.yml`, 4 dans `databricks.yml` | §3.1 | Variable `dbx_service_principal` par cible |
| E6 | Important | Valeurs de pipeline en dur dans le YAML (endpoint et modèle Vector Search, projet, branche et endpoint Lakebase, endpoint LLM) | §3.1 | Variables dans `databricks.yml` |
| E7 | Important | Cluster GPU `parse_gpu_cluster` sans policy ; LEAP impose une policy | §3.1 | Dérogation écrite dans le YAML ; demande d'une policy GPU-ML |
| E7 bis | Important | Trois tâches lourdes sans `timeout_seconds` : `3_parse`, `3b_parse_pdf`, `3c_chunks`. Seul le timeout du job (48 h) les borne, et le job n'a qu'un slot (`max_concurrent_runs: 1`) | §3.1 | `timeout_seconds` sur chaque tâche, calé sur la durée observée |
| E8 | Important | Pas de vérification des docstrings ni de lint en CI (les tests sont lancés) | §5 | Étape `check_docstrings.py server utils` dans `bitbucket-pipelines.yml` |
| E9 | Mineur | Cinq notebooks : `## Config Project Imports` au lieu de `## Config Function Imports` | §2.1 | Renommer |
| E10 | Mineur | Trois notebooks sans `## Config logger` (logger créé dans une autre sous-section) | §2.1 | Ajouter la sous-section |
| E11 | Mineur | Trois sections vides sans `#N/A` : `Data Preparation` (`category_reference`, `doc_catalog`), `Data Transformations` (`update_proj_vector_indexes`) | §2.1 | Ajouter `#N/A` (règle LEAP §3.1) |
| E12 | Mineur | README du pipeline : tableau « Files » attribue `create_proj_chunks.py` à `3_parse` (c'est `3c_chunks`) et n'a pas de ligne pour les notebooks `parsing_checkpoint` et `_gpu` | §5 | Corriger le tableau (le tableau « Tasks » est juste) |
| E13 | Mineur | CLAUDE.md (ligne 25) : « every notebook calls `quality.require_gx()` » ; le code appelle `tables.require_gx()` (7 notebooks) et `doc_catalog` n'appelle rien (serverless, checks en Python, voulu) | §5 | Corriger la règle et documenter l'exception |
| E14 | Mineur | `# /// script` (métadonnées d'environnement serverless) en tête de `create_proj_parse_manifest.py`, alors que la tâche tourne sur cluster classique | §2.2 | Supprimer, ou confirmer l'usage |
| E15 | Mineur | Pré-contrôle `raise RuntimeError` en `# Inputs` de `doc_catalog` (hors `# Quality Checks`) | §2.2 | Déplacer en `# Quality Checks` ou documenter comme pré-condition |
| E16 | Mineur | `row_number()` puis `filter(_rn > 1)` dans `manifest_steps.py:184`, alors que `drop_duplicates_by_order()` existe | §2.2 | Remplacer si le sens « garder le plus récent » est identique |
| E17 | Mineur | `evaluation/pairwise_answers.py` importe des fonctions privées de `server` (`_strip_division`, `_trim_history`, `_with_today_date`, `_cost_eur`) | §6 | Exposer des noms publics dans `server.services.chat` |
| E18 | Mineur | Alertes en `Europe/Paris` ; la règle est `Europe/Brussels` (même décalage, mais la règle est littérale) | §3.2 | Aligner |
| E19 | Mineur | Tags `project: Qualibot` et `project-name: qualibot` (doublon, casse différente) | §3.2 | Garder `project-name` |
| E20 | Mineur | `E501` : 875 lignes de plus de 100 caractères dans `utils/parsing_pipeline` (la config `ruff` du projet fixe 100, rien ne l'applique ; `server` en a plus de 900) | §4 | Trancher : `ruff format` en CI, ou retirer le réglage |
| E21 | Mineur | Alias `from pyspark.sql import functions as F` (15 imports ; LEAP impose `f`) | §4 | Décision : garder `F` et l'écrire dans CLAUDE.md |
| E22 | Mineur | Bloc `sys.path` copié-collé (8 lignes) dans 7 notebooks | §2.2 | Factoriser, ou accepter |

**Résolus depuis le premier audit** (`6ca3df7`) : le `run_as` racine en dur a été retiré (les cibles le posent toutes) ; `timeout_seconds` est posé sur le job et sur cinq tâches (E7 bis reste ouvert pour les trois tâches lourdes) ; l'étape `pytest` est ajoutée au CI ; `.collect()` est passé de 7 à 5 ; la source Intraqual est passée de Bronze à Gold (voir §3.2).

## 2. Notebooks (`utils/parsing_pipeline/create_proj_*.py`, `update_proj_vector_indexes.py`)

### 2.1 Structure et titres

**Conforme**

- Les 8 notebooks commencent par `# Databricks notebook source`, puis un en-tête `# PROJ <ASSET>` avec les rubriques `Description`, `Highlighted complexities`, `Intended Pipeline`, `Inputs Data`, `Output Tables` (LEAP §3.2).
- L'ordre des sections est correct dans les 8 : Technical debt, Configuration, Inputs, Data Preparation, Data Transformations, Quality Checks, Outputs.
- Les blocs sont nommés `## Prep<N> - …` et `## Tr. <N> - …` là où ils existent.
- Pas de `# DBTITLE`. `dbutils.widgets.removeAll()` est appelé avant les widgets dans les 8 notebooks.
- `Technical debt` est rempli dans les 8, avec des lignes concrètes.

**Écarts**

- **E9 — noms de sous-sections d'imports** (règle qualibot : `Config Function Imports`) :
  - `Config Project Imports` : `create_proj_category_reference.py:54`, `create_proj_doc_catalog.py:56`, `create_proj_image_metadata.py:61`, `create_proj_parse_manifest.py:67`, `update_proj_vector_indexes.py:56`.
  - `Config Function Imports` : `create_proj_chunks.py:94`, `create_proj_parsing_checkpoint.py:97`, `create_proj_parsing_checkpoint_gpu.py:67`.
  - Les notebooks de référence utilisent surtout `Config LEAP Function Imports` (55 occurrences). Une seule forme, appliquée partout.
- **E10 — `## Config logger` absent** dans `create_proj_category_reference.py`, `create_proj_image_metadata.py` et `create_proj_parse_manifest.py` : le logger est créé dans `Config Constants` ou `Config Widgets`. Les cinq autres ont la sous-section.
- **E11 — sections vides sans `#N/A`** :
  - `create_proj_category_reference.py` : `# Data Preparation` (ligne 110) suivi directement de `# Data Transformations` (ligne 115) ;
  - `create_proj_doc_catalog.py` : `# Data Preparation` (ligne 127) suivi de `# Data Transformations` (ligne 132) ;
  - `update_proj_vector_indexes.py` : `# Data Transformations` (ligne 152) suivi de `# Quality Checks` (ligne 157).

### 2.2 Code et organisation

**Conforme**

- Chaque cellule de code est précédée d'une cellule Markdown qui explique le *pourquoi* (vérifié par script). Le test `test_every_pipeline_notebook_explains_each_code_block` de `tests/test_code_conventions.py` couvre la règle ; E14 est le seul écart et il est déjà signalé.
- Les écritures passent par les wrappers `tables.py` (`write_table`, `merge_into`, `overwrite_rows`), qui délèguent à `leap_utils` (`save_table`, `merge_into`). `overwrite_rows` utilise `INSERT OVERWRITE` pour garder la clé primaire ; le commentaire le justifie (`tables.py:109`).
- Les contrôles GX passent par `tables.check_dataframe` et `check_rows` (`leap_utils` `gx_validation` en dessous), dans `# Quality Checks`, avec une sévérité `RED` ou `AMBER` explicite.
- Aucun `.cast()`, `.to_date()`, `.union()`, `.saveAsTable()`, `.toDF(*[…])` ni `.lstrip('0')`.
- `ruff` (`F401`, `F811`) : aucune importation inutilisée ni redéfinie. Les 47 erreurs `invalid-syntax` remontées par `ruff` viennent des lignes `%pip` (magic Databricks) : ce ne sont pas de vraies erreurs.
- Les commentaires des notebooks et des modules sont des rappels de comportement. Aucun ne raconte l'historique, ni ne cite une date, un incident, un fichier de `runbooks/` (grep sur `2025|2026|incident|fixed|regression|runbook|no longer|used to|PR`).

**Écarts**

- **E14** : `create_proj_parse_manifest.py:2-5` contient un bloc `# /// script` avec `environment_version = "5"`. C'est la forme serverless ; le notebook tourne sur `meta_cpu_cluster` (classique), donc le bloc n'a pas d'effet ou il est résiduel.
- **E15** : `create_proj_doc_catalog.py:122` lève `RuntimeError` si une table manque, en `# Inputs`. Le contrôle est correct mais hors de la section groupée.
- **E16** : `manifest_steps.py:184` fait `row_number().over(newest_first)` puis `filter(_rn > 1)` : une déduplication « garder le plus récent » que `table_utils.drop_duplicates_by_order()` (`leap_utils/data_asset/table_utils.py:818`) couvre.
- **E22** : le même bloc de 8 lignes (`_ctx = dbutils.notebook.entry_point…`, deux `sys.path.insert`) se trouve dans 7 notebooks.

### 2.3 Actions Spark et collecte (E1, E2)

- **`.show()` non gardé** : `create_proj_category_reference.py:127` (`.show(truncate=False)`) s'exécute dans la tâche `1_categories` à chaque run. Solution : le supprimer, ou le remplacer par un log.
- **`.count()` pour des logs** (hors GX et hors `DEBUG`) : `create_proj_category_reference.py:124`, `create_proj_image_metadata.py:140,186`, `create_proj_parsing_checkpoint.py:194,222,236`, `create_proj_parsing_checkpoint_gpu.py:173`, `update_proj_vector_indexes.py:202`. Chaque `.count()` lance un job Spark. Solution : un widget `debug` (absent aujourd'hui), puis `if DEBUG:` ; ou compter une seule fois ce qui est déjà matérialisé.
- **`.collect()`** :
  - `create_proj_doc_catalog.py:149` (liste de lignes) ;
  - `create_proj_image_metadata.py:142` (agrégat `groupBy` converti en dictionnaire) et `:160` (lignes à traiter) ;
  - `create_proj_parse_manifest.py:139` (ensemble d'IDDOC) et `:209`.
  Pour une colonne, `quality_checks.collect_column_in_list(df, col)` (`leap_utils/common/validation/quality_checks.py:42`) remplace l'ensemble et la liste. Les lignes à traiter (`:160`) et l'agrégat (`:142`) gardent le `.collect()`.

### 2.4 Widgets et environnement (E4)

- Aucun notebook ne déclare `pipeline_read_env`, `reference_read_env` ni `debug` (LEAP `S07`).
- L'environnement vient de deux sources :
  - la variable de cluster `PIPELINE_WRITE_ENV`, lue dans `config.py:36` (`_require_env`), pour six notebooks ;
  - le widget `pipeline_write_env` dans `create_proj_doc_catalog.py:88` et `update_proj_vector_indexes.py:92`, défaut `dev`.
- Le défaut `dev` est conforme à LEAP (`S08`), mais il est dangereux pour un job : un paramètre manquant écrit en DEV sans erreur.
- **Solution** : garder la variable de cluster pour tout le pipeline (déjà la source de `config.py`), supprimer les deux widgets d'environnement, et écrire la règle dans CLAUDE.md (« le pipeline reçoit son environnement par `PIPELINE_WRITE_ENV` ; le widget n'existe que pour les lancements à la main »).

### 2.5 Dépendances (E3)

- `create_proj_chunks.py:55` et `create_proj_parsing_checkpoint.py:58` installent en `%pip` : `numpy<2`, `pydantic>=2.10.3,<3`, `docling==2.140.0`, `docling-core==2.102.0`, etc.
- La tâche `3b_parse_pdf` (`resources/parsing_pipeline.job.yml:257`) déclare les mêmes paquets en `libraries`, avec les mêmes versions. Un changement à un seul endroit crée une dérive.
- OpenCV : `opencv-python-headless==4.11.0.86` dans les deux notebooks CPU (`create_proj_chunks.py:65`, `create_proj_parsing_checkpoint.py:68`), `==4.12.0.88` dans le notebook GPU (`create_proj_parsing_checkpoint_gpu.py:45`). Le README explique pourquoi on épingle OpenCV (4.13 échoue sur le runtime) ; l'écart 4.11 contre 4.12 entre CPU et GPU n'est expliqué nulle part.
- `requirements.txt` à la racine et `tests/test_requirements_pinned.py` existent déjà pour l'application ; ils ne couvrent pas les paquets du pipeline.
- **Solution** : une liste de pins unique du pipeline, lue par les `%pip -r` des notebooks, et un test sur le modèle de `test_requirements_pinned.py` qui la compare aux `libraries` du YAML. Décider si OpenCV doit être identique partout ; si non, l'écrire dans le README.
- LEAP impose `libraries` sur les tâches, jamais sur le cluster : c'est respecté. Les `%pip` dans le notebook (`3_parse`, `3c_chunks`) sont une variante ; la documenter dans CLAUDE.md.

## 3. Jobs (`resources/*.yml`, `databricks.yml`)

### 3.1 Clusters, `run_as`, valeurs en dur (E5, E6, E7)

- **E5 — UUID en dur dans les cibles** : 28 UUID dans `resources/*.yml` et 4 dans `databricks.yml` (lignes 214, 314, 374, 458). `parsing_pipeline.job.yml` pose désormais `run_as` dans chaque cible (lignes 369, 377, 385) ; `app_schedules.yml`, `chat_mlflow.yml` et `lakebase_sync.yml` répètent le DEV en dur dans `run_as` et `permissions`. `databricks.yml` ne déclare pas de variable `dbx_service_principal` (le template LEAP en a une : `templates/bundle_data_asset/databricks.yml`).
  - **Solution** : une variable `dbx_service_principal` par cible dans `databricks.yml`, puis `run_as: service_principal_name: ${var.dbx_service_principal}` partout.
- **E6 — valeurs en dur dans les tâches** : `resources/parsing_pipeline.job.yml:301` (`llm_fallback_endpoints: "databricks-gpt-5-6-luna"`, qui duplique le modèle de `config.py`), `:322-323` (`vector_search_endpoint: "qualibot"`, `embedding_model: "databricks-qwen3-embedding-0-6b"`), `:339-341` (`lakebase_project_id`, `lakebase_branch`, `lakebase_endpoint`).
  - **Solution** : variables dans `databricks.yml` par cible ; une seule source pour le modèle LLM.
- **E7 — cluster GPU sans policy** : `parse_gpu_cluster` n'a pas de `policy_id`, alors que les trois clusters CPU en ont une (`${var.job_cluster_policy_id_S}`). La raison (pas de version GPU-ML dans la liste autorisée de la policy S) est dans un commentaire de tête de YAML. **Solution** : demander une policy GPU-ML à l'administrateur ; en attendant, garder la dérogation et la noter dans le README du pipeline.
- **Clusters CPU** : `data_security_mode: SINGLE_USER`, `policy_id` S, `num_workers: 0` : conforme.
- **E7 bis — timeouts** : `timeout_seconds` est posé sur le job (`resources/parsing_pipeline.job.yml:34`, 172800 s) et sur `1_categories` (3600), `2_manifest` (7200), `4_describe_images` (43200), `5_sync_index` (10800) et `6_update_kb_metadata` (3600). Il manque sur `3_parse`, `3b_parse_pdf` et `3c_chunks`, les tâches qui parsent et tiennent le slot le plus longtemps. Une tâche bloquée (GPU, spot, boucle) tient le seul slot du job jusqu'à 48 h. **Solution** : un `timeout_seconds` sur chacune des trois, avec une marge au-dessus de la durée observée sur le dernier run complet.

### 3.2 Tags, fuseaux, notifications (E18, E19)

| Règle | `parsing_pipeline.job.yml` | `alerts.yml` | `app_schedules.yml` | `chat_mlflow.yml` | `lakebase_sync.yml` |
|---|---|---|---|---|---|
| `queue.enabled: true` | oui | oui | oui | oui | oui |
| `email_notifications.on_failure` = `${var.notification_email}` | oui | alertes SQL | oui | oui | oui |
| `no_alert_for_canceled_runs: true` | oui | — | oui | oui | oui |
| tags `activity-type`, `job-purpose`, `project-name` | oui (+ `project` en doublon, E19) | — | oui | oui | oui |
| fuseau `Europe/Brussels` | oui | **non** (`Europe/Paris`, E18) | oui | oui | oui |
| timeout du job | oui (48 h) ; tâches : 5 sur 8, E7 bis | — | — | — | — |
| nom `<D\|W\|M\|F\|Z>_<niveau>_<domaine>_<objet>` | `D_1_Qualibot_Parsing_Pipeline` | — | oui | oui | oui |

- **Notification PROD** : la cible PROD n'a pas de surcharge et hérite de `LEAP_Databricks_Alert@latecoere.aero` (défaut racine, `databricks.yml:132-135`) : conforme à LEAP §11.
- **Conforme** : `max_concurrent_runs: 1` là où le passage de relais le demande ; `pause_status` lu d'une variable ; `run_if: AT_LEAST_ONE_SUCCESS` sur `3c_chunks` ; `condition_task` sur `pdfs_for_gpu`.

### 3.3 Source des métadonnées (conforme, à savoir)

- La source n'est plus Bronze : `PARSING_INTRAQUAL_GOLD` = `${var.intraqual_gold_catalog_schema}` = `prod_gold.quality_system`, table `document_revisions_intraqual` (`config.py:43`). Les trois cibles lisent la même source, et la variable est déclarée dans `databricks.yml`. La cible UAT le justifie en commentaire (le metastore partagé).
- Le nom `prod_gold` est volontairement en dur dans les variables de chaque cible, pas dans le code : conforme au principe « config dans les variables ».

### 3.4 Points de vigilance (non écarts)

- `D_1` pour un pipeline quotidien : cohérent avec le préfixe des jobs partagés (commentaire en tête du YAML).
- Les chemins `notebook_path: ../utils/parsing_pipeline/…` pointent vers le dépôt déployé (`source: WORKSPACE`), et non vers Git (`git_source`) comme dans le template LEAP. Le modèle de déploiement de qualibot diffère ; la règle LEAP « code depuis `main` en prod » ne s'applique pas telle quelle. À documenter.

## 4. Convention Checker LEAP : tri des résultats

| Règle | Notebooks | Verdict |
|---|---|---|
| `B07` (pas de check RED) | 8 | **Faux positif** : `tables.check_dataframe` passe `validation_level=level` (`tables.py:344`) ; la regex attend le littéral `"RED"`. |
| `S21` (pas de `save_table`/`merge_into`) | 7 | **Faux positif** : les écritures passent par `tables.write_table` / `merge_into` (§2.2). |
| `S22` (logger) | 8 | **Faux positif** : `log_utils.setup_applevel_logger` (alias). Écart de nom seulement. |
| `S23` (pas de `concat_ws`) | 8 | **Sans objet** : les clés sont construites en amont (chunks, `parsed_documents`). |
| `N01` (nom de fichier) | 1 | **Exception documentée** (`update_proj_vector_indexes.py`). |
| `S07` / `S09` (widgets standard, non standard) | 8 / 7 | **À trancher** : qualibot n'impose pas les cinq widgets ; voir E4. |
| `S10` (alias `f`) | 4 | **Écart de nom** : qualibot utilise `F` (E21). |
| `S12` (`.show`) | 1 | **Écart réel** (E1). |
| `S14` (`.count` hors GX / `DEBUG`) | 5 | **Écart réel** (E1). |
| `S17` / `S31` (`.collect`) | 3 / 2 | **Écart réel** (E2). |

**Ruff** : aucune importation inutilisée (`F401`), aucune variable inutilisée (`F841`), aucune redéfinition (`F811`). Restent les `F821` sur `spark`, `dbutils`, `display` (globales de notebook, attendues).

**Docstrings** : `check_docstrings.py` sur `utils/parsing_pipeline` et sur `server utils` : 0 problème.

## 5. Documentation et CI

- **E12 — README du pipeline** (`utils/parsing_pipeline/README.md:17`) : le tableau « Files » associe `create_proj_chunks.py` à `3_parse`. Le tableau « Tasks » du même README (ligne 53) l'associe bien à `3c_chunks`. Le tableau « Files » n'a pas de ligne pour `create_proj_parsing_checkpoint.py` (`3_parse`) ni pour `create_proj_parsing_checkpoint_gpu.py` (`3b_parse_pdf`).
- **E13 — CLAUDE.md** (ligne 25, section *Notebooks*) : « Every notebook calls `quality.require_gx()` in its import cell ». Le code appelle `tables.require_gx()` dans 7 notebooks ; `create_proj_doc_catalog.py` n'appelle rien (serverless, checks en Python par `tables.check_rows`, voulu). Corriger la règle et documenter l'exception.
- **E8 — CI** : `bitbucket-pipelines.yml` (étape `tests`, lignes 24-31) lance `pytest -q tests` avant chaque déploiement PROD, et peut être lancée seule. Il manque une étape `python3 runbooks/scripts/code_quality/check_docstrings.py server utils` et une étape de lint (`ruff check --no-fix`, la règle `fix = true` de `pyproject.toml` doit être désactivée pour la CI).

## 6. Autres jobs Python

| Fichier | Constat | Gravité |
|---|---|---|
| `utils/lakebase_sync/*_job.py`, `utils/app_mgmt/stop_start_app_job.py`, `utils/evaluation/*.py` (notebooks) | en-tête Databricks, `logger` présent, pas de `print()`, `removeAll()` avant les widgets, pas de `DBTITLE`, pas de commentaire d'historique | conforme |
| `utils/evaluation/pairwise_answers.py:156-160` | importe `_strip_division`, `_trim_history`, `_with_today_date`, `_cost_eur` (fonctions privées de `server`) : un changement dans l'application casse le job sans erreur de compilation | Mineur (E17) |
| `resources/alerts.yml` | fuseau `Europe/Paris` (lignes 42, 79, 128, …) | Mineur (E18) |
| `resources/*.yml` | UUID en dur | Important (E5) |

Les jobs Python ne déclarent pas de `Technical debt` : hors périmètre LEAP (qui ne concerne que les notebooks de pipeline), donc pas un écart.

## 7. `leap_utils/` : dérive par rapport à l'archive

- Mêmes fichiers des deux côtés.
- Une seule différence de contenu : `leap_utils/data_asset/table_utils.py`. Le flag `FRESHNESS_PILOT_ENABLED = False` désactive l'écriture du pilote de fraîcheur, qui écrirait dans `{env}_bronze` (catalogue non géré par qualibot). C'est voulu et documenté.
- Les autres différences sont des fins de ligne (CRLF dans l'archive, LF ici).
- **Action** : aucune. Garder le flag à jour si la référence change.

## 8. Lacunes des guidelines LEAP (`datab`) — à compléter, sans modifier le dépôt

Ces points ne sont pas couverts par `datab/CLAUDE.md`. Propositions à valider avant d'écrire dans `datab` :

1. **Environnement d'un pipeline multi-tâches** : une variable de cluster (`PIPELINE_WRITE_ENV`) est-elle acceptable à la place des widgets ?
2. **Cluster GPU** : une dérogation à « policy obligatoire » quand l'organisation n'a pas de policy GPU, avec un calendrier de levée.
3. **Dépendances** : une seule liste de pins pour les notebooks et le YAML, avec un test de cohérence.
4. **Alias pyspark** : `F` ou `f` (LEAP impose `f` ; qualibot utilise `F`).
5. **Écritures** : un wrapper local de `leap_utils` (`tables.py`) est accepté si chaque écriture délègue à `save_table` ou `merge_into` ; l'exception `INSERT OVERWRITE` (clé primaire) est à inscrire.
6. **Faux positifs du Convention Checker** : `B07`, `S21`, `S22`, `S23` quand les checks passent par un wrapper.
7. **Pré-conditions** : un contrôle qui vérifie l'existence d'une table en entrée est-il un check (groupé) ou une pré-condition ?

## 9. Actions proposées (ordre de travail)

**Lot A — mécanique, sans risque (une seule PR)**
1. Renommer les 5 sous-sections `Config Project Imports` (E9).
2. Ajouter `## Config logger` dans 3 notebooks (E10) et `#N/A` dans 3 sections vides (E11).
3. Corriger le tableau « Files » du README (E12) et la règle `quality.require_gx()` de CLAUDE.md (E13).
4. Supprimer le bloc `# /// script` s'il est inutile (E14).

**Lot B — exécution Spark (validation sur DEV requise)**
5. Ajouter le widget `debug` ; garder le `.show()` et les `.count()` derrière `if DEBUG:` (E1).
6. Remplacer les `.collect()` de listes par `collect_column_in_list` (E2).
7. Déplacer le `RuntimeError` de `doc_catalog` en `# Quality Checks` (E15).

**Lot C — configuration et ressources (validation sur DEV requise)**
8. Une seule source d'environnement (E4) : décision, puis documentation.
9. Variables `dbx_service_principal` et valeurs de pipeline dans `databricks.yml` (E5, E6).
9 bis. `timeout_seconds` sur `3_parse`, `3b_parse_pdf` et `3c_chunks` (E7 bis) : à faire en premier dans ce lot, sans changer le code.
10. Une seule liste de pins et un test de cohérence (E3).

**Lot D — gouvernance**
11. Étape de vérification des docstrings et de lint dans `bitbucket-pipelines.yml` (E8).
12. Demande de policy GPU-ML (E7).
13. Décisions sur E20 (longueur de ligne) et E21 (alias `F`), à écrire dans CLAUDE.md.

**Lot E — à la prochaine modification du fichier concerné**
14. E16, E17, E18, E19, E22.

## 10. Conforme (points vérifiés)

- En-tête `# PROJ <ASSET>` et rubriques de description.
- Ordre des sections des 8 notebooks ; `Technical debt` rempli.
- Une explication Markdown avant chaque cellule de code.
- Pas d'environnement en dur dans le code (sauf la valeur par défaut `dev` des deux widgets, voulue).
- Pas de `print`, `DBTITLE`, `.cast`, `.to_date`, `.union`, `.saveAsTable`, `.toDF(*…)`, `.lstrip('0')`.
- GX : tous les contrôles sont dans `# Quality Checks` (sauf E15), sévérité explicite.
- Écritures : passent par les wrappers qui délèguent à `leap_utils`.
- Commentaires : rappels de comportement ; aucun historique, aucune date, aucune référence à `runbooks/`.
- Docstrings : 0 problème ; `ruff` : aucune importation inutilisée.
- Jobs : `queue`, `email_notifications`, `no_alert_for_canceled_runs`, tags obligatoires, `run_as` par cible, `SINGLE_USER` sur les clusters CPU, timeout du job, fuseau `Europe/Brussels` (sauf alertes, E18).
- Notification PROD : `LEAP_Databricks_Alert@latecoere.aero`.
- Schedules : `PAUSED` ou variable.
- Tests : `tests/test_code_conventions.py`, `tests/test_requirements_pinned.py`, et une étape `pytest` en CI.

---

_Rapport produit à partir de `datab` `main` (`816e584`) et de `claude/sharp-mayer-uqwx8x` (`49237be`). Aucune modification de `datab`. Aucune exécution Databricks._
