# QualiBOT : passation

Complète le README (qui décrit le code). Ici : ce qu'il faut savoir pour **opérer** l'app depuis un poste **Windows** (PowerShell, navigateur Bitbucket et Databricks). Les noms de fichiers sont ceux du dépôt.

## 1. Architecture

- **App** : FastAPI (`server/`) + React (`client/`), Databricks App `qualibot`. Trois fonctions : comparaison de documents, recherche d'impact, chat.
- **Données** : une chaîne de parsing quotidienne produit des tables Delta et un index Vector Search `chunks_index`, lu par le chat et la recherche d'impact (`utils/parsing_pipeline/README.md`).
- **Historique** (conversations, feedbacks, erreurs, caches) : Lakebase (Postgres), projet `qualibot`, base `doccompare`. Colonnes : `server/services/infra/lakebase/schema.md`.
- **Source des documents** : Intraqual, catalogue `prod_landingzone.intraqual` (lu par les trois envs, jamais modifié).
- **Déploiement** : bundle Databricks `qualibot` (`databricks.yml`, `resources/*.yml`) lancé par des pipelines Bitbucket (`bitbucket-pipelines.yml`), voir §8.

| Env | Workspace | Tables | SP qui exécute les jobs |
|---|---|---|---|
| DEV | `dbc-c623749d-731b` | `dev_proj.qualibot` | `fde6ff28-739f-4a41-b61e-604a298c8478` (job-runner-sa-dev) |
| UAT | `dbc-3a17bfce-9e88` | `uat_proj.qualibot` | `3e5cd4e5-f765-4760-b974-ee7715258b39` (job-runner-sa-uat) |
| PROD | `dbc-77b87d5a-6bcb` | `prod_proj.qualibot` | `b82d3b5a-b633-4738-8dde-68bb2142cb02` (job-runner-sa-prod) |

