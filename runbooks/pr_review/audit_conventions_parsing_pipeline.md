# Audit des conventions — `utils/parsing_pipeline`, `resources/` et jobs Python

Date : 2026-10-22. Audit initial le 2026-10-21 ; mise à jour le 2026-10-22 après application des corrections (statuts ci-dessous).

Branche : `claude/sharp-mayer-uqwx8x`. Base de l'audit : commit `49237be`. Les corrections sont dans le commit qui suit ce rapport.

Historique : une première version portait sur `6ca3df7`. La branche a avancé de 25 fichiers du périmètre entre les deux commits ; la version du 21 relit tout sur `49237be`.

## 0. Périmètre, sources et méthode

**Sources de règles**

| Source | Version | Rôle |
|---|---|---|
| `CLAUDE.md` de ce dépôt (sections *Notebooks*, *Jobs*, *Conventions de code*, *Conventions opérationnelles*) | branche courante | règle de référence pour qualibot |
| `datab` (dépôt personnel LEAP), branche la plus récente = `main` | `816e584` (2026-10-15) | guidelines LEAP : `CLAUDE.md`, `templates/`, `README.md` |
| `archives/qualibot_archi.zip` de `datab` (dépôt de référence) | extrait dans un dossier de travail | notebooks `proj/` et `data_asset/`, `leap_utils/`, `platform/ci/check_notebook.py` (le Convention Checker) |

Les règles de qualibot priment quand elles diffèrent de LEAP. Chaque écart est signalé avec la règle qui le fixe.

**Méthode**

1. Lecture de `datab/CLAUDE.md`, de `templates/notebook_proj_template.py`, des `templates/bundle_data_asset/` et de `platform/ci/check_notebook.py`.
2. Convention Checker LEAP exécuté sur les 8 notebooks, puis tri : vrais écarts, faux positifs, règles sans objet.
3. `check_docstrings.py` sur `utils/parsing_pipeline`, puis sur `server utils` : **0 problème** dans les deux cas, après corrections.
4. `ruff` 0.17.1 (`--no-fix`, `--isolated`) : `F401`, `F841`, `F811`.
5. Scans `grep` ciblés, lectures des YAML, de `databricks.yml` et de `bitbucket-pipelines.yml`.
6. **Vérification après corrections** : suite complète `pytest -q tests` dans un venv de travail (dépendances de `requirements.txt` sauf `fasttext-wheel`, qui ne compile pas ici, et `python-multipart`) : **704 réussis, 13 ignorés**. Les ignorés et l'absence de `fasttext` ne touchent pas aux fichiers modifiés.

**Limites**

- Rien n'a été exécuté sur Databricks : les notebooks modifiés n'ont pas tourné. Leur syntaxe Python est vérifiée, et leurs cellules restent découpées comme le veut `test_code_conventions.py`.
- Les templates Gold et Silver de `datab` ne sont lus que par leurs titres.

## 1. Synthèse

**Verdict.** Le pipeline respecte les règles dures : pas d'environnement en dur dans le code, aucun `.cast()`, `.to_date()`, `.union()`, `.saveAsTable()` ni `print()`, pas de `DBTITLE`, GX regroupé dans `# Quality Checks`, une explication Markdown avant chaque bloc de code, docstrings complètes, commentaires « pourquoi » sans historique, aucune importation inutilisée.

**Aucun bloquant au sens du Convention Checker.** Après les corrections de cette passe, il reste ouverts : la dérive des dépendances (E3), les UUID et valeurs en dur dans les YAML (E5, E6), le cluster GPU sans policy (E7), l'absence de lint et de docstrings en CI (E8), et les points de documentation E13 et E20 à E22.

### Tableau des écarts (priorité décroissante)

