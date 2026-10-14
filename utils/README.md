# utils/

Scripts qui tournent en dehors de l'application web : chaîne de parsing des
documents, déploiement, alertes, évaluation du chat et de la recherche,
droits Unity Catalog et outils d'exploitation Lakebase. Un dossier par sujet.
L'application elle-même est dans `server/` et `client/` (voir le
[README racine](../README.md)). Les scripts ponctuels (migrations, copies,
empaquetages lancés une fois) sont dans [`oneshot/`](../oneshot/README.md) et
ne sont jamais déployés.

## Carte du dossier

| Dossier ou fichier | Contenu | Lancé par |
|---|---|---|
| [`parsing_pipeline/`](parsing_pipeline/README.md) | Chaîne quotidienne de parsing, de découpage et d'indexation des documents (tâches `1_` à `6_`) | job `parsing_pipeline` (`resources/parsing_pipeline.job.yml`) |
| [`deploy/`](deploy/) | `deploy_qualibot.ps1` : déploiement manuel du code de l'application. `render_target_config_env.py` : écrit `target_config.env` à partir de `target_env.json`. `target_env.json` : réglages de l'application par cible | `deploy_qualibot.ps1` à la main ; `render_target_config_env.py` par le pipeline Bitbucket (`bitbucket-pipelines.yml`) |
| [`alerts/`](alerts/README.md) | Six requêtes SQL d'alertes de suivi du parsing, du chat et des jobs | à la main, dans Databricks SQL |
| [`app_mgmt/`](app_mgmt/) | `stop_start_app_job.py` : arrêt et démarrage planifiés des applications Databricks | jobs de `resources/app_schedules.yml` |
| [`lakebase_sync/`](lakebase_sync/README.md) | Export de la base Lakebase vers un volume, import vers DEV, export et import JSON vers PROD | jobs de `resources/lakebase_sync.yml` ; étapes « Sync prod data » de `bitbucket-pipelines.yml` |
| [`dev_copy/`](dev_copy/) | Import du corpus copié de l'UAT dans DEV, et création de l'endpoint Vector Search | job `copy_uat_to_dev` (`resources/dev_copy.yml`) |
| [`grants/`](grants/) | Droits Unity Catalog des identités de service (application, pipeline) et accès aux volumes | jobs de `resources/grants.yml` |
| [`evaluation/`](evaluation/) | Évaluation du chat et de la recherche : tests de charge, comparaison de réponses, évaluation de la recherche, expérience et prompts MLflow, scores des traces, contrôle des endpoints de vision | `create_chat_experiment.py`, `register_chat_prompts.py`, `score_chat_traces.py` : jobs de `resources/chat_mlflow.yml` ; le reste à la main (voir [runbooks/dev.md](../runbooks/dev.md)) |
| [`ops_config.py`](ops_config.py) | Réglages partagés : profils CLI, bases Lakebase par environnement, connexion (`lakebase_connect`, `lakebase_connect_with`), listes d'ordre des tables | importé par `lakebase_sync/` et `oneshot/lakebase/` |

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
