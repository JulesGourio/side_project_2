# Alertes SQL (Databricks SQL)

Six requêtes SQL, une alerte par requête. Chaque requête renvoie une colonne
qui déclenche l'alerte quand sa condition est vraie. Les requêtes lisent les
tables du schéma du projet (`dev_proj.qualibot` dans les fichiers ; pour
l'UAT, remplacer `dev_proj` par `uat_proj`). Les alertes sont créées à la
main dans Databricks : étapes dans [runbooks/uat.md](../../runbooks/uat.md)
(section D5.8d).

| Fichier | Déclenche quand (fenêtre) | Colonne et condition | Table lue |
|---|---|---|---|
| `01_parsed_without_chunks.sql` | Un document `SUCCESS` destiné au RAG n'a aucun passage dans `chunks` 4 h après son parsing (documents des 7 derniers jours) | `docs_without_chunks` > 0 | `processed_files`, `chunks` |
| `02_parse_errors.sql` | La dernière tentative de parsing d'un document est `ERROR` ou `TIMEOUT` (24 h). Un document réparé par une nouvelle tentative n'est pas compté | `docs_in_error` > 0 | `processed_files` |
| `03_chat_failure_rate.sql` | Sur la dernière heure, trop de réponses du chat sont en erreur ou sans source. Le seuil dépend du trafic : en dessous de 5 messages, rien ne se déclenche | `alert_triggered` = 1 | `chat_messages` |
| `04_job_failures.sql` | Un job dont le nom contient `_Qualibot_` a échoué sur les 24 dernières heures | `failed_runs` > 0 | `system.lakeflow` |
| `05_new_error_kinds.sql` | Une nouvelle `fingerprint` d'erreur (route, étape, type, emplacement dans le code) apparaît sur les 24 dernières heures et n'existe pas dans la table. Les avertissements (`severity` = `warning`) sont exclus | `new_error_kinds` > 0 | `errors` |
| `06_chat_degraded_turns.sql` | Sur 24 heures, au moins 20 % des tours de chat (et au moins 10 tours) ont `status` = `degraded` (étape non exécutée comme configurée) | `alert_triggered` = 1 | `chat_turns` |

Les colonnes de contexte (`sample_refs`, `failed_jobs`, `sample_errors`,
`top_causes`) servent au modèle de message de l'alerte : elles listent les
documents, les jobs, les erreurs ou les causes concernés.

## Fréquence et destinataires

Les alertes se configurent dans Databricks (planification, entrepôt SQL
serverless, destinataires, modèle de message). Les destinataires ne sont pas
dans le dépôt. Pour 06 et 03, une seule notification par jour suffit ;
sinon, un problème persistant envoie un message à chaque évaluation.

## Limites

- `03`, `05` et `06` lisent les copies Delta de `chat_messages`, `errors` et
  `chat_turns`, alimentées par les jobs d'export et d'import décrits dans
  [`utils/lakebase_sync/README.md`](../lakebase_sync/README.md). Elles ne sont
  donc aussi fraîches que cette copie. Tant que la table n'existe pas (aucune
  ligne importée), la requête échoue avec « table not found ».
- `04` exige `SELECT` sur `system.lakeflow` pour le propriétaire de l'alerte.
- `01` et `02` lisent `processed_files` : elles ne voient que les documents
  parsés par la chaîne quotidienne (voir
  [`utils/parsing_pipeline/README.md`](../parsing_pipeline/README.md)).
- Ne sont pas couverts : la fraîcheur des sources Intraqual (la tâche
  `1_categories` échoue déjà dans ce cas, et `04` le signale) et la santé des
  index Vector Search.