- Mêmes noms de tables, volumes et index dans chaque env. Volumes : `doc_compare` (outils LibreOffice), `images` (images extraites), `staging` (échanges et exports), `docling_models`.
- Profils CLI Windows : `DEV`, `UAT`, `PROD` (fichier `%USERPROFILE%\.databrickscfg`).
- Groupes : CoreAdmin et CoreDev (CAN_MANAGE) ; End-users DocCompare et ChatBot (CAN_USE).
- Alertes SQL : `resources/alerts.yml`.
- Ordre de travail : on change et on teste en DEV, puis UAT, puis PROD. Les copies entre envs passent par un volume `staging` (export dans un env, import dans l'autre), jamais par une copie directe de table.

## 2. Les jobs

Définis dans `resources/`. Mêmes noms dans les trois envs. Fuseau : Europe/Brussels.

| Job | Rôle | Planning | DEV / UAT / PROD |
|---|---|---|---|
| `D_1_Qualibot_Parsing_Pipeline` | Parsing, chunks, descriptions d'images, index (§3) | tous les jours 04:00 | en pause / actif / actif |
| `D_1_Qualibot_Lakebase_Export_To_Volume` | Copie les tables Lakebase vers le volume `staging` puis vers des tables Delta (alimente les alertes et le dashboard d'usage) | 02:00 et 14:00 | actif partout |
| `D_2_Qualibot_User_Capabilities_Sync` | Lit `system.access.audit` et écrit les groupes de chaque utilisateur dans la table `users` du Lakebase (base de l'accès à l'app) | toutes les 2 h | actif partout |
| `D_2_Qualibot_Score_Chat_Traces` | Note les traces MLflow du chat (juges LLM sur échantillon) | 02:30 | en pause / actif / en pause |
| `Z_2_Qualibot_Create_Chat_Experiment` | Crée l'expérience MLflow des traces | manuel | |
| `Z_3_Qualibot_Register_Chat_Prompts` | Enregistre les prompts du chat dans MLflow (à relancer après toute modification d'un prompt) | manuel | |
| `Z_4_Qualibot_Retrieval_Eval` | Évaluation de la recherche du chat (§7) | manuel | |
| `D_1_Qualibot_Apps_Stop_Nightly_Dev` | Arrête l'app DEV | tous les jours 21:00 | DEV seulement |
| `W_1_Qualibot_Stop_Weekend_<Env>` | Arrête l'app | vendredi 21:00 | UAT actif ; DEV et PROD en pause |
| `W_1_Qualibot_Start_Weekend_<Env>` (PROD : `Start_Weekday_Prod`) | Démarre l'app | lundi 07:00 | UAT actif ; DEV et PROD en pause |

Les plannings se changent dans `databricks.yml` (variables `parsing_schedule_pause`, `lakebase_export_schedule_pause`, `alerts_pause_status`) puis `deploy-<env>-jobs`.

### Lancer un job à la main (PowerShell)

Interface : Databricks → Jobs → le job → **Run now with different parameters**. En ligne de commande :

```powershell
# 1. trouver l'id du job (remplacer DEV par UAT ou PROD)
databricks jobs list --profile DEV -o json | ConvertFrom-Json |
  Where-Object { $_.settings.name -eq 'D_1_Qualibot_Parsing_Pipeline' } |
  Select-Object job_id

# 2. lancer avec des paramètres (fichier en ASCII : PowerShell 5 ajoute sinon un BOM qui casse le JSON)
$id = 123456789   # job_id trouvé ci-dessus
@{ job_id = $id; job_parameters = @{ run_mode = 'incremental'; max_parse_files = '500' } } |
  ConvertTo-Json -Depth 5 | Set-Content run.json -Encoding ascii
databricks jobs run-now --profile DEV --json "@run.json"
```

Le job de parsing a `max_concurrent_runs: 1` et une file : un deuxième run attend le premier.

## 3. Pipeline de parsing

Détail : `utils/parsing_pipeline/README.md`.

`1_categories` → `2_manifest` → `3_parse` → (`3b_parse_pdf` sur GPU, seulement s'il y a plus de 30 PDFs à traiter) → `3c_chunks` → `4_describe_images` → `5_sync_index` → `6_update_kb_metadata`

| Tâche | Ce qu'elle fait |
|---|---|
| `1_categories` | Lit les tables Intraqual (documents, catégories), échoue si elles n'ont pas été mises à jour depuis plus de `max_source_staleness_hours` |
| `2_manifest` | Fixe la liste des documents du périmètre (table `parse_manifest`) et supprime les chunks des documents sortis du périmètre |
| `3_parse` | Choisit les fichiers à parser, les parse (Docling ou lecteurs de secours) et écrit le résultat dans le checkpoint |
| `3b_parse_pdf` | Docling sur GPU pour les gros lots de PDFs |
| `3c_chunks` | Découpe le texte en passages (`chunks`), crée les lignes d'images à décrire |
| `4_describe_images` | Un modèle de vision décrit chaque image, la description devient un passage |
| `5_sync_index` | Synchronise l'index Vector Search `chunks_index` |
| `6_update_kb_metadata` | Écrit le catalogue de documents et la date de la base de connaissances dans le Lakebase |

### Paramètres du job

| Paramètre | Défaut | Rôle |
|---|---|---|
| `run_mode` | `incremental` | `incremental` : ne parse que les nouveaux documents ou ceux dont le contenu a changé. `full` : reparse tout (le checkpoint est cloné avant). `rechunk` : refait seulement le découpage à partir du checkpoint, sans rien parser |
| `parse_filter` | vide | Liste d'IDDOC : limite le run à ces documents. Uniquement avec `incremental` |
| `max_parse_files` | 500 | Nombre maximum de fichiers parsés par run. Il existe parce que très peu de documents changent chaque jour : un run quotidien en traite quelques-uns. Le plafond protège d'un run qui repartirait de zéro (état vide, nouveau périmètre) et saturerait les clusters et les endpoints LLM pendant des heures. Les fichiers restants sont repris au run suivant. `0` = pas de plafond (utile pour un rebuild voulu) |
| `max_source_staleness_hours` | 36 | Garde de fraîcheur de `1_categories` ; `0` la désactive |
| `redescribe_all_images` | false | `true` : redécrire toutes les images |
| `describe_images` | true | `false` : aucun appel au modèle de vision (les descriptions déjà faites sont quand même réinjectées) |

### Notions clés

- **Checkpoint** : la table `parsing_checkpoint`. Une ligne par tentative de parsing d'un fichier, avec l'IDDOC du document, `document_sha256` (empreinte du contenu du fichier), `ingestion_timestamp`, `parse_status` (`SUCCESS`, `ERROR`, `EMPTY_TEXT`, …), le parseur utilisé (Docling, XML, etc.) et le contenu extrait. C'est le cache du parsing : au run suivant, un document n'est reparsé que si le couple (IDDOC, `document_sha256`) n'a pas de ligne `SUCCESS` réutilisable. On vérifie donc sur le **contenu du fichier** (son empreinte), pas sur sa date ni son chemin. La table garde l'historique des tentatives : pour compter les états, prendre la dernière ligne de chaque IDDOC.
- **Un IDDOC = une révision.** Chaque révision d'un document a un nouvel IDDOC ; l'ancien sort du périmètre et ses chunks sont supprimés. Si plusieurs IDDOC partagent une REF, seule la révision la plus récente garde ses chunks.
- **Périmètre** : `parse_manifest` ne contient que les documents Intraqual courants (dernière révision, état validé, non masqués) qui sont dans l'une des catégories racines retenues (processus One QMS, AS programmes, AS instructions de travail, AS processus, directives groupe, training, VA, organization) ou dans les deux sous-catégories « To be classified ». La liste est dans `utils/parsing_pipeline/config.py` (`SCOPE_ROOT_IDCATS`, `SCOPE_SUB_IDCATS`). Garde-fou : si le nombre de documents du périmètre baisse de plus de 30 % par rapport au run précédent (ce qui signale presque toujours un problème de lecture des sources, pas un vrai retrait), `2_manifest` échoue sans rien supprimer ; pour une réduction voulue, mettre `PARSING_MAX_SCOPE_DROP_PCT` à 0 le temps d'un run.
- **Parseurs** : PDF, DOCX, PPTX et HTML passent par Docling. `xml_fallback` : lecteur XML de secours des DOCX/PPTX quand Docling échoue. Un PDF scanné (moins de 150 caractères de texte) passe en OCR par le modèle de vision (statut `pending_llm_ocr` en attendant).
- **Catégorie manquante** : si une révision n'a pas de catégorie, on regarde si une révision précédente du même document en a une ; si oui elle est reprise, sinon le document est ignoré.
- **Tailles de passages** (`utils/parsing_pipeline/config.py`) : `MIN_CHUNK_TOKENS = 300`, `TARGET_CHUNK_TOKENS = 450`, `MAX_CHUNK_TOKENS = 550`, `MAX_CHUNK_CHARS = 1950`. Un passage est coupé aux titres de niveau 1 à 3 ; il vise 450 tokens, ne descend pas sous 300 (un morceau trop petit est fusionné avec le suivant), ne dépasse pas 550 tokens ni 1950 caractères (le plafond en caractères protège les lignes de points et de tirets, qui coûtent peu de tokens). Deux passages voisins se recouvrent de 12 %. Le reranker de Vector Search ne lit que les 2000 premiers caractères d'un passage, d'où le plafond sous 2000. Les mêmes valeurs sont en repli dans `utils/parsing_pipeline/worker.py` : les garder identiques. Ces valeurs s'appliquent aux nouveaux documents et à tout `rechunk` ou `full` ; les passages déjà en base gardent leur ancienne taille tant qu'aucun `rechunk` n'est lancé (non lancé pour l'instant). Pour les changer : modifier ces constantes (ou les variables `PARSING_MIN_CHUNK_TOKENS`, `PARSING_TARGET_CHUNK_TOKENS`, `PARSING_MAX_CHUNK_TOKENS`, `PARSING_MAX_CHUNK_CHARS`), déployer avec `deploy-<env>-jobs`, puis lancer un run `rechunk`.
- **Images** : extraites dans le volume `images` par `3_parse`, décrites par `4_describe_images`. Modèles de vision : `databricks-gpt-6-luna` avec repli `databricks-gpt-5-6-luna`, chacun avec son propre quota. Embeddings de l'index : `databricks-qwen3-embedding-0-6b`.

### Pièges

- `full` et `rechunk` **écrasent `chunks`** : les passages d'images disparaissent jusqu'à la fin de `4_describe_images`, et l'index re-vectorise tout (long).
- Ne jamais lancer `full` ou `rechunk` avec `parse_filter`.
- Un run avec `parse_filter` supprime quand même (dans `2_manifest`) les chunks des anciennes révisions des documents ciblés sans parser les nouvelles : ces documents disparaissent de l'index jusqu'au prochain run incrémental normal. Après un run ciblé, lancer un incrémental.
- Les lignes `ERROR` du checkpoint sont retentées à chaque run incrémental.
- Ne pas supprimer des lignes du checkpoint sans vérifier quels documents perdraient leur seule ligne : ils seraient reparsés.
- Supprimer et recréer un index supprime ses droits : redonner `SELECT` sur la table des chunks au SP de l'app.

## 4. Index et volumes

- Un seul index pour le chat et la recherche d'impact : `<env>_proj.qualibot.chunks_index` (filtre `division` pour AS / IS). L'index utilisé par l'app ne doit pas changer sans prévenir : l'app redémarre automatiquement.
- `chunks_baseline_index` (DEV) : index de comparaison pour l'évaluation, à reconstruire avant chaque benchmark.
- Catalogue de documents (REF, titres, révisions) : table Lakebase `doc_catalog`, réécrite par `6_update_kb_metadata`, rechargée par l'app toutes les 30 minutes.

## 5. Lakebase

Projet `qualibot`, branche `production`, base `doccompare`, un par env (jamais copié d'un env à l'autre en fonctionnement normal). L'app crée et complète son schéma au démarrage et s'y connecte avec son propre SP (rôle `app-doc-compare-sp`, id en dur dans `databricks.yml` : à changer seulement si l'app est recréée). Colonnes de chaque table : `server/services/infra/lakebase/schema.md`.

| Groupe | Tables | Écrites par |
|---|---|---|
| Chat | `chat_sessions` (conversation), `chat_messages` (messages), `chat_feedbacks` (pouces), `chat_turns` (un tour = question + réponse, avec étapes, durées, modèles, tokens, coût, statut), `chat_retrieved_chunks` (chaque passage trouvé, gardé ou écarté) | l'app |
| Comparaison | `compare_messages` (historique), `compare_feedbacks`, `llm_requests` (un appel d'analyse) | l'app |
| Recherche d'impact | `impact_requests`, `impact_document_results` (un document jugé), `impact_feedbacks` | l'app |
| Caches | `impact_cache`, `analysis_cache`, `summary_cache` | l'app |
| Erreurs | `app_errors` (erreur ou avertissement d'une étape, avec `severity`, `stage`, `origin`) | l'app |
| Utilisateurs | `users` (utilisateur connu, `groups`, `can_chat`, `can_compare`, `groups_synced_at`) | l'app, et le job `D_2_Qualibot_User_Capabilities_Sync` pour les groupes |
| Catalogue | `doc_catalog` (REF, titre, lien, révision, date de chaque document) ; `doc_references` (pour chaque code de référence, les documents qui le citent) | tâche `6_update_kb_metadata` après chaque run de parsing |
| Date de la base | `catalog_metadata` (une ligne, `id = 1` : « documents à jour au ») | tâche `6_update_kb_metadata` ; saisie à la main possible |

### Les « codes »

- **Codes d'avertissement d'un tour** (`chat_turns.warnings`, un code par problème ; le détail est dans `app_errors` avec le même `trace_id`). Le statut d'un tour est `ok`, `degraded` (au moins un avertissement), `error` ou `aborted`.

| Code | Sens |
|---|---|
| `translate_in_failed` | traduction de la question en échec : recherche avec la question d'origine |
| `rewrite_failed` / `rewrite_fallback` | aucune réécriture / réécriture faite par le modèle de repli |
| `vs_partial` | certaines requêtes Vector Search ont échoué |
| `rerank_refused` | l'index a refusé le reranker |
| `no_passages` | aucun passage trouvé |
| `llm_fallback` / `llm_retried` | réponse donnée par le modèle de repli / appels retentés (429, timeout) |
| `answer_continued` / `answer_interrupted` | réponse coupée puis reprise / coupée sans reprise possible |
| `translate_back_failed` | traduction de la réponse en échec |
| `persist_failed`, `user_upsert_failed`, `session_not_owned` | écriture des messages, mise à jour de `users`, session d'un autre utilisateur |
| `query_truncated` | requête trop longue coupée |
| `client_disconnected`, `send_failed` | information sur la connexion du navigateur |

- **Codes de référence** (ex. `SF-1027`) : à chaque passage, `cited_codes` liste les codes qu'il cite et `chunk_preprocessed` les écrit sous une seule orthographe (`SF-1027 [SF1027]`), à l'identique côté requêtes de l'app (module `utils/parsing_pipeline/code_normalization.py`). `doc_references` est construite à partir de `cited_codes` : elle permet de répondre à « quels documents citent X ? » avec la liste complète.
- **Jointure** : `trace_id` (`chat-` + 32 caractères hexadécimaux) relie `chat_turns`, `chat_retrieved_chunks`, `app_errors` et `chat_messages` ; `chat_turns.mlflow_trace_id` relie au trace MLflow.

### Export du Lakebase vers Delta

Job `D_1_Qualibot_Lakebase_Export_To_Volume`, deux tâches : `export_lakebase_to_volume` (une table = un fichier JSON Lines dans `/Volumes/<env>_proj/qualibot/staging/lakebase_export`) puis `import_lakebase_tables_to_delta` (charge chaque fichier dans une table Delta du même nom dans `<env>_proj.qualibot`, saute une table vide). Serverless obligatoire (le Lakebase n'est pas joignable depuis un cluster classique). Les variables `lakebase_import_to_delta` (charger ou non) et `lakebase_tables_to_skip` (tables exclues de l'export) se règlent par env dans `databricks.yml`. Les fichiers contiennent des données utilisateur (questions, réponses, identifiants) : ils restent dans le volume.

### Qui lit les copies Delta

- Le dashboard Lakeview **Qualibot Usage Tracking** (suivi d'usage). Les tables exactes qu'il lit sont définies dans le dashboard lui-même, pas dans le dépôt : **à vérifier dans Databricks**. La note de limite du README indique que celui de DEV lit encore d'anciennes tables.
- Les alertes SQL (`resources/alerts.yml`, une série par env, mêmes noms) :

| Alerte | Tables lues | Heure |
|---|---|---|
| Documents sans passages | `parsed_documents`, `chunks` | 09:00 |
| Erreurs de parsing | `parsed_documents` | 09:00 |
| Échecs du chat | `chat_messages` | 03:00 et 15:00 |
| Jobs en échec | `system.lakeflow.jobs`, `system.lakeflow.job_run_timeline` (limité aux jobs du workspace de l'env) | 08:00 |
| Nouvelles erreurs | `app_errors` | 03:00 |
| Tours dégradés | `chat_turns` | 03:00 |

  Les alertes tournent sur un warehouse SQL par env (`alerts_warehouse_id`) et sont en pause ou actives selon `alerts_pause_status`.
- La notation des traces du chat (`D_2_Qualibot_Score_Chat_Traces`) : `chat_messages`, `chat_sessions`, `chat_feedbacks`, `chat_turns`, `chat_retrieved_chunks`, `app_errors`.

### Groupes des utilisateurs (table `users`)

Le job `D_2_Qualibot_User_Capabilities_Sync` lit, dans `system.access.audit`, le dernier événement d'ajout ou de retrait par (utilisateur, groupe suivi). Groupes suivis : pour le chat `Role-Project-LEAP-End-users-Qualibot-ChatBot`, `Role-LATECOERE-User-Internal`, `Role-Project-LEAP-CoreDev`, `Role-Project-LEAP-CoreAdmin` ; pour la comparaison `Role-Project-LEAP-End-users-Qualibot-DocCompare`, CoreDev, CoreAdmin. Il écrit `groups`, `can_chat`, `can_compare`, `groups_synced_at`. Le SP du job doit avoir SELECT sur `system.access.audit`.

## 6. Accès à l'app

- Le chat est ouvert à tout visiteur de l'app (la permission `CAN_USE` de l'app est le filtre). La comparaison est réservée aux groupes DocCompare, CoreDev et CoreAdmin.
- Les groupes viennent de la table `users` (job `D_2_Qualibot_User_Capabilities_Sync`, depuis le journal d'audit) et de SCIM `Me` avec le jeton du visiteur. Un retrait de groupe met jusqu'à 2 h environ à être pris en compte.
- `CAPS_BYPASS=true` ouvre tout le monde : autorisé en DEV seulement.

## 7. Évaluation de la recherche (MLflow)

- Job `Z_4_Qualibot_Retrieval_Eval` (`resources/retrieval_eval.yml`, notebooks dans `utils/evaluation/`), manuel, un job par env. Il écrit d'abord la table du jeu de questions (`<env>_proj.qualibot.eval_golden_retrieval`, la liste est dans le notebook `create_golden_retrieval_set.py`), puis joue chaque question contre la recherche réelle de l'app, une fois par configuration, et enregistre les scores dans MLflow (expérience `/Shared/qualibot/<env>_retrieval_eval`).
- Une question réussit quand un passage d'un des documents attendus (REF) figure parmi les K premiers passages envoyés au modèle. Indicateurs : rappel@K, tous les documents attendus@K, MRR.
- Paramètres du job : `preset` (`deploye` = config actuellement déployée, `tailles`, `complet`, `perso`), `indexes`, `reference` (config de référence pour les comparaisons), `ks`, `max_parallel` (16), `rerun_existing`, `mlflow_experiment`.
- Un résultat est mis en cache sous le nom de la configuration + la signature du code : après un changement de l'index (nouveau découpage par exemple), renommer les configurations (suffixe) et mettre `rerun_existing=false`.

### Configuration actuelle du chat en PROD

Valeurs de `app.yaml` (aucune surcharge pour PROD dans `utils/deploy/target_env.json`) :
- Première question d'une conversation (français ou anglais) : traduite dans l'autre langue (`CHAT_TRANSLATE_ONLY` activé dans les trois envs), puis deux requêtes de **32 passages** chacune (`CHAT_PAIR_RERANK_TOP_K=32`).
- Questions suivantes et autres langues : réécriture bilingue, **32 passages** par requête (`CHAT_RERANK_TOP_K=32`).
- **Pas de recherche brute** (`CHAT_RAW_TOP_K=0`, `CHAT_PAIR_RAW_TOP_K=0`).
- Passages fusionnés triés par meilleur score du reranker, puis plafonnés à **50** (`CHAT_MAX_SEARCH_PASSAGES`) ; les recherches par REF et par citation s'ajoutent.
- Recherche hybride Vector Search (vecteurs + mots-clés) avec reranker.

### Autres configurations testées (description)

| Variante | Principe |
|---|---|
| Nombre de passages par requête | 12, 16, 24, 32, 48 |
| Recherche brute | une requête de plus sans reranker, avec une part des places dans le résultat final ; activée ou non |
| Fusion des listes | meilleur rang (actuelle), RRF (somme des inverses des rangs), fusion à plat |
| Recherche scindée | une liste vecteurs et une liste mots-clés (BM25) fusionnées avec un poids sur les mots-clés |
| Normalisation des codes | codes réécrits en une orthographe unique dans les passages et les requêtes (`SF-1027` et `SF1027`), contre sans normalisation (index `chunks_baseline_index`) |
| Questions du chat | réécriture bilingue contre traduction seule, avec ou sans la question telle que posée |

## 8. Déploiement

Le code est déployé depuis le dépôt Bitbucket avec des **pipelines manuels** (fichier `bitbucket-pipelines.yml`) : rien ne part sur un simple push. Lancement : Bitbucket → Pipelines → **Run pipeline** → choisir la branche → **Custom** → choisir le pipeline dans la liste.

Chaque environnement Bitbucket (`dev`, `uat`, `production`) porte un jeton `DATABRICKS_TOKEN` du SP de l'env ; le script `utils/deploy/bitbucket_deploy.sh` écrit le profil et lance `databricks bundle deploy`. L'interface (`client/out`) est construite à chaque déploiement, jamais versionnée.

### Les deux pipelines à connaître, pour chaque env

| Pipeline | Ce qu'il fait | Quand l'utiliser |
|---|---|---|
| `deploy-<env>` | Valide et déploie le bundle (jobs, notebooks, ressources, permissions), **puis redéploie et redémarre l'app** | Changement du code de l'app (`server/`, `client/`), de `app.yaml` ou de `target_env.json` |
| `deploy-<env>-jobs` | Valide et déploie le bundle **sans toucher à l'app** : elle continue de tourner avec son code actuel | Changement des notebooks de parsing, des jobs, des plannings, des variables du bundle |

Avec `<env>` = `dev`, `uat` ou `prod` : `deploy-dev`, `deploy-dev-jobs`, `deploy-uat`, `deploy-uat-jobs`, `deploy-prod`, `deploy-prod-jobs`.

Règles : DEV sans test préalable ; UAT et PROD lancent d'abord la suite de tests (un test en échec arrête le déploiement) ; PROD refuse toute branche autre que `main`. Ordre : DEV, puis UAT, puis PROD.

### Autres pipelines

| Pipeline | Rôle |
|---|---|
| `tests` | Lance seulement la suite de tests (Python et frontend) |
| `plan-dev`, `plan-uat` | Lecture seule : affiche ce que le bundle changerait, sans rien déployer |
| `prod-state-check` | Lecture seule : contrôle l'état du bundle et des objets PROD |
| `prod-state-fix` | Une seule fois : détache l'état du bundle de l'ancien schéma `prod_landingzone.qualibot` et rattache les objets de `prod_proj.qualibot` (script `utils/deploy/prod_state_fix.sh`) |
| `migrate-dev-state`, `migrate-uat-state` | Une seule fois : déplace l'état du bundle vers le nouveau dossier `/Workspace/.bundle/qualibot/` |
| `uat-state-fix`, `bind-uat-lakebase`, `bind-uat-staging`, `unbind-dev-test-volume` | Réparations d'état du bundle UAT et DEV (détacher ou rattacher un objet existant) |
| `sync-prod-data` | Copie le volume des modèles Docling d'UAT vers PROD en deux étapes |

### Déploiement depuis le poste (alternative)

```powershell
# à la racine du dépôt, profil déjà connecté (databricks auth login --profile DEV)
.\utils\deploy\deploy_qualibot.ps1 -AppEnv dev
```
Options : `-SkipBuild` (pas de reconstruction du frontend), `-SyncOnly` (envoie le code sans reconstruire ni redémarrer), `-Infra` (lance `databricks bundle deploy` : ressources, permissions, liaisons de volumes ; demande MANAGE sur le catalogue).

### Configuration de l'app par env

Ordre de priorité : `utils/deploy/target_env.json` (surcharges par env, rendu en `target_config.env` au déploiement), puis `app.yaml` (valeurs par défaut commentées), puis les valeurs du code. Les variables du bundle (planning, workers, index synchronisés, cluster policy) sont dans `databricks.yml`, une section par env.

### Lancer l'app en local

Voir le README (profil CLI, `.env.local`, `python -m uvicorn server.app:app`, `bun run dev`). Sous Windows, retirer `DATABRICKS_TOKEN` de l'environnement s'il existe : `Remove-Item Env:\DATABRICKS_TOKEN`.

## 9. Comparaison de documents (DocCompare)

Onglet `/compare` (`client/src/components/compare/`, API `server/routers/compare/`, logique `server/services/compare/`). Accès : groupes DocCompare, CoreDev, CoreAdmin (`require_compare`). Activation : `COMPARE_ENABLED`.

**Ce que fait l'utilisateur** : il dépose deux versions d'un document (PDF, images, Word, PowerPoint, Excel, XML ; 20 Mo maximum chacun) et obtient, séparément ou ensemble :
- un **résumé des changements** (Markdown, une ligne par changement, export PDF) ;
- un **tableau des changements** (type, criticité haute / moyenne / basse, numéros `C1`, `C2`…, export Excel) ;
- une **recherche d'impact** (§10) ;
- le **résumé d'un seul document**.

**Déroulement d'une analyse** (`POST /api/compare/analyze`, flux SSE) : le navigateur calcule le SHA-256 des deux fichiers et envoie les fichiers avec la méthode (`standard` = résumé, `structured` = tableau). Le serveur vérifie le cache (clé = hash des deux fichiers + version de l'app), attend un créneau (`COMPARE_MAX_CONCURRENT`), transforme les fichiers par un *processeur* selon leur type (`server/services/compare/processors/`, versions proposées dans `versions.yml`), calcule la différence, puis appelle le modèle. Une ligne d'audit est écrite dans la table Lakebase `llm_requests` (statut `ok`, `error` ou `aborted`). Les très gros écarts (plus de 60 000 caractères) sont découpés aux limites de sections en morceaux de 45 000 caractères, analysés en parallèle puis fusionnés (`chunked_analysis.py`). Si le navigateur se déconnecte, l'appel au modèle s'arrête.

**Modèles** : analyse `databricks-claude-sonnet-5-5` (`COMPARE_ANALYSIS_ENDPOINT`, avec réflexion adaptative, effort `COMPARE_THINKING_EFFORT`) ; repli sur `COMPARE_LLM_FALLBACK_ENDPOINTS` (sinon GPT-5.6 Luna ou GPT-6 Luna). Résumé d'un document et images : `databricks-gpt-6-luna` (`COMPARE_SUMMARY_ENDPOINT`, `COMPARE_SUMMARY_IMAGE_ENDPOINT`). Un endpoint qui échoue est mis au repos, le suivant de la chaîne est essayé ; l'erreur n'est levée que si aucun ne répond. Prompts : fichiers de `server/prompts/compare/`.

**Réglages** (`app.yaml`, surcharges par env dans `utils/deploy/target_env.json`) : `COMPARE_MAX_TOKENS`, `COMPARE_ANALYSIS_TIMEOUT_S` (300), `COMPARE_METHOD_<TYPE>` (méthode par défaut par type de fichier), `COMPARE_SUMMARY_MAX_CHARS` (300 000), `COMPARE_VOLUME_PATH`.

**Sauvegarde et historique** : si `COMPARE_VOLUME_PATH` est défini (par env : `/Volumes/<env>_proj/qualibot/doc_compare`), un dossier de session reçoit les deux fichiers, le texte de l'analyse puis l'Excel ou le PDF. L'historique est dans la table Lakebase `compare_messages`, les votes dans `compare_feedbacks`, les résumés en cache dans `summary_cache`, les analyses en cache dans `analysis_cache`. Le navigateur garde aussi les documents et résultats en local storage (rechargement de page) ; un échec de sauvegarde n'empêche jamais l'affichage.

**Aperçus** : PDF et images lus localement ; DOCX converti en PDF fidèle par LibreOffice (`soffice.py`, archive du volume `doc_compare`, variable `SOFFICE_ARCHIVE_VOLUME_PATH`) ; `.doc` par antiword (installé au démarrage par `start.sh`) ; autres formats convertis en HTML.

**Limites connues** : les tableaux situés dans les en-têtes et pieds de page des DOCX ne sont pas comparés (ils contiennent surtout des tableaux de révision). Les images du tableau sont associées aux lignes par ordre d'apparition, pas par numéro de page.

## 10. Recherche d'impact

Sert à répondre à : « quels autres documents de la base sont touchés par ces changements ? ». Route `POST /api/compare/impact` (flux NDJSON), code `server/routers/compare/impact.py`, `server/services/compare/impact_queries.py`, client `client/src/components/compare/impact/`.

**Déroulement** :
1. Les changements viennent du tableau (ou, à défaut, du résumé), numérotés `C1`, `C2`…
2. `impact_queries.py` construit une requête de recherche par changement (jusqu'à `COMPARE_IMPACT_MAX_QUERIES` = 30 ; les changements de plus faible criticité partagent la dernière requête). Une requête est plafonnée à 2000 caractères. Les changements qui n'entrent dans aucune requête sont listés (`changes_not_searched`) et signalés à l'utilisateur.
3. Chaque requête interroge l'index Vector Search `COMPARE_IMPACT_INDEX` (`<env>_proj.qualibot.chunks_index`, le même que le chat) et renvoie `COMPARE_IMPACT_PER_QUERY_RESULTS` = 40 passages.
4. Les documents candidats sont classés ; les `COMPARE_IMPACT_MAX_CANDIDATES` = 15 meilleurs partent chez le juge. Un document comparé peut s'exclure lui-même des résultats.
5. Pour chaque candidat, un appel du modèle juge (`COMPARE_IMPACT_ENDPOINT` = `databricks-gpt-6-luna`, prompt `server/prompts/compare/impact_judge.md`) rend un verdict `impacted`, `check` (à vérifier), `not_impacted` ou `error`, la raison, et chaque passage en conflit avec une citation mot pour mot. Le modèle `gemini-3-1-flash-lite` a été écarté pour le juge : trop de faux positifs.
6. Le flux envoie `plan` (changements, candidats, requêtes), un événement `document` par document jugé au fur et à mesure, puis `done` (consommation, durée).

**Cache et audit** : le résultat est mis en cache (table `impact_cache`, clé = hash des fichiers, liste des changements, index, modèle) seulement si aucun document n'a échoué ; `force_refresh` l'ignore. Lignes d'audit : `impact_requests` (la recherche) et `impact_document_results` (chaque document jugé). Votes : `impact_feedbacks` (un par verdict de document, plus un avis général).

**Interface** : résultats « par document » ou « par changement », filtre par verdict, clic sur `C12` pour aller à la ligne du tableau, export Excel de la liste d'actions (`POST /api/compare/impact/export-excel`), résultat rattaché à l'entrée d'historique.

**Dépendances côté données** : la qualité dépend de `chunks_index` (donc du parsing, §3). Si l'index ne répond pas, la recherche ne se lance pas ; l'interface ne propose la recherche que si le nom d'index est valide.

## 11. À traiter / questions ouvertes

- Épingler les versions des bibliothèques installées sur le cluster de parsing.
- Corriger `2_manifest` pour qu'un run avec `parse_filter` ne supprime pas les chunks des anciennes révisions sans parser les nouvelles.
- Option `full` en MERGE plutôt qu'en écrasement (les chunks ne disparaîtraient plus pendant le run).
