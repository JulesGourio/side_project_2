# Lakebase — tables et colonnes

Référence des tables de l'application dans Lakebase (projet `qualibot`, branche `production`). Une base par environnement : `doccompare` (UAT, DEV, PROD) et `doccompare_test` (`qualibot-uat-test`).

Source de vérité : `server/services/infra/lakebase/schema.py` (création et mise à jour), `columns.py` (colonnes du journal du chat). Les tables sont créées au démarrage de l'app (`ensure_schema`) : `CREATE TABLE IF NOT EXISTS`, puis `ALTER TABLE … ADD COLUMN IF NOT EXISTS`. Au démarrage, l'app retire des colonnes obsolètes et fusionne les doublons de la table `users` ; aucune table n'est supprimée.

Une colonne nouvelle s'ajoute dans `schema.py` (ou dans `columns.py` pour les tables du chat). Une clé nouvelle du journal d'un tour (`TurnLog.data`) doit avoir sa colonne dans `CHAT_TURN_COLUMNS` : `tests/test_lakebase_logging.py` le vérifie. Pour une base déjà en service sans redémarrage de l'app, la même colonne se passe par `runbooks/scripts/lakebase/migrations.py`.

## Vue d'ensemble

| Table | Une ligne par | Écrite par |
|---|---|---|
| `chat_turns` | tour de chat (question → réponse), réussi ou non | `infra/lakebase/audit.py` (`store_chat_turn`) |
| `chat_retrieved_chunks` | passage retrouvé pour un tour, envoyé au modèle ou écarté | `infra/lakebase/audit.py` |
| `chat_sessions` | conversation du chat | `routers/chat/persistence.py`, `routers/chat/sessions.py` |
| `chat_messages` | message d'une conversation | `routers/chat/persistence.py`, `routers/chat/sessions.py` |
| `chat_feedbacks` | vote 👍 / 👎 sur un message du chat | `routers/chat/sessions.py` |
| `errors` | erreur, ou avertissement d'une étape (`severity`) | `infra/lakebase/audit.py` |
| `messages` | analyse de comparaison, historique | `routers/history.py` |
| `feedbacks` | vote sur une analyse de comparaison | `routers/feedback.py` |
| `llm_requests` | appel d'analyse `/compare/analyze` | `infra/lakebase/audit.py` |
| `impact_requests` | impact search (`/compare/impact`) | `infra/lakebase/audit.py` |
| `impact_document_results` | document jugé par le modèle dans une impact search | `infra/lakebase/audit.py` |
| `impact_feedbacks` | vote sur une impact search : sur le résultat entier, ou sur le verdict d'un document | `routers/feedback.py` |
| `impact_cache` | résultat d'impact search mis en cache | `infra/lakebase/cache.py` |
| `summary_cache` | résumé d'un document mis en cache | `infra/lakebase/cache.py` |
| `users` | utilisateur connu de l'app | `infra/lakebase/users.py` |
| `knowledge_base_metadata` | date « documents à jour au » (une seule ligne, `id = 1`) | saisie à la main ; lue par `routers/config.py` |
| `doc_catalog` | catalogue des documents : référence, titre, lien, révision, date | tâche `6_update_kb_metadata` du pipeline ; lue par `infra/doc_catalog.py` |

