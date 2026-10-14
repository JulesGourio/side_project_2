# utils/

Tout ce qui tourne en dehors de l'application web : chaîne de parsing des
documents, déploiement, alertes, copies de données, évaluation du chat et
outils d'administration. Un dossier par sujet. L'application elle-même est
dans `server/` et `client/` (voir le [README racine](../README.md)).

## Carte du dossier

| Dossier ou fichier | Contenu | Lancé par |
|---|---|---|
| [`parsing_pipeline/`](parsing_pipeline/README.md) | Chaîne quotidienne de parsing, de découpage et d'indexation des documents (tâches `1_` à `6_`) | `resources/parsing_pipeline.job.yml` |
| [`deploy/`](deploy/) | `deploy_qualibot.ps1` (déploiement manuel de l'application), `render_target_config_env.py` et `target_env.json` (réglages de l'application par cible), outils d'extraction Intraqual (`scrap_ids_intraqual.py`, `download_intraqual.py`, `unzip_intraqual.py`, `upload_intraqual.py`) | à la main ; `bitbucket-pipelines.yml` pour `render_target_config_env.py` |
| [`alerts/`](alerts/README.md) | Requêtes SQL des six alertes de suivi (à créer à la main dans Databricks) | à la main |
| [`app_mgmt/`](app_mgmt/) | `stop_start_app_job.py` : arrêt et démarrage planifiés des applications Databricks | `resources/app_schedules.yml` |
| [`lakebase_sync/`](lakebase_sync/README.md) | Export, import, copies et mise à jour du schéma de la base Lakebase | `resources/lakebase_sync.yml`, `bitbucket-pipelines.yml` |
| [`dev_copy/`](dev_copy/) | Copie du corpus de l'UAT vers DEV : export sur l'UAT, import dans DEV, endpoint Vector Search | `resources/dev_copy.yml` |
| [`grants/`](grants/) | Droits Unity Catalog des identités de service (application, pipeline), accès aux volumes | `resources/grants.yml` |
| [`uat_setup/`](uat_setup/) | `setup_proj_schema.py` : copie l'état du parsing dans le schéma du projet de la cible `qualibot-uat-test` | `resources/uat_setup.yml` |
| [`evaluation/`](evaluation/) | Évaluation du chat et de la recherche : tests de charge, comparaison de réponses, évaluation de la recherche, expérience et prompts MLflow, scores des traces | `resources/chat_mlflow.yml`, à la main |
| [`soffice_packaging/`](soffice_packaging/) | Construction de l'archive LibreOffice portable utilisée par l'aperçu PDF fidèle des comparaisons | à la main (archive déposée dans un volume) |
| [`compare_preview.py`](compare_preview.py) | Rapport hors ligne d'une paire de documents à comparer (pas d'appel LLM) | `python -m utils.compare_preview` |
| [`ops_config.py`](ops_config.py) | Réglages partagés : profils CLI, projets et bases Lakebase, connexion (`lakebase_connect*`) | importé par les scripts |

## Conventions

- Journalisation avec `logger` (module `logging`), jamais `print`. Les notebooks
  et les scripts en ligne de commande suivent la même règle.
- Un notebook de `utils/` reçoit ses paramètres par widgets. Il commence par
  `dbutils.widgets.removeAll()`, puis déclare ses widgets.
- Les notebooks importent `ops_config.py` avec
  `sys.path.insert(0, os.path.dirname(os.getcwd()))` : le dossier du notebook
  est dans `sys.path`, son parent est `utils/`.
- Sur les colonnes de données, `try_cast` et `try_to_date` remplacent
  `cast` et `to_date`, pour qu'une valeur mal formée ne fasse pas échouer
  toute la tâche.
- `target_env.json` est la source unique des réglages de l'application par
  cible. Le script PowerShell et le pipeline Bitbucket les lisent tous les
  deux : ne pas les recopier ailleurs.

## Points de vigilance

- **Arrêt et démarrage des applications** (`app_mgmt/stop_start_app_job.py`) :
  le SDK lève `BadRequest` quand l'application est déjà dans l'état demandé.
  Le notebook lit donc d'abord l'état et passe l'appel si l'application y est
  déjà.
- **Fins de ligne de `target_env.json` rendu** : `render_target_config_env.py`
  écrit `target_config.env` en fins de ligne LF. Un fichier en CRLF ajoute un
  `\r` à chaque valeur, et toutes les URL construites à partir de ces valeurs
  deviennent fausses.
- **Notebook importé dans le workspace** : `databricks workspace import` doit
  viser exactement le `notebook_path` du job (sans suffixe `.py`). Sinon, un
  doublon apparaît sans erreur.
- **Déploiement** : l'application se déploie avec `deploy/deploy_qualibot.ps1`
  (ou le pipeline Bitbucket), et les jobs avec `databricks bundle deploy`. Le
  [README racine](../README.md) décrit les cibles. Les étapes à faire dans
  Databricks sont dans [runbooks/uat.md](../runbooks/uat.md) et
  [runbooks/dev.md](../runbooks/dev.md).
