# QualiBOT : passation

Complète le README (qui décrit le code). Ici : ce qu'il faut savoir pour **opérer** l'app depuis un poste **Windows** (PowerShell, navigateur Bitbucket et Databricks). Les noms de fichiers sont ceux du dépôt.


**Sommaire** : [1 Architecture](#1-architecture) · [2 Jobs](#2-les-jobs) · [3 Parsing](#3-pipeline-de-parsing) · [4 Index](#4-index-et-volumes) · [5 Lakebase](#5-lakebase) · [6 Accès](#6-accès-à-lapp) · [7 Éval retrieval](#7-évaluation-de-la-recherche-mlflow) · [8 Déploiement](#8-déploiement) · [9 DocCompare](#9-comparaison-de-documents-doccompare) · [10 Impact](#10-recherche-dimpact) · [11 Chatbot](#11-chatbot) · [12 Liens PROD](#12-liens-prod) · [13 Ouvert](#13-à-traiter--questions-ouvertes)

## 1. Architecture

- **App** : FastAPI ([`server/`](server/)) + React ([`client/`](client/)), Databricks App `qualibot`. Trois fonctions : comparaison de documents, recherche d'impact, chat.
- **Données** : une chaîne de parsing quotidienne produit des tables Delta et un index Vector Search `chunks_index`, lu par le chat et la recherche d'impact ([`utils/parsing_pipeline/README.md`](utils/parsing_pipeline/README.md)).
- **Historique** (conversations, feedbacks, erreurs, caches) : Lakebase (Postgres), projet `qualibot`, base `doccompare`. Colonnes : [`server/services/infra/lakebase/schema.md`](server/services/infra/lakebase/schema.md).
- **Source des documents** : Intraqual, catalogue `prod_landingzone.intraqual` (lu par les trois envs, jamais modifié).
- **Déploiement** : bundle Databricks `qualibot` ([`databricks.yml`](databricks.yml), `resources/*.yml`) lancé par des pipelines Bitbucket ([`bitbucket-pipelines.yml`](bitbucket-pipelines.yml)), voir §8.

| Env | Workspace | Tables | SP qui exécute les jobs |
|---|---|---|---|
| DEV | `dbc-c623749d-731b` | `dev_proj.qualibot` | `fde6ff28-739f-4a41-b61e-604a298c8478` (job-runner-sa-dev) |
| UAT | `dbc-3a17bfce-9e88` | `uat_proj.qualibot` | `3e5cd4e5-f765-4760-b974-ee7715258b39` (job-runner-sa-uat) |
| PROD | `dbc-77b87d5a-6bcb` | `prod_proj.qualibot` | `b82d3b5a-b633-4738-8dde-68bb2142cb02` (job-runner-sa-prod) |

- Mêmes noms de tables, volumes et index dans chaque env. Volumes : `doc_compare` (outils LibreOffice), `images` (images extraites), `staging` (échanges et exports), `docling_models`.
- Profils CLI Windows : `DEV`, `UAT`, `PROD` (fichier `%USERPROFILE%\.databrickscfg`).
- Groupes : CoreAdmin et CoreDev (CAN_MANAGE) ; End-users DocCompare et ChatBot (CAN_USE).
- Alertes SQL : [`resources/alerts.yml`](resources/alerts.yml).
- Ordre de travail : on change et on teste en DEV, puis UAT, puis PROD. Les copies entre envs passent par un volume `staging` (export dans un env, import dans l'autre), jamais par une copie directe de table.

## 2. Les jobs

Définis dans [`resources/`](resources/). Mêmes noms dans les trois envs. Fuseau : Europe/Brussels.

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

Les plannings se changent dans [`databricks.yml`](databricks.yml) (variables `parsing_schedule_pause`, `lakebase_export_schedule_pause`, `alerts_pause_status`) puis `deploy-<env>-jobs`.

## 3. Pipeline de parsing

Détail : [`utils/parsing_pipeline/README.md`](utils/parsing_pipeline/README.md).

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
- **Périmètre** : `parse_manifest` ne contient que les documents Intraqual courants (dernière révision, état validé, non masqués) qui sont dans l'une des catégories racines retenues (processus One QMS, AS programmes, AS instructions de travail, AS processus, directives groupe, training, VA, organization) ou dans les deux sous-catégories « To be classified ». La liste est dans [`utils/parsing_pipeline/config.py`](utils/parsing_pipeline/config.py) (`SCOPE_ROOT_IDCATS`, `SCOPE_SUB_IDCATS`). Garde-fou : si le nombre de documents du périmètre baisse de plus de 30 % par rapport au run précédent (ce qui signale presque toujours un problème de lecture des sources, pas un vrai retrait), `2_manifest` échoue sans rien supprimer ; pour une réduction voulue, mettre `PARSING_MAX_SCOPE_DROP_PCT` à 0 le temps d'un run.
- **Parseurs** : PDF, DOCX, PPTX et HTML passent par Docling. `xml_fallback` : lecteur XML de secours des DOCX/PPTX quand Docling échoue. Un PDF scanné (moins de 150 caractères de texte) passe en OCR par le modèle de vision (statut `pending_llm_ocr` en attendant).
- **Catégorie manquante** : si une révision n'a pas de catégorie, on regarde si une révision précédente du même document en a une ; si oui elle est reprise, sinon le document est ignoré.
- **Tailles de passages** ([`utils/parsing_pipeline/config.py`](utils/parsing_pipeline/config.py)) : `MIN_CHUNK_TOKENS = 300`, `TARGET_CHUNK_TOKENS = 450`, `MAX_CHUNK_TOKENS = 550`, `MAX_CHUNK_CHARS = 1950`. Un passage est coupé aux titres de niveau 1 à 3 ; il vise 450 tokens, ne descend pas sous 300 (un morceau trop petit est fusionné avec le suivant), ne dépasse pas 550 tokens ni 1950 caractères (le plafond en caractères protège les lignes de points et de tirets, qui coûtent peu de tokens). Deux passages voisins se recouvrent de 12 %. Le reranker de Vector Search ne lit que les 2000 premiers caractères d'un passage, d'où le plafond sous 2000. Les mêmes valeurs sont en repli dans [`utils/parsing_pipeline/worker.py`](utils/parsing_pipeline/worker.py) : les garder identiques. Ces valeurs s'appliquent aux nouveaux documents et à tout `rechunk` ou `full` ; les passages déjà en base gardent leur ancienne taille tant qu'aucun `rechunk` n'est lancé (non lancé pour l'instant). Pour les changer : modifier ces constantes (ou les variables `PARSING_MIN_CHUNK_TOKENS`, `PARSING_TARGET_CHUNK_TOKENS`, `PARSING_MAX_CHUNK_TOKENS`, `PARSING_MAX_CHUNK_CHARS`), déployer avec `deploy-<env>-jobs`, puis lancer un run `rechunk`.
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

Projet `qualibot`, branche `production`, base `doccompare`, un par env (jamais copié d'un env à l'autre en fonctionnement normal). L'app crée et complète son schéma au démarrage et s'y connecte avec son propre SP (rôle `app-doc-compare-sp`, id en dur dans [`databricks.yml`](databricks.yml) : à changer seulement si l'app est recréée). Colonnes de chaque table : [`server/services/infra/lakebase/schema.md`](server/services/infra/lakebase/schema.md).

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

### Fonctionnement technique

Descriptions détaillées : [`server/services/infra/lakebase/README.md`](server/services/infra/lakebase/README.md) (module), [`schema.md`](server/services/infra/lakebase/schema.md) (colonnes), [`utils/lakebase_sync/README.md`](utils/lakebase_sync/README.md) (export et groupes).
- **Connexion** ([`connection.py`](server/services/infra/lakebase/connection.py)) : pool de connexions Postgres ; le jeton d'accès du SP est renouvelé automatiquement (durée de vie d'environ 1 h). Hôte, projet, branche, base : [`utils/ops_config.py`](utils/ops_config.py).
- **Schéma** ([`schema.py`](server/services/infra/lakebase/schema.py), [`columns.py`](server/services/infra/lakebase/columns.py)) : créé et complété au démarrage de l'app, de façon idempotente (ajout de colonnes sans perte).
- **Écritures « au mieux »** : une erreur d'écriture dans les journaux (`chat_turns`, `app_errors`…) ne casse jamais la réponse à l'utilisateur ; elle est journalisée. Si le Lakebase est indisponible, les routes qui en dépendent répondent 503.
- **Autres modules** : [`users.py`](server/services/infra/lakebase/users.py) (utilisateurs et groupes), [`cache.py`](server/services/infra/lakebase/cache.py) (caches), [`audit.py`](server/services/infra/lakebase/audit.py) (journaux).

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

- **Codes de référence** (ex. `SF-1027`) : à chaque passage, `cited_codes` liste les codes qu'il cite et `chunk_preprocessed` les écrit sous une seule orthographe (`SF-1027 [SF1027]`), à l'identique côté requêtes de l'app (module [`utils/parsing_pipeline/code_normalization.py`](utils/parsing_pipeline/code_normalization.py)). `doc_references` est construite à partir de `cited_codes` : elle permet de répondre à « quels documents citent X ? » avec la liste complète.
- **Jointure** : `trace_id` (`chat-` + 32 caractères hexadécimaux) relie `chat_turns`, `chat_retrieved_chunks`, `app_errors` et `chat_messages` ; `chat_turns.mlflow_trace_id` relie au trace MLflow.

### Export du Lakebase vers Delta

Job `D_1_Qualibot_Lakebase_Export_To_Volume`, deux tâches : `export_lakebase_to_volume` (une table = un fichier JSON Lines dans `/Volumes/<env>_proj/qualibot/staging/lakebase_export`) puis `import_lakebase_tables_to_delta` (charge chaque fichier dans une table Delta du même nom dans `<env>_proj.qualibot`, saute une table vide). Serverless obligatoire (le Lakebase n'est pas joignable depuis un cluster classique). Les variables `lakebase_import_to_delta` (charger ou non) et `lakebase_tables_to_skip` (tables exclues de l'export) se règlent par env dans [`databricks.yml`](databricks.yml). Les fichiers contiennent des données utilisateur (questions, réponses, identifiants) : ils restent dans le volume.

### Qui lit les copies Delta

- Le dashboard Lakeview **Qualibot Usage Tracking** (suivi d'usage). Les tables exactes qu'il lit sont définies dans le dashboard lui-même, pas dans le dépôt : **à vérifier dans Databricks**. La note de limite du README indique que celui de DEV lit encore d'anciennes tables.
- Les alertes SQL ([`resources/alerts.yml`](resources/alerts.yml), une série par env, mêmes noms) :

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

- Job `Z_4_Qualibot_Retrieval_Eval` ([`resources/retrieval_eval.yml`](resources/retrieval_eval.yml), notebooks dans [`utils/evaluation/`](utils/evaluation/)), manuel, un job par env. Il écrit d'abord la table du jeu de questions (`<env>_proj.qualibot.eval_golden_retrieval`, la liste est dans le notebook `create_golden_retrieval_set.py`), puis joue chaque question contre la recherche réelle de l'app, une fois par configuration, et enregistre les scores dans MLflow (expérience `/Shared/qualibot/<env>_retrieval_eval`).
- Une question réussit quand un passage d'un des documents attendus (REF) figure parmi les K premiers passages envoyés au modèle. Indicateurs : rappel@K, tous les documents attendus@K, MRR.
- Paramètres du job : `preset` (`deploye` = config actuellement déployée, `tailles`, `complet`, `perso`), `indexes`, `reference` (config de référence pour les comparaisons), `ks`, `max_parallel` (16), `rerun_existing`, `mlflow_experiment`.
- Un résultat est mis en cache sous le nom de la configuration + la signature du code : après un changement de l'index (nouveau découpage par exemple), renommer les configurations (suffixe) et mettre `rerun_existing=false`.

### Configuration actuelle du chat en PROD

Valeurs de [`app.yaml`](app.yaml) (aucune surcharge pour PROD dans [`utils/deploy/target_env.json`](utils/deploy/target_env.json)) :
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

Pipelines Bitbucket **manuels** ([`bitbucket-pipelines.yml`](bitbucket-pipelines.yml)) : Pipelines → **Run pipeline** → branche → **Custom** → nom du pipeline. Rien ne part sur un push. Chaque environnement Bitbucket (`dev`, `uat`, `production`) porte le jeton du SP de l'env ; [`utils/deploy/bitbucket_deploy.sh`](utils/deploy/bitbucket_deploy.sh) lance `databricks bundle deploy`. L'interface est reconstruite à chaque déploiement.

### Les deux pipelines à connaître, pour chaque env

| Pipeline | Ce qu'il fait | Quand l'utiliser |
|---|---|---|
| `deploy-<env>` | Valide et déploie le bundle (jobs, notebooks, ressources, permissions), **puis redéploie et redémarre l'app** | Changement du code de l'app ([`server/`](server/), [`client/`](client/)), de [`app.yaml`](app.yaml) ou de `target_env.json` |
| `deploy-<env>-jobs` | Valide et déploie le bundle **sans toucher à l'app** : elle continue de tourner avec son code actuel | Changement des notebooks de parsing, des jobs, des plannings, des variables du bundle |

Avec `<env>` = `dev`, `uat` ou `prod` : `deploy-dev`, `deploy-dev-jobs`, `deploy-uat`, `deploy-uat-jobs`, `deploy-prod`, `deploy-prod-jobs`.

Règles : DEV sans test préalable ; UAT et PROD lancent d'abord la suite de tests (un test en échec arrête le déploiement) ; PROD refuse toute branche autre que `main`. Ordre : DEV, puis UAT, puis PROD.

### Autres pipelines

| Pipeline | Rôle |
|---|---|
| `tests` | Lance seulement la suite de tests (Python et frontend) |
| `plan-dev`, `plan-uat` | Lecture seule : affiche ce que le bundle changerait, sans rien déployer |
| `prod-state-check` | Lecture seule : contrôle l'état du bundle et des objets PROD |
| `prod-state-fix` | Une seule fois : détache l'état du bundle de l'ancien schéma `prod_landingzone.qualibot` et rattache les objets de `prod_proj.qualibot` (script [`utils/deploy/prod_state_fix.sh`](utils/deploy/prod_state_fix.sh)) |
| `migrate-dev-state`, `migrate-uat-state` | Une seule fois : déplace l'état du bundle vers le nouveau dossier `/Workspace/.bundle/qualibot/` |
| `uat-state-fix`, `bind-uat-lakebase`, `bind-uat-staging`, `unbind-dev-test-volume` | Réparations d'état du bundle UAT et DEV (détacher ou rattacher un objet existant) |
| `sync-prod-data` | Copie le volume des modèles Docling d'UAT vers PROD en deux étapes |

Configuration de l'app par env : [`utils/deploy/target_env.json`](utils/deploy/target_env.json) (surcharges) > [`app.yaml`](app.yaml) (défauts) > code. Variables du bundle (plannings, workers, index) : [`databricks.yml`](databricks.yml).

## 9. Comparaison de documents (DocCompare)

Documentation de référence : [client/src/components/compare/README.md](client/src/components/compare/README.md) (interface, flux, historique), [server/routers/compare/README.md](server/routers/compare/README.md) (routes), [server/services/compare/README.md](server/services/compare/README.md) (services et LLM), [server/services/compare/processors/README.md](server/services/compare/processors/README.md) (extraction par type de fichier), [server/services/compare/processors/_diff_engines/README.md](server/services/compare/processors/_diff_engines/README.md) (moteurs de différence), [server/services/compare/processors/docx/README.md](server/services/compare/processors/docx/README.md) (DOCX).

Accès : groupes DocCompare, CoreDev, CoreAdmin (`require_compare`). Activation : `COMPARE_ENABLED`.

**Ce que fait l'utilisateur** : il dépose deux versions d'un document (PDF, images, Word, PowerPoint, Excel, XML ; 20 Mo maximum chacun) et obtient, séparément ou ensemble : un **résumé des changements** (Markdown, une ligne par changement, export PDF), un **tableau des changements** (type, criticité haute / moyenne / basse, numéros `C1`, `C2`…, export Excel), une **recherche d'impact** (§10), ou le **résumé d'un seul document**.

### Comment une analyse est réalisée

Route `POST /api/compare/analyze` ([`server/routers/compare/analyze.py`](server/routers/compare/analyze.py)), réponse en flux SSE.

1. **Hash et cache.** Le navigateur calcule le SHA-256 des deux fichiers. Le serveur cherche une analyse identique en cache (table `analysis_cache`, clé = hash des deux fichiers + version de l'app + réglages qui façonnent le rapport : méthode, modèle, prompt) ; s'il y en a une, il la rejoue. `force_refresh` l'ignore.
2. **Créneau.** Au plus `COMPARE_MAX_CONCURRENT` analyses en même temps ; les autres attendent.
3. **Extraction (processeur).** Un processeur par type de fichier (`processors/factory.py`, `EXTENSION_MAP`) lit les deux fichiers et en sort le texte et les images :
   - PDF : PyMuPDF, texte par bloc avec balises de page, fusion des paragraphes coupés par un saut de page, lignes de tableaux, images dédoublonnées par hash ;
   - DOCX / DOC : lignes dans l'ordre du document balisées `[Page N, Para M]` (`.doc` lu par antiword puis olefile) ;
   - PPTX / PPT : texte et images par diapositive ;
   - XML : texte des éléments ; Excel : différence cellule par cellule, lignes appariées sur la première colonne ; images : les deux images envoyées à un modèle de vision après redimensionnement.
4. **Différence (moteurs partagés, `_diff_engines/`).** Les paragraphes similaires des deux versions sont alignés ; le résultat montre les changements dans le texte (`~~supprimé~~`, `**ajouté**`). Le moteur fusionne les en-têtes et pieds de page répétés, le même changement répété sur chaque page, détecte le texte déplacé plutôt que supprimé, et distingue un vrai changement d'un changement de style (un verbe modal « doit » devient « peut » est substantiel, deux verbes du même groupe d'obligation ne le sont pas). Les images des deux versions sont appariées par empreinte (modifiée, retournée, supprimée, ajoutée). La différence est coupée à `COMPARE_MAX_DIFF_CHARS` ; l'utilisateur est prévenu quand la fin du rapport manque.
5. **Méthode et prompt.** `standard` (résumé Markdown), `structured` (tableau JSON) ou `comparative` (cachée dans l'interface). La liste des méthodes proposées par type de fichier est dans `processors/versions.yml`, la méthode par défaut dans `COMPARE_METHOD_<TYPE>`. Les prompts sont dans [`server/prompts/compare/`](server/prompts/compare/) (`analysis_standard.md`, `analysis_structured.md`, `analysis_default.md`).
6. **Appel du modèle.** `databricks-claude-sonnet-5-5` (`COMPARE_ANALYSIS_ENDPOINT`, réflexion adaptative réglée par `COMPARE_THINKING_EFFORT`), avec repli sur `COMPARE_LLM_FALLBACK_ENDPOINTS` (sinon GPT-5.6 Luna ou GPT-6 Luna). Un endpoint en échec est mis au repos et le suivant est essayé ; l'erreur n'est levée que si aucun ne répond. Au-delà de `COMPARE_CHUNK_THRESHOLD_CHARS` (60 000 caractères), la différence est coupée aux limites de sections en morceaux de `COMPARE_CHUNK_SIZE_CHARS` (45 000), analysés en parallèle puis fusionnés dans un seul flux (`chunked_analysis.py`).
7. **Fin.** Une ligne d'audit est écrite dans `llm_requests` (statut `ok`, `error` ou `aborted`, durées, tokens, coût). Si le navigateur se déconnecte, l'appel au modèle s'arrête et la ligne est `aborted`.

### Comment le diff est calculé

Code : [`processors/text_diff.py`](server/services/compare/processors/text_diff.py) (gabarit commun aux processeurs de texte), [`_diff_engines/engines.py`](server/services/compare/processors/_diff_engines/engines.py) (moteur principal), [`collapse.py`](server/services/compare/processors/_diff_engines/collapse.py) et [`relocation.py`](server/services/compare/processors/_diff_engines/relocation.py) (nettoyage), [`text.py`](server/services/compare/processors/_diff_engines/text.py) (tests de changement substantiel), [`images.py`](server/services/compare/processors/_diff_engines/images.py) (images), [`prompts.py`](server/services/compare/processors/_diff_engines/prompts.py) (prompts système). Le modèle ne reçoit pas les deux documents entiers : il reçoit **une différence déjà calculée par du code**, qu'il met en forme et explique. C'est ce qui rend l'analyse fiable et peu coûteuse.

Moteur principal `paragraph_semantic_diff`, dans l'ordre :
1. **Découpage en blocs.** Chaque texte est découpé en blocs (un paragraphe, une ligne de tableau), chacun avec sa section, sa page et une forme de comparaison normalisée (minuscules, points de conduite et espaces réduits). Les blocs « (Suite) » / « (Continued) » coupés par un saut de page sont recollés.
2. **En-têtes et pieds répétés.** Un texte qui revient sur chaque page (nom de société, titre de norme, tampon `4/39`) est dédoublonné, sinon il apparaîtrait comme une nouvelle section par page.
3. **Appariement exact.** Les blocs identiques dans les deux versions sont écartés. Les blocs répétés sont appariés par **position** (pas dans l'ordre d'apparition), ce qui évite une suppression fantôme.
4. **Re-segmentation.** Un paragraphe simplement redécoupé autrement par l'extraction (deux blocs devenus un, ou l'inverse) est annulé.
5. **Appariement par similarité.** Les blocs restants de l'ancienne et de la nouvelle version sont comparés deux à deux (rapport de mots supérieur à 0,55, au moins 3 mots) ; les meilleurs couples deviennent des entrées **MODIFIED** avec marquage dans le texte (`~~supprimé~~`, `**ajouté**`), le reste devient **REMOVED** ou **ADDED**. Des bornes rapides (longueurs, mots communs) évitent la plupart des comparaisons coûteuses.
6. **Deuxième et troisième chance.** Une ligne de tableau très réécrite mais de même étiquette de ligne et de même section est appariée plutôt que listée supprimée puis ajoutée. Un titre qui change seulement de numéro (`3. INSPECTION` devient `4. INSPECTION`) est apparié ; une vraie valeur modifiée (`2.5 mm max` devient `3.5 mm max`) n'est jamais masquée.
7. **Ce qui n'est pas un vrai changement est filtré.** Un changement de verbe modal est substantiel seulement entre groupes d'obligation (obligatoire, permis, conseillé : « doit » vers « peut »), pas à l'intérieur d'un groupe. Un changement de pagination seule est ignoré. Un même changement répété sur chaque page, ou un restyle sur tout le document, est replié en une seule entrée annotée. Du texte déplacé (et non supprimé) est repéré par `relocation.py` et marqué comme déplacé ; les artefacts de découpage de l'extraction sont éliminés.
8. **Sortie.** Entrées groupées par section dans l'ordre du document. Le nombre de blocs filtrés est transmis. Le texte est coupé à `COMPARE_MAX_DIFF_CHARS` (l'utilisateur est prévenu).

La méthode `comparative` utilise un autre moteur, `section_canonical_diff` (différence unifiée groupée par titre). Les seuils sont des variables d'environnement (`COMPARE_PAIR_RATIO_THRESHOLD`, `COMPARE_ARTIFACT_MAX_WORDS`, `COMPARE_BOILERPLATE_MIN_PAGES`, `COMPARE_SYSTEMIC_SUB_MIN`, `COMPARE_WORD_LEVEL_CHECK`…, liste dans le [README des moteurs](server/services/compare/processors/_diff_engines/README.md)).

**Images** : chaque image est réduite à une empreinte (`dhash`, `ahash`, puis une empreinte 16 x 16 qui confirme l'identité d'un couple). Les couples sont classés MODIFIED, ORIENTATION CHANGED, REMOVED ou ADDED, et les paires sont envoyées au modèle avec le diff texte.

**Excel** : différence cellule par cellule, lignes appariées sur la première colonne ; une colonne de formules ou de dates qui change dans plus de la moitié des lignes (feuilles d'au moins 20 lignes) est considérée recalculée et seulement signalée ; une feuille présente d'un seul côté est signalée avec ses colonnes et ses 30 premières lignes. Si la première colonne n'est pas une clé unique, c'est une différence ligne à ligne. **Images (fichiers image)** : les deux images partent telles quelles à un modèle de vision.

### Ce qui est filtré (règles condensées)

Quatre couches successives écartent le bruit avant d'afficher une différence.

**A. À l'extraction** : PDF : numéros de page parasites supprimés, bandes d'en-tête (18 % du haut) et de pied (12 % du bas) ignorées, cellules de tableau dédoublonnées. DOCX : paragraphes de table des matières ignorés, lignes de tableau en double dédoublonnées, modifications suivies acceptées (on compare le texte final).

**B. Moteur de diff** :
- Blocs identiques écartés ; en-têtes et pieds de **15 mots au plus** répétés sur au moins 3 pages (fenêtre de 25 lignes) dédoublonnés ; blocs « (Suite) » recollés.
- Re-segmentation (au moins 8 mots, séries de 2 à 3 blocs) annulée.
- Paire modifiée **non substantielle** écartée : distance inférieure à 5 %, aucun changement au niveau du mot, mêmes nombres, mêmes références pièces, même profil de verbes modaux, mêmes références normatives.
- Restes courts identiques (12 mots au plus, dérive inférieure à 3,5 %) écartés ; cellules fusionnées (rapport d'au moins 3) ; changement de pagination seule ignoré.
- Texte **déplacé** (au moins 6 mots, couverture de 75 % ou plus, série de 8 mots) marqué RELOCATED et non supprimé/ajouté.
- Même changement répété sur au moins 3 pages replié en une entrée ; au moins 3 lignes de table des matières repliées ; substitution systématique (au moins 5 entrées, sans chiffre) repliée.
- Troncature du diff à 600 000 caractères (utilisateur prévenu).

**C. Prompt** : les prompts `analysis_standard.md` et `analysis_structured.md` ([`server/prompts/compare/`](server/prompts/compare/)) listent ce que le modèle ne doit pas rapporter (reformulations sans effet, mise en forme, numérotation, pagination).

**D. Affichage** : les lignes d'importance « Low » sont masquées par défaut ; les changements « Editorial » et « Structural » ne sont pas cherchés par la recherche d'impact.

Seuils réglables : variables `COMPARE_*` listées dans le [README des moteurs](server/services/compare/processors/_diff_engines/README.md).

### Résumé d'un seul document

`POST /api/compare/summarize` : texte envoyé à `COMPARE_SUMMARY_ENDPOINT` (`databricks-gpt-6-luna`, texte coupé à `COMPARE_SUMMARY_MAX_CHARS` = 300 000 caractères), image envoyée à `COMPARE_SUMMARY_IMAGE_ENDPOINT`. Mis en cache par hash du fichier (`summary_cache`). Un document sans texte donne un résumé « pas de contenu », pas une erreur.

### Sauvegarde, historique, aperçus

- Si `COMPARE_VOLUME_PATH` est défini (par env : `/Volumes/<env>_proj/qualibot/doc_compare`), un dossier de session reçoit les deux fichiers, le texte de l'analyse puis l'Excel ou le PDF. Les routes `save` / `load` (`volume.py`) refusent tout chemin hors de ce volume.
- Historique : table Lakebase `compare_messages` ; votes : `compare_feedbacks`. Le navigateur garde aussi documents et résultats en local storage ; un échec de sauvegarde n'empêche jamais l'affichage.
- Aperçus : PDF et images lus dans le navigateur ; DOCX converti en PDF fidèle par LibreOffice (`soffice.py`, archive dans le volume `doc_compare`, variable `SOFFICE_ARCHIVE_VOLUME_PATH`) ; `.doc` par antiword (installé au démarrage par [`start.sh`](start.sh)) ; autres formats convertis en HTML.

### Limites connues

Les tableaux situés dans les en-têtes et pieds de page des DOCX ne sont pas comparés (ils contiennent surtout des tableaux de révision). Dans le tableau des changements, les images sont associées aux lignes par ordre d'apparition, pas par numéro de page (le numéro rendu par le modèle n'est pas fiable).

## 10. Recherche d'impact

Question à laquelle elle répond : « quels autres documents de la base sont touchés par les changements de cette comparaison ? ». Documentation : [client/src/components/compare/README.md](client/src/components/compare/README.md) (section Impact search), [server/routers/compare/README.md](server/routers/compare/README.md). Code : [server/routers/compare/impact.py](server/routers/compare/impact.py) (route et flux), [server/services/compare/impact_queries.py](server/services/compare/impact_queries.py) (changements et requêtes), [server/services/infra/vector_search.py](server/services/infra/vector_search.py) (recherche, agrégation, juge), prompt [server/prompts/compare/impact_judge.md](server/prompts/compare/impact_judge.md). Interface : [`client/src/components/compare/impact/`](client/src/components/compare/impact/).

### Comment elle est réalisée

Route `POST /api/compare/impact`, réponse en flux NDJSON (une ligne JSON par événement).

1. **Liste numérotée des changements** (`extract_changes`). Le texte reçu est celui du tableau (JSON : section, type, criticité, avant, après, raison), sinon du résumé (Markdown : une section `##` puis une ligne par changement), sinon du texte libre. Chaque changement reçoit un identifiant stable `C1`, `C2`… dans l'ordre du document, que le juge et l'interface réutilisent. Les changements « éditorial / structurel » (renumérotation, table des matières) sont gardés dans la liste pour que les numéros correspondent au tableau, mais ne sont pas recherchés ; s'il n'y a que ça, ils sont recherchés quand même. « Aucun changement significatif » (dans plusieurs langues) donne un résultat vide sans appel de modèle.
2. **Une requête par changement** (`changes_to_queries`). Chaque changement devient une requête courte (au plus 2000 caractères : un texte long dilue le vecteur et ne trouve rien de précis). Jusqu'à `COMPARE_IMPACT_MAX_QUERIES` = 30 requêtes ; au-delà, les changements de plus faible criticité sont regroupés par section, et ceux qui ne tiennent encore dans aucune requête sont listés dans `changes_not_searched` (l'utilisateur est prévenu que des impacts peuvent manquer).
3. **Recherche** (`_fetch_chunks_multi`). Chaque requête interroge l'index Vector Search `COMPARE_IMPACT_INDEX` (`<env>_proj.qualibot.chunks_index`, le même index que le chat) en recherche hybride et rend `COMPARE_IMPACT_PER_QUERY_RESULTS` = 40 passages. Une requête en échec est retentée ; si toutes échouent, la recherche s'arrête avec une erreur, si seulement certaines échouent l'avertissement `VectorSearchPartial` est enregistré dans `app_errors` et le résultat n'est pas mis en cache. Le jeton utilisé est celui du SP de l'app ; en cas de refus 403 il est réessayé avec le jeton du visiteur.
4. **Agrégation par document** (`_aggregate_docs`). Les passages sont regroupés par document, un même passage trouvé par plusieurs changements n'est gardé qu'une fois avec l'union des changements qui l'ont fait remonter. Les documents sont classés par nombre de changements couverts puis meilleur score. Les variantes de langue d'un même document (REF qui ne diffère que par le suffixe de langue ou de site) sont fusionnées. Le document comparé lui-même (et ses variantes) est exclu. Les `COMPARE_IMPACT_MAX_CANDIDATES` = 15 meilleurs partent chez le juge ; les suivants sont listés comme non jugés.
5. **Juge, un appel par document candidat** (`_judge`, parallèle, avec reprises). Le modèle `COMPARE_IMPACT_ENDPOINT` (`databricks-gpt-6-luna`, repli `databricks-gpt-5-6-luna`) reçoit la liste des changements (plafonnée à `COMPARE_IMPACT_MAX_QUERY_CHARS` = 20 000 caractères) et les passages du candidat, et répond en JSON : `impacted`, `confidence` (haute / moyenne / basse), `reason`, et pour chaque passage en conflit le numéro du passage, les changements concernés, la section, une **citation mot pour mot** et une explication. La citation est retrouvée dans le texte du passage pour être surlignée dans l'interface. Le modèle `gemini-3-1-flash-lite` a été écarté pour ce rôle : trop de faux positifs.
6. **Verdict** (`_status`) : `impacted`, `check` (à vérifier), `not_impacted` ou `error`. Un verdict « impacté » dont aucune citation n'est retrouvée dans le texte, ou une confiance basse, devient `check` : une affirmation invérifiable n'est jamais présentée comme certaine. Un appel du juge qui échoue donne ce document en `error` sans arrêter les autres.
7. **Flux envoyé** : événement `plan` (changements, nombre de requêtes réussies et en échec, passages trouvés, candidats, documents exclus ou non jugés, changements non recherchés), un événement `document` par document dès qu'il est jugé, puis `done` (tokens, coût, durée, id de la requête). Les documents sont ensuite triés (impactés d'abord).