**Clés de jointure du chat** :
- `trace_id` (`chat-` suivi de 32 caractères hexadécimaux) : présent dans `chat_turns`, `chat_retrieved_chunks`, `errors` et `chat_messages`.
- `chat_turns.user_message_id` et `chat_turns.assistant_message_id` : `chat_messages.id` des deux messages du tour.
- `chat_turns.mlflow_trace_id` : identifiant de la trace MLflow du tour (vide si `CHAT_MLFLOW_EXPERIMENT` est vide ou si l'envoi a échoué). Le vote 👍 / 👎 y est rattaché comme évaluation.

## Chat

### `chat_turns`

Une ligne par tour. Les colonnes de la première table sont fixes ; les autres viennent de `CHAT_TURN_COLUMNS` (`columns.py`).

**Résultat**

| Colonne | Type | Sens |
|---|---|---|
| `id` | SERIAL | clé primaire |
| `created_at` | TIMESTAMPTZ | date du tour |
| `trace_id` | TEXT NOT NULL, UNIQUE | identifiant du tour (`chat-…`) |
| `user_message_id` | INTEGER | `chat_messages.id` de la question (NULL après suppression) |
| `assistant_message_id` | INTEGER | `chat_messages.id` de la réponse (NULL après suppression) |
| `status` | TEXT NOT NULL | `ok`, `degraded`, `error` ou `aborted` (voir ci-dessous) |
| `error_stage` | TEXT | étape en échec |
| `error_type` | TEXT | type de l'erreur |
| `error_msg` | TEXT | message de l'erreur |
| `warnings` | TEXT[] NOT NULL, défaut `{}` | codes des avertissements du tour (voir « Codes ») |

`status` : `ok` = réponse donnée, rien à signaler ; `degraded` = réponse donnée, mais au moins une étape n'a pas tourné comme configuré (`warnings` dit laquelle) ; `error` = pas de réponse ; `aborted` = le navigateur est parti avant la fin.

**Ce qui était attendu**

| Colonne | Type | Sens |
|---|---|---|
| `index_name` | TEXT | index de recherche utilisé |
| `config` | JSONB | réglages du moteur au moment du tour (index, tailles de recherche, modèles de réponse et de secours, modèle de réécriture, limites de tokens) |

**Question et identité**

| Colonne | Type | Sens |
|---|---|---|
| `session_id` | TEXT | conversation (pas de clé étrangère : un tour dont les messages n'ont pas pu être écrits reste journalisé) |
| `user_id` | TEXT | utilisateur |
| `workspace_id` | TEXT | espace de travail Databricks |
| `app_version` | TEXT | version de l'app |
| `division` | TEXT | périmètre demandé : `ALL`, `AS` ou `IS` |
| `question` | TEXT | question posée |
| `question_lang` | TEXT | langue détectée de la question |
| `question_en` | TEXT | question envoyée à la recherche quand elle a été traduite |
| `history_messages` | INTEGER | messages de la conversation transmis au moteur |

**Réécriture de la requête**

| Colonne | Type | Sens |
|---|---|---|
| `fr_query` | TEXT | requête de recherche en français |
| `en_query` | TEXT | requête de recherche en anglais |
| `rewrite_ok` | BOOLEAN | la réécriture (ou la traduction) a réussi |
| `rewrite_mode` | TEXT | `translate` : premier message, seulement traduit dans l'autre langue ; `rewrite` : réécriture bilingue |
| `rewrite_endpoint` | TEXT | modèle qui a réécrit (NULL : tous les modèles ont échoué) |
| `rewrite_attempts` | JSONB | chaque essai de réécriture ou de traduction, relances comprises |
| `rewrite_input_tokens` | INTEGER | tokens en entrée de la réécriture |
| `rewrite_output_tokens` | INTEGER | tokens en sortie de la réécriture |
| `rewrite_cost_eur` | DOUBLE PRECISION | coût de la réécriture |

**Recherche**

| Colonne | Type | Sens |
|---|---|---|
| `named_refs` | TEXT[] | références (REF) nommées dans la conversation, recherchées en plus |
| `vs_calls_expected` | INTEGER | requêtes Vector Search prévues |
| `vs_calls_ok` | INTEGER | requêtes Vector Search réussies |
| `rerank_ok` | BOOLEAN | le reranker a répondu |
| `passages_retrieved` | INTEGER | passages retrouvés (lignes de `chat_retrieved_chunks`) |
| `passages_sent` | INTEGER | passages transmis au modèle (`kept`) |
| `documents_sent` | INTEGER | documents distincts transmis au modèle |
| `prompt_chars` | INTEGER | taille du prompt, en caractères |
| `instructions_sha` | TEXT | empreinte (16 caractères) des instructions système utilisées ; change quand `server/prompts/chat/system/*.md` change |
| `mlflow_trace_id` | TEXT | trace MLflow du tour |

**Réponse**

| Colonne | Type | Sens |
|---|---|---|
| `answer_endpoint` | TEXT | modèle qui a répondu (NULL : aucun) |
| `llm_fallback` | BOOLEAN | réponse du modèle de secours |
| `llm_attempts` | JSONB | chaque appel au modèle : endpoint, résultat, statut HTTP, secondes |
| `truncated` | BOOLEAN | limite de tokens atteinte |
| `continuations` | INTEGER | nombre de reprises après une coupure |
| `input_tokens` | INTEGER | tokens en entrée de la réponse |
| `output_tokens` | INTEGER | tokens en sortie de la réponse |
| `thinking_tokens` | INTEGER | tokens de raisonnement |
| `cost_eur` | DOUBLE PRECISION | coût de la réponse (la réécriture est dans `rewrite_cost_eur`) |
| `answer_chars` | INTEGER | taille de la réponse, en caractères |
| `citations_count` | INTEGER | nombre de citations dans la réponse |
| `cited_refs` | TEXT[] | références citées dans la réponse |
| `sources_count` | INTEGER | pastilles de source affichées (citées ou nommées dans le texte) |
| `answer_translated` | BOOLEAN | réponse traduite dans la langue de la question |
| `user_message` | TEXT | message affiché à l'utilisateur en cas d'échec (NULL : rien) |

**Durées** (millisecondes, INTEGER)

| Colonne | Mesure |
|---|---|
| `total_ms` | du message reçu à la fin du tour (réponse envoyée, messages sauvegardés) |
| `first_token_ms` | du message reçu au premier mot de la réponse : ce que l'utilisateur attend |
| `translate_in_ms` | détection de langue et traduction de la question |
| `retrieval_ms` | toute la recherche : réécriture, recherche et recherche des références |
| `rewrite_ms` | réécriture |
| `search_ms` | recherche Vector Search |
| `ref_lookup_ms` | recherche des références nommées |
| `queue_wait_ms` | attente d'un créneau de génération (`CHAT_MAX_CONCURRENT_ANSWERS`) |
| `generation_ms` | génération de la réponse, relances comprises |
| `translate_back_ms` | traduction de la réponse |
| `persist_ms` | écriture de `chat_messages` |

**Codes** (`warnings`, un code par problème ; le détail est dans `errors`, même `trace_id`) :

| Code | Ce qui s'est passé |
|---|---|
| `translate_in_failed` | détection ou traduction de la question en échec : recherche faite avec la question d'origine |
| `rewrite_failed` | aucun modèle n'a réécrit la requête : recherche avec la question seule |
| `rewrite_fallback` | réécriture faite par le modèle de secours |
| `vs_partial` | une partie des requêtes Vector Search a échoué (`vs_calls_ok` < `vs_calls_expected`) |
| `rerank_refused` | l'index a refusé le reranker : recherche brute seule |
| `no_passages` | aucun passage trouvé |
| `llm_fallback` | réponse du modèle de secours |
| `llm_retried` | appels en échec (429, timeout…) relancés avant la réponse |
| `answer_continued` | réponse coupée puis reprise |
| `answer_interrupted` | réponse coupée, aucun modèle n'a pu la finir |
| `translate_back_failed` | traduction de la réponse en échec : réponse laissée dans la langue du modèle |
| `persist_failed` | les messages n'ont pas pu être écrits dans `chat_messages` |
| `client_disconnected`, `send_failed` | information sur la connexion du navigateur |

### `chat_retrieved_chunks`

Une ligne par passage retrouvé pour un tour, avec son texte. Les passages d'une révision remplacée sont supprimés de la table d'index : cette table garde ce sur quoi la réponse a été construite.

| Colonne | Type | Sens |
|---|---|---|
| `id` | BIGSERIAL | clé primaire |
| `created_at` | TIMESTAMPTZ | date d'écriture |
| `turn_id` | INTEGER NOT NULL | `chat_turns.id` (suppression en cascade) |
| `trace_id` | TEXT | `trace_id` du tour |
| `position` | INTEGER | ordre de récupération (fusion par meilleur rang) |
| `kept` | BOOLEAN NOT NULL, défaut `TRUE` | passage transmis au modèle |
| `drop_reason` | TEXT | si écarté : `other_language` (une autre langue du même document a été préférée) ou `over_cap` (au-delà de `CHAT_MAX_SEARCH_PASSAGES`) |
| `prompt_rank` | INTEGER | ordre dans le prompt (passages transmis seulement) |
| `doc_number` | INTEGER | numéro `[n]` du document dans le prompt |
| `cited` | BOOLEAN | le document est cité par la réponse |
| `source` | TEXT | `search` (trouvé par la recherche) ou `ref_lookup` (apporté par la recherche des références) |
| `hits` | JSONB | chaque requête qui a trouvé le passage : `q`, `via`, `rank`, `score` |
| `best_score` | DOUBLE PRECISION | meilleur score |
| `chunk_id` | TEXT | identifiant du passage dans l'index |
| `iddoc` | TEXT | identifiant Intraqual de la révision du document |
| `ref` | TEXT | référence du document |
| `division` | TEXT | division du document |
| `url` | TEXT | lien du document |
| `semantic_headers` | TEXT | en-têtes sémantiques du passage |
| `chunk_text` | TEXT | texte du passage |

Index : `idx_chat_retrieved_chunks_turn_id` (`turn_id`), `idx_chat_retrieved_chunks_ref` (`ref`).

### `chat_sessions`

| Colonne | Type | Sens |
|---|---|---|
| `id` | TEXT PK | identifiant de la conversation |
| `created_at`, `updated_at` | TIMESTAMPTZ | dates |
| `user_id`, `workspace_id`, `workspace_url` | TEXT | propriétaire |
| `name` | TEXT NOT NULL, défaut `New conversation` | titre |
| `deleted` | BOOLEAN NOT NULL, défaut `FALSE` | suppression logique : les lignes ne sont jamais effacées physiquement |
| `deleted_at` | TIMESTAMPTZ | date de la suppression logique |
| `share_token` | TEXT | jeton de partage en lecture seule (NULL : non partagée) |

Index unique : `chat_sessions_share_token_idx` (`share_token`, où il n'est pas NULL).

### `chat_messages`

| Colonne | Type | Sens |
|---|---|---|
| `id` | SERIAL PK | identifiant du message |
| `created_at` | TIMESTAMPTZ | date |
| `session_id` | TEXT | `chat_sessions.id` (suppression en cascade) |
| `user_id`, `workspace_id`, `workspace_url` | TEXT | auteur |
| `role` | TEXT NOT NULL | `user` ou `assistant` |
| `content` | TEXT NOT NULL | texte du message |
| `trace_id` | TEXT | `trace_id` du tour (`chat_turns`) |
| `endpoint_name` | TEXT | libellé du moteur : `chat-all`, `chat-as` ou `chat-is` (NULL sur les copies d'une conversation partagée). Le modèle qui a répondu est `chat_turns.answer_endpoint` |
| `sources_json` | TEXT | sources affichées avec la réponse (référence, titre, révision, date) |
| `deleted`, `deleted_at` | BOOLEAN, TIMESTAMPTZ | suppression logique |
| `status` | TEXT NOT NULL, défaut `ok` | `ok` ou `error` ; les tours en échec sont gardés, avec la raison |
| `error_msg` | TEXT | raison de l'échec |
| `division` | TEXT NOT NULL, défaut `ALL` | périmètre de la question : `ALL`, `AS` ou `IS` |
| `question_lang` | TEXT | langue de la question, telle que détectée par la traduction |

Index : `chat_messages_session_idx` (`session_id`).

### `chat_feedbacks`

| Colonne | Type | Sens |
|---|---|---|
| `id` | SERIAL PK | identifiant |
| `created_at` | TIMESTAMPTZ | date |
| `message_id` | INTEGER | `chat_messages.id` (NULL après suppression) |
| `session_id` | TEXT | `chat_sessions.id` (NULL après suppression) |
| `user_id`, `workspace_id`, `workspace_url` | TEXT | auteur du vote |
| `vote` | TEXT NOT NULL | `up` ou `down` |
| `comment` | TEXT | commentaire facultatif |
| `resolved` | BOOLEAN NOT NULL, défaut `FALSE` | vote traité (tableau de bord) |
| `resolution_reason` | TEXT | raison du traitement |

## Recherche de comparaison et historique

### `messages`

Une ligne par analyse de comparaison, pour l'historique.

| Colonne | Type | Sens |
|---|---|---|
| `id` | SERIAL PK | identifiant |
| `created_at` | TIMESTAMPTZ | date |
| `user_id`, `workspace_id`, `workspace_url` | TEXT | utilisateur |
| `old_filename`, `new_filename` | TEXT NOT NULL | noms des fichiers comparés |
| `old_file_hash`, `new_file_hash` | TEXT | empreintes des fichiers |
| `analysis_text` | TEXT | résultat de l'analyse |
| `impact_text` | TEXT | résultat de l'impact search rattaché à la comparaison |
| `volume_session_path` | TEXT | chemin des fichiers sur le volume de la session |
| `file_type` | TEXT | type de fichier |
| `processing_method` | TEXT | méthode d'extraction |
| `processor_version` | TEXT | version du processeur |
| `ttft_s`, `generation_s` | DOUBLE PRECISION | secondes avant le premier mot, et de génération |
| `input_tokens`, `output_tokens`, `thinking_tokens`, `total_tokens` | INTEGER | tokens |
| `cost_eur` | DOUBLE PRECISION | coût |
| `app_version` | TEXT | version de l'app |
| `llm_request_id` | INTEGER | `llm_requests.id` de l'analyse |
| `endpoint_name` | TEXT | modèle utilisé (copie de `llm_requests.endpoint_name`) |

### `feedbacks`

Votes sur une analyse de comparaison.

| Colonne | Type | Sens |
|---|---|---|
| `id` | SERIAL PK | identifiant |
| `created_at` | TIMESTAMPTZ | date |
| `message_id` | INTEGER | `messages.id` (NULL après suppression) |
| `user_id`, `workspace_id`, `workspace_url` | TEXT | auteur |
| `vote` | TEXT NOT NULL | `up` ou `down` |
| `comment` | TEXT | commentaire |
| `resolved` | BOOLEAN NOT NULL, défaut `FALSE` | vote traité |
| `resolution_reason` | TEXT | raison du traitement |

### `llm_requests`

Une ligne par analyse `/compare/analyze`.

| Colonne | Type | Sens |
|---|---|---|
| `id` | SERIAL PK | identifiant |
| `created_at` | TIMESTAMPTZ | date |
| `old_file_hash`, `new_file_hash` | TEXT | empreintes |
| `file_type` | TEXT | type de fichier |
| `endpoint_name` | TEXT | modèle |
| `messages_json` | TEXT | messages envoyés au modèle |
| `input_tokens`, `output_tokens`, `thinking_tokens`, `total_tokens` | INTEGER | tokens |
| `cost_eur` | DOUBLE PRECISION | coût |
| `http_status` | INTEGER | statut HTTP de l'appel |
| `error_type`, `error_msg` | TEXT | erreur éventuelle |
| `status` | TEXT | `ok`, `error` ou `aborted` (le navigateur est parti avant la fin) |
| `queue_wait_ms` | INTEGER | attente d'un créneau d'analyse |
| `build_ms` | INTEGER | extraction des fichiers et prompt |
| `first_token_ms` | INTEGER | de l'appel au premier mot |
| `generation_ms` | INTEGER | de l'appel à la fin |
| `total_ms` | INTEGER | durée totale |
| `chunked_parts` | INTEGER | nombre de parties (découpage en plusieurs appels ; 0 : un seul appel) |

## Impact search

### `impact_requests`

Une ligne par appel à `/compare/impact`, réussi ou non.

| Colonne | Type | Sens |
|---|---|---|
| `id` | SERIAL PK | identifiant |
| `created_at` | TIMESTAMPTZ | date |
| `user_id`, `workspace_id` | TEXT | utilisateur |
| `method` | TEXT NOT NULL | méthode de la recherche d'impact (ex. `index_llm_per_doc`) |
| `old_file_hash`, `new_file_hash` | TEXT | empreintes |
| `changes_chars` | INTEGER | taille de la liste des changements, en caractères |
| `truncated` | BOOLEAN | liste des changements tronquée |
| `chunks_returned` | INTEGER | passages retournés par la recherche |
| `num_documents` | INTEGER | documents trouvés |
| `duration_s` | DOUBLE PRECISION | durée totale, en secondes |
| `endpoint_name` | TEXT | modèle du juge |
| `input_tokens`, `output_tokens`, `total_tokens` | INTEGER | tokens |
| `cost_eur` | DOUBLE PRECISION | coût |
| `http_status` | INTEGER | statut HTTP |
| `error_type`, `error_msg` | TEXT | erreur éventuelle |
| `status` | TEXT | `ok`, `error` ou `aborted` |
| `app_version` | TEXT | version de l'app |
| `index_name` | TEXT | index de recherche |
| `queries_used` | INTEGER | requêtes Vector Search (une par changement) |
| `queries_failed` | INTEGER | requêtes en échec |
| `candidates` | INTEGER | documents envoyés au juge |
| `not_judged` | INTEGER | documents trouvés, au-delà de `impact_max_candidates` (non jugés) |
| `judge_failed` | INTEGER | appels du juge en échec après relances |
| `extract_ms` | INTEGER | liste des changements → requêtes |
| `search_ms` | INTEGER | recherche |
| `judge_ms` | INTEGER | tous les appels du juge (en parallèle) |

### `impact_document_results`

Une ligne par document jugé par le modèle.

| Colonne | Type | Sens |
|---|---|---|
| `id` | SERIAL PK | identifiant |
| `created_at` | TIMESTAMPTZ | date |
| `request_id` | INTEGER NOT NULL | `impact_requests.id` (suppression en cascade) |
| `ref` | TEXT | référence du document |
| `division` | TEXT | division |
| `url` | TEXT | lien |
| `impacted` | BOOLEAN | le document est impacté |
| `confidence` | TEXT | confiance du verdict |
| `section` | TEXT | section concernée |
| `reason` | TEXT | justification |
| `other_documents_json` | TEXT | autres documents cités par le verdict |
| `status` | TEXT | `impacted`, `check` (à vérifier), `not_impacted` ou `error` |
| `judge_ms` | INTEGER | durée de l'appel du juge |
| `judge_attempts` | INTEGER | essais de l'appel |
| `input_tokens`, `output_tokens` | INTEGER | tokens de l'appel |

Index : `idx_impact_document_results_request_id` (`request_id`).

### `impact_feedbacks`

Votes sur une impact search : sur le résultat entier (`ref` NULL, commentaire facultatif) ou sur le verdict d'un document.

| Colonne | Type | Sens |
|---|---|---|
| `id` | SERIAL PK | identifiant |
| `created_at` | TIMESTAMPTZ | date |
| `impact_request_id` | INTEGER | `impact_requests.id` (NULL après suppression) |
| `message_id` | INTEGER | `messages.id` de la comparaison (NULL après suppression) |
| `user_id`, `workspace_id`, `workspace_url` | TEXT | auteur |
| `old_file_hash`, `new_file_hash` | TEXT | empreintes des fichiers |
| `ref` | TEXT | document voté (NULL : vote sur le résultat entier) |
| `verdict_shown` | TEXT | verdict affiché au moment du vote |
| `vote` | TEXT NOT NULL | `up` ou `down` |
| `comment` | TEXT | commentaire |
| `resolved` | BOOLEAN NOT NULL, défaut `FALSE` | vote traité |
| `resolution_reason` | TEXT | raison du traitement |

Index : `idx_impact_feedbacks_request_id` (`impact_request_id`).

### `impact_cache`

Résultat d'une impact search, réutilisé pour les mêmes fichiers (« Re-run » l'ignore).

| Colonne | Type | Sens |
|---|---|---|
| `id` | SERIAL PK | identifiant |
| `created_at` | TIMESTAMPTZ | date |
| `old_file_hash`, `new_file_hash` | TEXT NOT NULL | empreintes des deux fichiers |
| `app_version` | TEXT NOT NULL | version de l'app |
| `result_json` | TEXT NOT NULL | résultat complet |

Index : `idx_impact_cache_hashes` (`old_file_hash`, `new_file_hash`, `app_version`).

### `summary_cache`

Résumé d'un document, réutilisé pour le même fichier.

| Colonne | Type | Sens |
|---|---|---|
| `id` | SERIAL PK | identifiant |
| `created_at` | TIMESTAMPTZ | date |
| `file_hash` | TEXT NOT NULL | empreinte du document |
| `app_version` | TEXT NOT NULL | version de l'app |
| `result_json` | TEXT NOT NULL | résumé |
| `endpoint_name` | TEXT | modèle qui a produit le résumé |

Index : `idx_summary_cache_hash` (`file_hash`, `app_version`).

## Journal des erreurs

### `errors`

Une ligne par erreur ou avertissement. Les avertissements d'un tour de chat sont écrits avec le tour (une ligne par code de `warnings`).

| Colonne | Type | Sens |
|---|---|---|
| `id` | SERIAL PK | identifiant |
| `created_at` | TIMESTAMPTZ | date |
| `user_id`, `workspace_id` | TEXT | utilisateur |
| `endpoint` | TEXT | route de l'app (ex. `/api/chat/ws`) |
| `error_type` | TEXT | type de l'erreur |
| `error_msg` | TEXT | message de l'erreur |
| `old_filename`, `new_filename`, `file_type` | TEXT | fichiers concernés (comparaison) |
| `stack_trace` | TEXT | pile d'appels (4 000 caractères au plus) |
| `llm_request_id` | INTEGER | `llm_requests.id` concerné |
| `severity` | TEXT NOT NULL, défaut `error` | `error` (la requête a échoué) ou `warning` (elle a continué, dégradée) |
| `stage` | TEXT | étape en échec. Chat : `translate_in`, `rewrite`, `search`, `ref_lookup`, `llm`, `translate_back`, `persist`, `ws`. Comparaison : `build`, `llm`, `stream` (analyse) ; `search`, `judge` (impact) |
| `http_status` | INTEGER | statut HTTP |
| `upstream` | TEXT | index Vector Search ou endpoint de modèle appelé |
| `trace_id` | TEXT | `trace_id` du tour de chat |
| `session_id` | TEXT | conversation |
| `context` | JSONB | attendu et obtenu : chaîne de modèles prévue, essais, requêtes envoyées ou échouées, code de l'avertissement |
| `app_version` | TEXT | version de l'app |
| `origin` | TEXT | endroit du code, `chemin:ligne in fonction` (la dernière ligne de la pile dans ce dépôt, sinon le code qui a signalé l'erreur) |
| `fingerprint` | TEXT | 12 caractères qui regroupent la même erreur d'une requête à l'autre (même route, étape, type et `origin`, numéro de ligne ignoré) |
| `user_message` | TEXT | ce que l'utilisateur a vu ; NULL : rien n'a été montré |

`user_message` dans le chat : toujours le même texte, envoyé seulement quand toutes les relances ont échoué. Aucun message technique n'est envoyé au navigateur du chat : la cause n'est que dans `errors`, `chat_turns.error_*` et `chat_messages.error_msg`.

Index : `idx_errors_created_at` (`created_at`), `idx_errors_trace_id` (`trace_id`), `idx_errors_fingerprint` (`fingerprint`).

## Divers

### `users`

| Colonne | Type | Sens |
|---|---|---|
| `id` | SERIAL PK | identifiant |
| `created_at`, `updated_at` | TIMESTAMPTZ | dates |
| `user_id` | TEXT UNIQUE NOT NULL | identifiant Databricks de l'utilisateur |
| `workspace_id` | TEXT | espace de travail |
| `email` | TEXT | e-mail (sans espaces ; unique quand il n'est pas NULL) |
| `display_name` | TEXT | nom affiché |
| `workspace_url` | TEXT | adresse de l'espace de travail |
| `role` | TEXT NOT NULL, défaut `user` | rôle |
| `is_active` | BOOLEAN NOT NULL, défaut `TRUE` | compte actif |

Index unique : `users_email_idx` (`email`, où il n'est pas NULL).

### `knowledge_base_metadata`

Une seule ligne (`id = 1`, contrainte `single_row`) : la date « documents à jour au », affichée dans l'interface.

| Colonne | Type | Sens |
|---|---|---|
| `id` | INTEGER PK, défaut `1` | toujours `1` |
| `documents_as_of` | DATE | date affichée |
| `updated_at` | TIMESTAMPTZ | dernière mise à jour |

### `doc_catalog`

Catalogue des documents du périmètre de recherche. Réécrit par la tâche `6_update_kb_metadata` du pipeline après chaque run quotidien (`utils/parsing_pipeline/create_proj_doc_catalog.py`) ; lu par l'app (rechargement toutes les 30 min).

| Colonne | Type | Sens |
|---|---|---|
| `ref` | TEXT PK | référence du document |
| `base_ref` | TEXT | référence de base ; NULL : calculée par l'app (`doc_catalog._canon`) |
| `title` | TEXT | titre |
| `url` | TEXT | lien |
| `division` | TEXT | division |
| `in_chat` | BOOLEAN NOT NULL, défaut `TRUE` | document présent dans l'index du chat |
| `updated_at` | TIMESTAMPTZ NOT NULL | dernière écriture |
| `revision` | TEXT | révision en cours (indice Intraqual) |
| `doc_date` | DATE | date de publication de la révision |

Les colonnes `revision` et `doc_date` sont ajoutées par l'app si elle est propriétaire de la table ; sinon par la tâche 6 du pipeline. L'app les donne au modèle avec chaque document et les affiche sur les pastilles de source.

## Requêtes de contrôle

Exemples de contrôle des tables, pour l'éditeur SQL Lakebase de la base concernée :

```sql
-- Tours de la semaine par statut, durées médianes
SELECT status, COUNT(*),
       PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY total_ms)       AS total_p50,
       PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY first_token_ms) AS first_token_p50
FROM chat_turns WHERE created_at > NOW() - INTERVAL '7 days' GROUP BY status;

-- Avertissements les plus fréquents
SELECT w AS warning, COUNT(*) FROM chat_turns, UNNEST(warnings) AS w
WHERE created_at > NOW() - INTERVAL '7 days' GROUP BY w ORDER BY 2 DESC;

-- Un tour en détail : ses passages, dans l'ordre du prompt
SELECT prompt_rank, doc_number, ref, cited, source, best_score, LEFT(chunk_text, 120)
FROM chat_retrieved_chunks WHERE trace_id = 'chat-…' AND kept ORDER BY prompt_rank;

-- Erreurs par empreinte, sur 7 jours
SELECT fingerprint, endpoint, severity, stage, error_type, origin, COUNT(*) AS n,
       MIN(created_at) AS first_seen, MAX(created_at) AS last_seen, MAX(LEFT(error_msg, 150)) AS example
FROM errors WHERE created_at > NOW() - INTERVAL '7 days'
GROUP BY 1, 2, 3, 4, 5, 6 ORDER BY n DESC;
```

## Index

| Table | Index |
|---|---|
| `chat_turns` | `idx_chat_turns_created_at` (`created_at`), `idx_chat_turns_session_id` (`session_id`), `idx_chat_turns_assistant_message_id` (`assistant_message_id`) |
| `chat_retrieved_chunks` | `idx_chat_retrieved_chunks_turn_id` (`turn_id`), `idx_chat_retrieved_chunks_ref` (`ref`) |
| `chat_sessions` | `chat_sessions_share_token_idx` (`share_token`, unique, où il n'est pas NULL) |
| `chat_messages` | `chat_messages_session_idx` (`session_id`) |
| `errors` | `idx_errors_created_at`, `idx_errors_trace_id`, `idx_errors_fingerprint` |
| `impact_document_results` | `idx_impact_document_results_request_id` (`request_id`) |
| `impact_feedbacks` | `idx_impact_feedbacks_request_id` (`impact_request_id`) |
| `impact_cache` | `idx_impact_cache_hashes` (`old_file_hash`, `new_file_hash`, `app_version`) |
| `summary_cache` | `idx_summary_cache_hash` (`file_hash`, `app_version`) |
| `users` | `users_email_idx` (`email`, unique, où il n'est pas NULL) |