| # | Gravité | Écart | Où | Solution | Statut |
|---|---|---|---|---|---|
| E1 | Important | Actions Spark hors `DEBUG` : `.show()` non gardé, `.count()` servant un log | §2.3 | `debug` ajouté, garde `if DEBUG:` | **Traité** (2 gardes, 1 `count` retiré, 1 `count() > 0` → `isEmpty()`) |
| E2 | Important | `.collect()` : cinq appels dans trois notebooks | §2.3 | Le helper `collect_column_in_list` a été retiré de `leap_utils` (commit `96780e0`) | **Clos sans changement** : chaque `.collect()` restant sert une valeur Python |
| E3 | Important | Dépendances épinglées à deux endroits (notebooks `%pip` et `libraries` du YAML), dérive d'OpenCV 4.11 / 4.12 | §2.5 | Une liste de pins unique et un test | **Ouvert** |
| E4 | Important | Widgets standard absents ; environnement lu à deux endroits | §2.4 | Deux chemins documentés, défaut `dev` supprimé, `debug` ajouté | **Traité** (règle à inscrire dans CLAUDE.md, E13) |
| E5 | Important | UUID de service principal en dur dans les cibles (28 + 4) | §3.1 | Variable `dbx_service_principal` par cible | **Ouvert** |
| E6 | Important | Valeurs de pipeline en dur dans le YAML | §3.1 | Variables dans `databricks.yml` | **Ouvert** |
| E7 | Important | Cluster GPU `parse_gpu_cluster` sans policy | §3.1 | Demande d'une policy GPU-ML | **Ouvert** (demande à l'administrateur) |
| E7 bis | Important | Trois tâches lourdes sans `timeout_seconds` | §3.1 | `timeout_seconds` sur `3_parse`, `3b_parse_pdf`, `3c_chunks` | **Traité** (valeurs à caler sur un run complet) |
| E8 | Important | Pas de vérification des docstrings ni de lint en CI | §5 | Étape dans `bitbucket-pipelines.yml` | **Ouvert** |
| E9 | Mineur | `## Config Project Imports` au lieu de `## Config Function Imports` (5 notebooks) | §2.1 | Renommer | **Traité** |
| E10 | Mineur | `## Config logger` absent (3 notebooks) | §2.1 | Sous-section ajoutée | **Traité** |
| E11 | Mineur | Sections vides sans `#N/A` (3 sections) | §2.1 | `#N/A` ajouté | **Traité** |
| E12 | Mineur | README : tableau « Files » faux, lignes manquantes | §5 | Tableau corrigé, paragraphe des timeouts mis à jour | **Traité** |
| E13 | Mineur | CLAUDE.md : « every notebook calls `quality.require_gx()` » ; le code appelle `tables.require_gx()` | §5 | Corriger la règle et l'exception | **Ouvert** (hors de la liste demandée ; à faire avec la règle E4) |
| E14 | Mineur | `# /// script` serverless dans un notebook sur cluster classique | §2.2 | Bloc supprimé | **Traité** |
| E15 | Mineur | `RuntimeError` de pré-condition en `# Inputs` de `doc_catalog` | §2.2 | Documenté comme pré-condition, code inchangé | **Traité** (choix : pas de déplacement, voir §2.2) |
| E16 | Mineur | `row_number()` + `_rn > 1` au lieu de `drop_duplicates_by_order` | §2.2 | Étudié | **Non retenu** (voir §2.2) |
| E17 | Mineur | Job qui importe des fonctions privées de `server` | §6 | Noms publics | **Ouvert** |
| E18 | Mineur | Alertes en `Europe/Paris` | §3.2 | `Europe/Brussels` (6 lignes) | **Traité** |
| E19 | Mineur | Tag `project` en doublon avec `project-name` | §3.2 | Tag supprimé | **Traité** |
| E20 | Mineur | `E501` : 875 lignes de plus de 100 caractères | §4 bis | `indent-width` corrigé (2 → 4), `fix = true` retiré, formatage du pipeline ; 583 lignes restantes, dont 190 Markdown `# MAGIC`, 92 commentaires, 254 chaînes ou code | **Traité en partie** (le reste demande une réécriture manuelle) |
| E21 | Mineur | Alias `F` au lieu de `f` (16 fichiers) | §4 bis | Renommé en `f` ; trois variables `f` locales renommées (`fh`, `field`) pour éviter les conflits | **Traité** |
| E22 | Mineur | Bloc `sys.path` copié dans 7 notebooks | §4 bis | Accepté : il doit précéder les imports locaux | **Clos sans changement** |