### Cache, audit, retours

- **Cache** (table `impact_cache`) : clé = hash des deux fichiers + version de l'app + liste des changements + index + modèle. Un résultat n'est mis en cache que s'il est complet : aucune requête en échec, aucun juge en erreur, aucun jugement venant d'un modèle de repli. `force_refresh` ignore le cache.
- **Audit** : `impact_requests` (la recherche : durées, requêtes, passages, tokens, coût, statut `ok` / `error` / `aborted`) et `impact_document_results` (chaque document jugé). Les dégradations vont dans `app_errors` (sévérité `warning`).
- **Votes** : `POST /api/compare/impact/feedback` → `impact_feedbacks` (un vote par verdict de document, plus un avis général sur la recherche).
- **Interface** : résultats « par document » ou « par changement », filtre par verdict, un clic sur `C12` ramène à la ligne du tableau, export Excel de la liste d'actions (`POST /api/compare/impact/export-excel`), résultat rattaché à l'entrée d'historique (`PUT /api/history/{id}/impact`).

### Ce dont elle dépend

La qualité vient de l'index `chunks_index` (donc du parsing, §3 : périmètre, tailles de passages) et du modèle juge. L'interface ne propose la recherche que si le nom d'index est valide (`/api/config/app`).

## 11. Chatbot