## 2. Notebooks (`utils/parsing_pipeline/create_proj_*.py`, `update_proj_vector_indexes.py`)

### 2.1 Structure et titres

**Conforme** : en-tête `# PROJ <ASSET>` et rubriques ; ordre des sections ; blocs `## Prep<N>` et `## Tr. <N>` ; pas de `DBTITLE` ; `removeAll()` avant les widgets ; `Technical debt` rempli.

**Corrigé dans cette passe**

- **E9** : `## Config Function Imports` dans les 8 notebooks (`create_proj_category_reference.py`, `create_proj_doc_catalog.py`, `create_proj_image_metadata.py`, `create_proj_parse_manifest.py`, `update_proj_vector_indexes.py` étaient en `Config Project Imports`).
- **E10** : `## Config logger` ajouté dans `create_proj_category_reference.py`, `create_proj_image_metadata.py` et `create_proj_parse_manifest.py`, avec une ligne qui explique le rôle de la sous-section.
- **E11** : `#N/A` ajouté sous `# Data Preparation` (`create_proj_category_reference.py`, `create_proj_doc_catalog.py`) et sous `# Data Transformations` (`update_proj_vector_indexes.py`), comme le guide LEAP §3.1.

### 2.2 Code et organisation

**Conforme** : cellules de code précédées d'une explication Markdown (test `test_every_pipeline_notebook_explains_each_code_block` vert) ; écritures via les wrappers de `tables.py` ; contrôles GX dans `# Quality Checks` ; aucun idiome interdit ; commentaires sans historique.

**Corrigé dans cette passe**

- **E14** : bloc `# /// script` supprimé de `create_proj_parse_manifest.py`.
- **E15** : le contrôle d'existence des tables de `doc_catalog` reste en `# Inputs`, mais il a maintenant sa propre cellule Markdown « Pre-condition: parsing tables exist ». Le déplacer en `# Quality Checks` aurait changé le message d'erreur : il sortirait après les lectures Spark, et un table absente donnerait une `AnalysisException` au lieu du nom de la table.
- **E16 — non retenu** : `drop_duplicates_by_order()` garde la première ligne de chaque clé. Le code a besoin de tous les IDDOC sauf le plus récent. Il faudrait donc garder la première ligne, puis faire un anti-join pour retrouver les autres : un join de plus pour le même résultat. Le filtre `_rn > 1` reste le plus direct. Pas de changement.

**Clos**

- **E22** : même bloc `sys.path` dans 7 notebooks, accepté (§4 bis).

### 2.3 Actions Spark hors `DEBUG` (E1, E2)

**E1 — traité.**

- `create_proj_category_reference.py` : `.groupBy(...).count().show()` ne s'exécute plus que si `DEBUG` est vrai. Le texte du bloc Markdown dit maintenant « en mode debug ».
- `create_proj_parsing_checkpoint_gpu.py` : le message `[OUTPUT]` n'appelle plus `df_written.count()` ; il donne l'identifiant du run, et le nombre de lignes reste visible dans la table.
- `update_proj_vector_indexes.py` : `spark.table(FULL_TABLE).count()` (un parcours de toute la table `chunks_full`) n'est fait qu'en `DEBUG`.
- `create_proj_parsing_checkpoint.py` : `df_cpu.count() > 0` devient `not df_cpu.isEmpty()` : même résultat, sans compter toutes les lignes.

Les autres `.count()` restent, parce qu'ils décident d'une branche ou d'un budget, et pas seulement d'un log :

- `create_proj_image_metadata.py:140` (`pending_count` : lot et `if pending_count == 0`) ;
- `create_proj_image_metadata.py:186` (`described_count == 0` décide d'injecter ou non) ;
- `create_proj_parsing_checkpoint.py:194` (`num_files > 0`) ;
- `create_proj_parsing_checkpoint.py:222` (`enforce_parse_budget` : le budget est un nombre).

**E2 — clos sans changement de code.** Le helper `collect_column_in_list` cité dans la première version n'existe plus : `leap_utils` a été réduit au sous-ensemble utilisé par qualibot (commit `96780e0`, `quality_checks.py` retiré). Les `.collect()` restants ramènent des valeurs que le code utilise en Python :

- `create_proj_doc_catalog.py:149` : lignes du catalogue, écrites ensuite dans Lakebase ;
- `create_proj_image_metadata.py:142` : deux compteurs de statut, dans le log de production ;
- `create_proj_image_metadata.py:160` : lignes à décrire, une par image ;
- `create_proj_parse_manifest.py:139` : ensemble des IDDOC du périmètre, utilisé en Python ;
- `create_proj_parse_manifest.py:209` : dictionnaire de catégories principales.

Aucune de ces listes ne peut être remplacée par un calcul Spark sans réécrire la logique qui la consomme.

### 2.4 Widgets et environnement (E4) — traité

**Décision.** Le pipeline a deux chemins, et chacun est fixé :

- Les tâches `1_categories` à `4_describe_images` lisent `PIPELINE_WRITE_ENV` dans l'environnement du cluster (`config.py:36`).
- `5_sync_index` (`update_proj_vector_indexes.py`) et `6_update_kb_metadata` (`create_proj_doc_catalog.py`, serverless, sans variable d'environnement) reçoivent `pipeline_write_env` par `base_parameters`, lu dans un widget.

**Changements.**

- Le défaut `dev` des deux widgets est supprimé. Un paramètre manquant ne peut plus écrire en DEV sans erreur : le notebook s'arrête avec `ValueError` si la valeur n'est pas `dev`, `uat` ou `prod`.
- Le widget `debug` est ajouté dans `create_proj_category_reference.py` et `update_proj_vector_indexes.py` (il pilote les gardes E1).
- `pipeline_read_env` n'est pas ajouté : le pipeline n'écrit ni ne lit de table d'un autre environnement que `PIPELINE_WRITE_ENV`. `reference_read_env` est ajouté, pour la source Intraqual (voir §3.3). Pas de widget : ce sont des variables de cluster, comme `PIPELINE_WRITE_ENV`.

### 2.5 Dépendances (E3) — ouvert

- `create_proj_chunks.py:55` et `create_proj_parsing_checkpoint.py:58` installent en `%pip` : `numpy<2`, `pydantic>=2.10.3,<3`, `docling==2.140.0`, `docling-core==2.102.0`, etc.
- La tâche `3b_parse_pdf` (`resources/parsing_pipeline.job.yml`, ligne 257) déclare les mêmes paquets en `libraries`. Un changement à un seul endroit crée une dérive.
- OpenCV : `4.11.0.86` dans les deux notebooks CPU, `4.12.0.88` dans le notebook GPU. Le README explique pourquoi on épingle (4.13 échoue sur le runtime) ; l'écart 4.11 / 4.12 n'est expliqué nulle part.
- `requirements.txt` et `tests/test_requirements_pinned.py` existent pour l'application. Ils ne couvrent pas les paquets du pipeline.
- **Solution proposée** : une liste de pins du pipeline, lue par les `%pip -r` des notebooks, et un test sur le modèle de `test_requirements_pinned.py` qui la compare aux `libraries` du YAML. Décider si OpenCV doit être identique partout.

## 3. Jobs (`resources/*.yml`, `databricks.yml`)

### 3.1 Clusters, `run_as`, valeurs en dur, timeouts (E5, E6, E7, E7 bis)

- **E5 — ouvert** : 28 UUID dans `resources/*.yml` et 4 dans `databricks.yml`. `databricks.yml` ne déclare pas de variable `dbx_service_principal`. Solution : une variable par cible, puis `${var.dbx_service_principal}` partout.
- **E6 — ouvert** : `resources/parsing_pipeline.job.yml` lignes 301 (`llm_fallback_endpoints`), 322-323 (`vector_search_endpoint`, `embedding_model`), 339-341 (`lakebase_*`). Solution : variables par cible.
- **E7 — ouvert** : `parse_gpu_cluster` n'a pas de `policy_id`. La raison est dans un commentaire de tête de YAML. Demande d'une policy GPU-ML à l'administrateur.
- **E7 bis — traité** : `timeout_seconds` posé sur les trois tâches lourdes, et leurs valeurs écrites dans le README :
  - `3_parse` : 86400 s (24 h). Au-dessus du run de 20 h relevé dans CLAUDE.md.
  - `3b_parse_pdf` : 43200 s (12 h).
  - `3c_chunks` : 21600 s (6 h).
  Ces valeurs sont des marges, pas des mesures : à caler sur le prochain run complet. Le job reste à 48 h, et chaque tâche est plus courte que lui.
- **Clusters CPU** : `SINGLE_USER`, policy S, `num_workers: 0` : conforme.

### 3.2 Tags, fuseaux, notifications (E18, E19) — traité

| Règle | `parsing_pipeline.job.yml` | `alerts.yml` | `app_schedules.yml` | `chat_mlflow.yml` | `lakebase_sync.yml` |
|---|---|---|---|---|---|
| `queue.enabled: true` | oui | oui | oui | oui | oui |
| `email_notifications.on_failure` | oui | alertes SQL | oui | oui | oui |
| `no_alert_for_canceled_runs: true` | oui | — | oui | oui | oui |
| tags `activity-type`, `job-purpose`, `project-name` | oui (`project` retiré, E19) | — | oui | oui | oui |
| fuseau `Europe/Brussels` | oui | oui (corrigé, E18) | oui | oui | oui |
| timeout du job et des tâches | oui (48 h) ; tâches : toutes sauf la condition `3b_needs_gpu`, depuis E7 bis | — | — | — | — |
| nom `<D\|W\|M\|F\|Z>_<niveau>_<domaine>_<objet>` | `D_1_Qualibot_Parsing_Pipeline` | — | oui | oui | oui |

- **Notification PROD** : hérite de `LEAP_Databricks_Alert@latecoere.aero` (défaut racine, `databricks.yml`). Conforme.
- `tests/test_alerts.py` ne contrôle pas le fuseau : aucune modification de test nécessaire.

### 3.3 Source des métadonnées (conforme)

- Source construite à partir de l'environnement de référence : `INTRAQUAL_GOLD_CATALOG_SCHEMA = f"{REFERENCE_READ_ENV}_gold.quality_system"` (`config.py`), table `document_revisions_intraqual`. `REFERENCE_READ_ENV` est une variable de cluster posée sur les quatre clusters, et `reference_read_env` vaut `prod` dans les trois cibles (`databricks.yml`). Plus de catalogue écrit en dur par cible.

## 4. Convention Checker LEAP : tri des résultats

| Règle | Notebooks | Verdict |
|---|---|---|
| `B07` (pas de check RED) | 8 | **Faux positif** : `tables.check_dataframe` passe `validation_level=level` (`tables.py:344`). |
| `S21` (pas de `save_table`/`merge_into`) | 7 | **Faux positif** : écritures via `tables.write_table` / `merge_into`. |
| `S22` (logger) | 8 | **Faux positif** : `log_utils.setup_applevel_logger` (alias). |
| `S23` (pas de `concat_ws`) | 8 | **Sans objet** : clés construites en amont. |
| `N01` (nom de fichier) | 1 | **Exception documentée**. |
| `S07` / `S09` (widgets) | 8 / 7 | **Traité** (E4) : décision de §2.4. |
| `S08` (défaut du widget `pipeline_write_env`) | — | **Traité** (E4) : défaut supprimé. |
| `S10` (alias `f`) | 4 | **Traité** (E21) : alias passé en `f`. |
| `S12` (`.show`) | 1 | **Traité** (E1). |
| `S14` (`.count` hors GX / `DEBUG`) | 5 → 2 | **Traité** (E1) pour les logs ; les deux restants décident d'une branche (§2.3). |
| `S17` / `S31` (`.collect`) | 3 / 2 | **Clos sans changement** (E2). |

**Ruff** : `F401`, `F841`, `F811` : aucun. `F821` sur `spark`, `dbutils`, `display` : globales de notebook, attendues.

## 4 bis. E20, E21, E22 — ce qui a été fait

**E21 — alias `F` → `f`.** Fait, comme le guide LEAP et son Convention Checker le demandent. Les 15 imports sont renommés, et les trois variables `f` locales qui auraient capturé le nom sont renommées : `fh` dans `worker.py` (fichiers ouverts), `field` dans `tables.py` et `output_steps.py` (champs de schéma). Le renommage est fait sur l'AST, pas par remplacement de texte. Vérifié : ruff `F` sans erreur sur les noms, suite de tests verte.

**E20 — longueur des lignes.** Fait en partie.
- `pyproject.toml` : `indent-width` passe de 2 à 4 (le code est en 4 espaces ; la valeur 2 aurait réindenté tout le dépôt si `ruff format` était lancé). `fix = true` retiré : un contrôle ne doit pas réécrire les fichiers.
- `ruff format` appliqué à `utils/parsing_pipeline` (les lignes `%pip` des notebooks sont mises de côté le temps du formatage, puis restaurées).
- Il reste 583 lignes de plus de 100 caractères : 190 lignes Markdown `# MAGIC`, 92 commentaires, 153 chaînes ou littéraux, 101 lignes de code que le formateur ne sait pas couper. Leur réécriture est manuelle. Proposition : une passe séparée, sur les commentaires et le Markdown, relue à la main.
- Pas de porte CI sur `E501` : la limite serait rouge tant que la passe n'est pas faite. La porte CI, si elle est ajoutée, se limite à `F` et `E9`.

**E22 — bloc `sys.path`.** Clos sans changement. Le bloc doit précéder les imports locaux (`tables`, `config`) ; une factorisation passerait par `%run`, non vérifiable sans Databricks.

## 4 ter. Environnement : les noms des variables

Question posée : le pipeline doit-il utiliser partout `PIPELINE_WRITE_ENV`, `PIPELINE_READ_ENV` et `REFERENCE_READ_ENV` comme le template ?

- `PIPELINE_WRITE_ENV` : déjà le nom du template, utilisé partout pour les écritures.
- `REFERENCE_READ_ENV` : ajouté pour la source Intraqual, qui est une donnée de référence d'un autre asset. Le template LEAP l'utilise déjà pour ce cas (`reference_read_env`, `prod` par défaut, `leap_utils/data_asset/table_utils.py` en fait autant pour les taux de change).
- `PIPELINE_READ_ENV` : non ajouté. Le pipeline ne lit aucune table d'un autre environnement que la sienne ; une variable sans usage serait du bruit.
- Les `PARSING_*` restants (`PARSING_RUN_MODE`, `PARSING_LLM_*`, etc.) sont des réglages du projet, pas des environnements. Les renommer en noms génériques n'apporterait rien.
- Le catalogue Intraqual était écrit en dur par cible (`prod_gold.quality_system`), et une variable `PARSING_INTRAQUAL_SUFFIX` était passée au cluster sans être lue. Les deux sont supprimées ; `databricks.yml` ne garde que `reference_read_env`.
- Garde : `tests/test_parsing_env_names.py` vérifie que chaque cluster reçoit `PIPELINE_WRITE_ENV` et `REFERENCE_READ_ENV`, qu'aucune des variables retirées ne revient, et que la source se dérive bien de `REFERENCE_READ_ENV`.

## 5. Documentation et CI

- **E12 — traité** : tableau « Files » du README corrigé (`create_proj_chunks.py` = `3c_chunks`, ajout des lignes `create_proj_parsing_checkpoint.py` et `create_proj_parsing_checkpoint_gpu.py`). Le paragraphe des timeouts indique les nouvelles limites.
- **E13 — ouvert** : CLAUDE.md ligne 25 dit « Every notebook calls `quality.require_gx()` ». Le code appelle `tables.require_gx()` (7 notebooks) ; `create_proj_doc_catalog.py` n'appelle rien (serverless). Corriger la règle, et inscrire la règle des widgets (E4) et la convention `F` (E21) si ces points sont retenus.
- **E8 — ouvert** : `bitbucket-pipelines.yml` lance `pytest -q tests` avant chaque déploiement PROD (étape `tests`). Il manque `check_docstrings.py server utils` et une étape de lint non destructive (voir E20).

## 6. Autres jobs Python

| Fichier | Constat | Gravité |
|---|---|---|
| `utils/lakebase_sync/*_job.py`, `utils/app_mgmt/stop_start_app_job.py`, `utils/evaluation/*.py` (notebooks) | en-tête Databricks, `logger`, pas de `print()`, `removeAll()`, pas de `DBTITLE`, pas d'historique | conforme |
| `utils/evaluation/pairwise_answers.py:156-160` | importe 4 fonctions privées de `server` | Mineur (E17, ouvert) |
| `resources/*.yml` | UUID en dur | Important (E5, ouvert) |

## 7. `leap_utils/` : dérive par rapport à l'archive

**Mise à jour.** `leap_utils/` de ce dépôt n'est plus une copie complète : il a été réduit au sous-ensemble utilisé par qualibot (commit `96780e0`, « leap_utils trimmed »). Par rapport à l'archive :

- 24 fichiers absents ici (dont `common/validation/quality_checks.py`, qui contenait `collect_column_in_list`) ; le reste est identique, sauf `data_asset/table_utils.py`.
- `table_utils.py` : le flag `FRESHNESS_PILOT_ENABLED = False` désactive l'écriture du pilote de fraîcheur, qui écrirait dans `{env}_bronze`. Voulu et documenté.
- Les autres différences sont des fins de ligne (CRLF dans l'archive).

**Action** : aucune. Tout ajout d'une fonction de `leap_utils` doit être vérifié dans l'archive avant usage.

## 8. Lacunes des guidelines LEAP (`datab`) — à compléter, sans modifier le dépôt

1. **Environnement d'un pipeline multi-tâches** : variable de cluster en priorité, widget seulement pour le serverless.
2. **Cluster GPU** : dérogation à « policy obligatoire » tant que l'organisation n'en fournit pas, avec un calendrier de levée.
3. **Dépendances** : une liste de pins pour les notebooks et le YAML, avec un test de cohérence.
4. **Alias pyspark** : `F` ou `f`.
5. **Écritures** : un wrapper local (`tables.py`) est accepté si chaque écriture délègue à `save_table` ou `merge_into` ; l'exception `INSERT OVERWRITE` (clé primaire) est à inscrire.
6. **Faux positifs du Convention Checker** : `B07`, `S21`, `S22`, `S23` quand les checks passent par un wrapper.
7. **Pré-conditions** : un contrôle d'existence d'une table en entrée est une pré-condition documentée, pas un check groupé.
8. **Timeouts par tâche** : valeur à poser sur chaque tâche, calée sur la durée observée, pas une limite héritée du job.

## 9. Actions restantes

**Fait dans cette passe** : E1, E4, E7 bis, E9 à E12, E14, E15 (documenté), E16 (non retenu), E18, E19 ; E2 clos sans changement.

**À faire, par ordre**

1. Calibrer les trois timeouts sur le prochain run complet (E7 bis).
2. Inscrire dans CLAUDE.md la règle des environnements (E4, §4 ter) et corriger la règle `quality.require_gx()` (E13).
3. Passe manuelle sur les 583 lignes longues restantes : commentaires et Markdown `# MAGIC` (E20, §4 bis).
4. Pins du pipeline dans une seule liste, avec un test (E3).
5. Variables `dbx_service_principal` et valeurs de pipeline dans `databricks.yml` (E5, E6).
6. Demande de policy GPU-ML (E7).
7. Porte de docstrings et de lint en CI (E8).
8. E17 au prochain changement de `pairwise_answers.py`.

---

_Rapport produit à partir de `datab` `main` (`816e584`) et de `claude/sharp-mayer-uqwx8x` (`49237be`), corrections appliquées le 2026-10-22. Aucune modification de `datab`. Aucune exécution Databricks._