Détail dans les README : [moteur](server/services/chat/engine/README.md) (le plus utile), [services](server/services/chat/README.md), [route WebSocket](server/routers/chat/README.md), [interface](client/src/components/chat/README.md), [prompts](server/prompts/README.md), [infra / Vector Search / Lakebase](server/services/infra/README.md), [schéma des tables](server/services/infra/lakebase/schema.md). Fichiers clés : [`engine/queries.py`](server/services/chat/engine/queries.py) (traduction et réécriture), [`engine/search.py`](server/services/chat/engine/search.py) (recherches), [`engine/retrieval.py`](server/services/chat/engine/retrieval.py) (tout avant la réponse), [`engine/answer.py`](server/services/chat/engine/answer.py) (prompt et flux de réponse), [`engine/references.py`](server/services/chat/engine/references.py) (« quels documents citent X ? »), [`engine/citations.py`](server/services/chat/engine/citations.py), [`llm.py`](server/services/chat/llm.py) (chaîne de modèles), [`routers/chat/ws.py`](server/routers/chat/ws.py).

### Fonctionnement en bref

Le navigateur ouvre un WebSocket (`/api/chat/ws`) et envoie la conversation, l'identifiant de session et la division (ALL, AS, IS). Pour chaque question :
1. **Accès** : refus si l'utilisateur n'a pas le droit chat (§6) ou si le chat est désactivé.
2. **Langue et requêtes** : la première question d'une conversation (français ou anglais) est seulement **traduite** dans l'autre langue (`CHAT_TRANSLATE_ONLY`) ; les questions suivantes et les autres langues sont **réécrites** par un modèle en une requête française et une anglaise (une question de suivi seule n'a pas de sens). Les noms de société et types de documents que la conversation n'emploie pas sont retirés de la réécriture.
3. **Recherche** : deux requêtes hybrides (vecteurs + mots-clés) sur `chunks_index`, filtrées sur la division, chacune reclassée (reranker) sur 32 passages. Les listes sont fusionnées par meilleur score de reranker et plafonnées à 50 passages. Les codes de référence (`SF-1027`) sont écrits sous leur forme canonique dans les requêtes comme dans l'index.
4. **Références nommées et citations** : les REF cités dans la question ou la conversation sont cherchés directement (via le catalogue). Pour « quels documents citent X ? », la liste vient de la table `doc_references`, pas d'une recherche.
5. **Un document, une langue** : si un document existe en plusieurs langues, on garde la variante qui cite le code demandé, sinon la plus récente, sinon celle de la langue de la question.
6. **Prompt et réponse** : les documents sont numérotés `[1]`, `[2]`… avec leur révision et leur date, les passages viennent avant la question, la date du jour est fournie. Le modèle (`databricks-gpt-6-luna`, repli `databricks-gpt-5-6-luna`, chaîne dans `llm.py`) répond en flux ; les marqueurs `[n]` deviennent des citations cliquables et la réponse se termine par un tableau des documents utilisés. Une réponse coupée est reprise sur l'endpoint suivant (deux fois au plus).
7. **Journal** : chaque tour est écrit dans Lakebase (`chat_turns`, `chat_retrieved_chunks`, `chat_messages`, `app_errors`, voir §5) et une trace MLflow est envoyée. L'utilisateur ne voit jamais d'erreur technique : un seul message générique, la cause est dans `app_errors`.

Les prompts sont des fichiers dans [`server/prompts/chat/`](server/prompts/chat/) ; après modification, relancer le job `Z_3_Qualibot_Register_Chat_Prompts`. La configuration actuelle et les variantes testées sont au §7.

## 12. Liens PROD

Espace de travail PROD : https://dbc-77b87d5a-6bcb.cloud.databricks.com

| Quoi | Lien |
|---|---|
| Liste des jobs | https://dbc-77b87d5a-6bcb.cloud.databricks.com/jobs |
| App `qualibot` | https://dbc-77b87d5a-6bcb.cloud.databricks.com/apps/qualibot |
| Alertes SQL | https://dbc-77b87d5a-6bcb.cloud.databricks.com/sql/alerts |
| Catalogue / schéma `prod_proj.qualibot` | https://dbc-77b87d5a-6bcb.cloud.databricks.com/explore/data/prod_proj/qualibot |
| Volume `staging` | https://dbc-77b87d5a-6bcb.cloud.databricks.com/explore/data/volumes/prod_proj/qualibot/staging |
| Index `chunks_index` | https://dbc-77b87d5a-6bcb.cloud.databricks.com/explore/data/prod_proj/qualibot/chunks_index |

**Jobs PROD**

| Job | Lien |
|---|---|
| `D_1_Qualibot_Parsing_Pipeline` | https://dbc-77b87d5a-6bcb.cloud.databricks.com/jobs/319182500454150 |
| `D_1_Qualibot_Lakebase_Export_To_Volume` | https://dbc-77b87d5a-6bcb.cloud.databricks.com/jobs/172239397407691 |
| `D_2_Qualibot_User_Capabilities_Sync` | https://dbc-77b87d5a-6bcb.cloud.databricks.com/jobs/898973603694951 |
| `D_2_Qualibot_Score_Chat_Traces` | https://dbc-77b87d5a-6bcb.cloud.databricks.com/jobs/661647982791628 |
| `Z_2_Qualibot_Create_Chat_Experiment` | https://dbc-77b87d5a-6bcb.cloud.databricks.com/jobs/3123577015114 |
| `Z_3_Qualibot_Register_Chat_Prompts` | https://dbc-77b87d5a-6bcb.cloud.databricks.com/jobs/940803650450984 |
| `Z_3_Qualibot_Endpoint_Creation` | https://dbc-77b87d5a-6bcb.cloud.databricks.com/jobs/492260422393526 |
| `Z_4_Qualibot_Retrieval_Eval` | https://dbc-77b87d5a-6bcb.cloud.databricks.com/jobs/694804097755110 |
| `W_1_Qualibot_Start_Weekday_Prod` | https://dbc-77b87d5a-6bcb.cloud.databricks.com/jobs/1051468687947987 |
| `W_1_Qualibot_Stop_Weekend_Prod` | https://dbc-77b87d5a-6bcb.cloud.databricks.com/jobs/559666163027745 |

**Lakebase** : projet `qualibot`, branche `production`, base `doccompare` ; https://dbc-77b87d5a-6bcb.cloud.databricks.com/lakebase/projects/deb195f2-6189-419e-a640-b29fa71e540c

## 13. À traiter / questions ouvertes

- Épingler les versions des bibliothèques installées sur le cluster de parsing.
- Corriger `2_manifest` pour qu'un run avec `parse_filter` ne supprime pas les chunks des anciennes révisions sans parser les nouvelles.
- Option `full` en MERGE plutôt qu'en écrasement (les chunks ne disparaîtraient plus pendant le run).
