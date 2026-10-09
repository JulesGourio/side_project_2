# Lakebase — ce que l'app enregistre de chaque requête (schéma du 2026-10-09)

Avec le Knowledge Assistant, le chat était une boîte noire : on ne gardait que la question, la
réponse et les REF cités. Le moteur Vector Search (`server/services/chat_vsi.py`) fait chaque étape
lui-même ; elles sont maintenant toutes enregistrées : requêtes de recherche, passages retrouvés
(avec leur texte), modèles appelés, durées, et tout ce qui ne s'est pas passé comme prévu.

Le schéma est créé et mis à jour par l'app à son démarrage (`_ensure_schema` dans
`server/services/lakebase.py`) : aucune DDL à lancer à la main. Que des `CREATE TABLE IF NOT
EXISTS` / `ADD COLUMN IF NOT EXISTS` : aucune donnée existante n'est touchée.

## Les tables du journal

| Table | Une ligne par | Nouveau |
|---|---|---|
| `chat_turns` | tour de chat (question → réponse), réussi ou non | **oui** |
| `chat_retrieved_chunks` | passage retrouvé pour un tour (envoyé au modèle ou écarté), avec son texte | **oui** |
| `errors` | erreur, ou étape dégradée (`severity = 'warning'`) | colonnes ajoutées |
| `llm_requests` | analyse `/compare/analyze` | colonnes ajoutées (statut, durées) |
| `impact_requests` | impact search | colonnes ajoutées (statut, compteurs, durées) |
| `impact_document_results` | document jugé dans une impact search | colonnes ajoutées (appel du juge) |

Clé de jointure du chat : **`trace_id`** (`vsi-<32 hex>`), présent dans `chat_turns`,
`chat_retrieved_chunks`, `errors` et sur les deux lignes `chat_messages` du tour.
`chat_turns.user_message_id` / `assistant_message_id` pointent aussi sur `chat_messages`.

## `chat_turns`

**Résultat** — `status` :

| `status` | Sens |
|---|---|
| `ok` | réponse donnée, tout s'est déroulé comme configuré |
| `degraded` | réponse donnée, mais au moins une étape n'a pas tourné comme prévu (`warnings` dit laquelle) |
| `error` | pas de réponse ; `error_stage` / `error_type` / `error_msg` disent où et pourquoi |
| `aborted` | le navigateur est parti avant la fin (onglet fermé, nouvelle question) |

Seuls les tours `ok`/`degraded` apparaissent dans l'historique de l'utilisateur (les lignes
`chat_messages` d'un tour `error` ou `aborted` ont ce statut et sont masquées, comme avant).

**`warnings`** (codes, dans l'ordre où ils sont arrivés ; le détail est dans `errors`, même
`trace_id`) :

| Code | Étape | Ce qui s'est passé |
|---|---|---|
| `translate_in_failed` | traduction de la question | détection / traduction en échec : recherche faite avec la question d'origine |
| `rewrite_failed` | reformulation | aucun modèle n'a reformulé : recherche avec la question seule (1 requête au lieu de 3) |
| `rewrite_fallback` | reformulation | reformulée par le modèle de secours |
| `vs_partial` | recherche | une partie des requêtes Vector Search a échoué (`vs_calls_ok` < `vs_calls_expected`) |
| `rerank_refused` | recherche | l'index a refusé le reranker : recherche brute seule |
| `ref_lookup_failed` / `title_lookup_failed` | recherche | la recherche ciblée sur les REF / titres nommés a échoué |
| `no_passages` | recherche | aucun passage trouvé |
| `llm_fallback` | réponse | réponse du modèle de secours (GPT-5.6 Luna) au lieu de GPT-6 Luna |
| `llm_retried` | réponse | appels en échec (429, timeout…) avant la réponse du modèle principal |
| `output_truncated` | réponse | limite de tokens atteinte : la fin peut manquer |
| `answer_continued` | réponse | réponse coupée puis reprise par un modèle |
| `answer_interrupted` | réponse | réponse coupée, aucun modèle n'a pu la finir |
| `translate_back_failed` | traduction de la réponse | réponse laissée en anglais |
| `persist_failed` | sauvegarde | les messages n'ont pas pu être écrits dans `chat_messages` |
| `client_disconnected` / `send_failed` | WebSocket | le navigateur est parti (information, ne rend pas le tour `degraded`) |

**Ce qui était attendu** — `config` (JSONB) : `chat_vsi.settings()` au moment du tour — index,
nombre de passages rerankés / bruts par requête, plafond, modèle de réponse et ses secours, modèle
de reformulation, limites de tokens. À comparer avec ce qui s'est passé :
`vs_calls_expected`/`vs_calls_ok`, `rerank_ok`, `rewrite_ok`/`rewrite_endpoint`,
`answer_endpoint`/`llm_fallback`, `llm_attempts` (chaque appel : endpoint, résultat, statut HTTP,
secondes).

**Question et recherche** — `question`, `question_lang`, `question_en` (question envoyée à la
recherche quand le bridge l'a traduite), `history_messages`, `fr_query`, `en_query`,
`named_refs` / `titled_refs` (documents cherchés en plus, parce que nommés dans la conversation ou
parce que leur titre correspond), `passages_retrieved` / `passages_sent` / `documents_sent`,
`prompt_chars`, `instructions_sha` (empreinte des instructions système utilisées : change quand
`instructions_*.md` ou `answer_rules.md` change).

**Réponse** — tokens et coût de la réponse (`input_tokens`, `output_tokens`, `thinking_tokens`,
`cost_eur`) et de la reformulation (`rewrite_*`), `truncated`, `continuations`, `answer_chars`,
`citations_count`, `cited_refs`, `sources_count` (pastilles affichées), `answer_translated`.

**Durées** (millisecondes) :

| Colonne | Mesure |
|---|---|
| `total_ms` | du message reçu à la fin du tour (réponse envoyée et messages sauvegardés) |
| `first_token_ms` | du message reçu au premier mot de la réponse — ce que l'utilisateur attend (avec le bridge, il ne voit rien avant la traduction complète) |
| `translate_in_ms` | détection de langue + traduction de la question |
| `retrieval_ms` | toute la recherche (= reformulation + recherche + recherches ciblées) |
| `rewrite_ms`, `search_ms`, `ref_lookup_ms`, `title_lookup_ms` | chacune de ces étapes |
| `queue_wait_ms` | attente d'un créneau de génération (`CHAT_VSI_MAX_CONCURRENT_ANSWERS`) |
| `generation_ms` | génération de la réponse (après l'attente), relances comprises |
| `translate_back_ms` | traduction de la réponse |
| `persist_ms` | écriture de `chat_messages` |

## `chat_retrieved_chunks`

Tous les passages retrouvés pour un tour, **avec leur texte** : le pipeline supprime les passages
d'une révision remplacée (`2_Cleanup_Volume`), un simple `chunk_id` deviendrait illisible. Écrit
dans la même transaction que `chat_turns`, après l'envoi de la réponse : l'utilisateur n'attend pas
cette écriture.

- `kept` : envoyé au modèle ; sinon `drop_reason` = `other_language` (une autre langue du même
  document a été préférée) ou `over_cap` (au-delà de `CHAT_VSI_MAX_SEARCH_PASSAGES`, 0 = pas de
  plafond par défaut) ;
- `position` (ordre de retrieval), `prompt_rank` (ordre dans le prompt), `doc_number` (le `[n]` du
  document dans le prompt), `cited` (son document est cité par la réponse) ;
- `source` : `search` (trouvé par la recherche principale) ou `ref_lookup` / `title_lookup`
  (apporté seulement par la recherche ciblée) ;
- `hits` (JSONB) : chaque requête qui l'a trouvé — `q` (0 = question telle quelle, 1 = FR,
  2 = EN, `null` pour une recherche ciblée), `via` (`rerank`, `raw`, `ref_lookup`, `title_lookup`),
  `rank`, `score` ; `best_score` ;
- `chunk_id`, `iddoc`, `ref`, `division`, `url`, `semantic_headers`, `chunk_text`.

Volume : environ 30 à 40 lignes et 50 Ko par tour, soit environ 50 Mo pour 1 000 tours. Tout est
gardé (pas de purge).

## `errors` — colonnes ajoutées

`severity` (`error` ou `warning` ; les lignes existantes valent `error`), `stage`, `http_status`,
`upstream` (index Vector Search ou endpoint de modèle appelé), `trace_id`, `session_id`,
`context` (JSONB : attendu vs obtenu — chaîne de modèles prévue et essais, requêtes envoyées /
échouées, code du warning…), `app_version`. La pile d'appels reste dans `stack_trace`.

Côté chat, les lignes `errors` sont écrites avec le tour (une par warning ou erreur). Côté
Compare : `/compare/analyze` renseigne `stage` (`build` = extraction des fichiers, `llm`,
`stream`) ; `/compare/impact` écrit un `warning` pour une recherche partielle
(`VectorSearchPartial`) et pour chaque appel du juge en échec, et `stage` = `search` / `judge`
pour un échec complet.

## Compare et impact search — colonnes ajoutées

- `llm_requests` (une analyse) : `status` (`ok` / `error` / `aborted`), `queue_wait_ms` (attente
  d'un créneau d'analyse), `build_ms` (extraction + prompt), `first_token_ms`, `generation_ms`,
  `total_ms`, `chunked_parts` (nombre de parties en map-reduce, 0 = un seul appel).
- `impact_requests` : `status` (`ok` / `error` / `aborted`), `app_version`, `index_name`,
  `queries_used`, `queries_failed`, `candidates`, `not_judged`, `judge_failed`, `extract_ms`,
  `search_ms`, `judge_ms` (`duration_s` reste le total). Une recherche interrompue par le
  navigateur est maintenant enregistrée (`aborted`).
- `impact_document_results` : `status`, `judge_ms`, `judge_attempts`, `input_tokens`,
  `output_tokens` (en fin de ligne, la partie lisible par le métier reste en tête).

## Champs obsolètes

`chat_messages.tool_name`, `tool_query`, `tool_result`, `reasoning_steps` : propres au Knowledge
Assistant, **plus écrits depuis le 2026-10-09**, gardés pour l'historique des tours KA (à supprimer
plus tard, après un export). `chat_messages.trace_id` est toujours écrit (= `chat_turns.trace_id`).
`chat_messages.endpoint_name` est gardé : c'est le libellé du moteur (`vsi-all` / `vsi-as` /
`vsi-is`) et les notebooks de notation filtrent dessus (`NULL` = copie d'une conversation
partagée) ; le modèle réel est `chat_turns.answer_endpoint`.

## Requêtes utiles

```sql
-- Tours des 7 derniers jours par résultat, et durées médiane / p90
SELECT status, COUNT(*),
       PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY total_ms)       AS total_p50,
       PERCENTILE_CONT(0.9) WITHIN GROUP (ORDER BY total_ms)       AS total_p90,
       PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY first_token_ms) AS first_token_p50
FROM chat_turns WHERE created_at > NOW() - INTERVAL '7 days' GROUP BY status;

-- Ce qui dégrade les tours
SELECT w AS warning, COUNT(*) FROM chat_turns, UNNEST(warnings) AS w
WHERE created_at > NOW() - INTERVAL '7 days' GROUP BY w ORDER BY 2 DESC;

-- Où part le temps (moyenne par étape)
SELECT AVG(translate_in_ms), AVG(rewrite_ms), AVG(search_ms), AVG(ref_lookup_ms), AVG(title_lookup_ms),
       AVG(queue_wait_ms), AVG(generation_ms), AVG(translate_back_ms), AVG(persist_ms), AVG(total_ms)
FROM chat_turns WHERE status IN ('ok', 'degraded') AND created_at > NOW() - INTERVAL '7 days';

-- Un tour en détail : ses passages, dans l'ordre du prompt
SELECT prompt_rank, doc_number, ref, cited, source, best_score, LEFT(chunk_text, 120)
FROM chat_retrieved_chunks WHERE trace_id = 'vsi-…' AND kept ORDER BY prompt_rank;

-- Documents souvent envoyés au modèle mais jamais cités
SELECT ref, COUNT(*) AS sent, SUM(CASE WHEN cited THEN 1 ELSE 0 END) AS cited
FROM chat_retrieved_chunks WHERE kept GROUP BY ref HAVING SUM(CASE WHEN cited THEN 1 ELSE 0 END) = 0
ORDER BY sent DESC LIMIT 30;

-- Erreurs et warnings, toutes fonctions confondues
SELECT endpoint, severity, stage, error_type, COUNT(*) FROM errors
WHERE created_at > NOW() - INTERVAL '7 days' GROUP BY 1, 2, 3, 4 ORDER BY 5 DESC;

-- Votes négatifs avec ce qui s'est passé pendant le tour
SELECT f.created_at, f.comment, t.status, t.warnings, t.answer_endpoint, t.total_ms, t.fr_query
FROM chat_feedbacks f JOIN chat_turns t ON t.assistant_message_id = f.message_id
WHERE f.vote = 'down' ORDER BY f.created_at DESC;
```

## Notation de la qualité

`utils/evaluation/score_chat_traces.py` (prototype, lancé à la main) lit les tours répondus dans
Lakebase (`chat_turns`, `chat_messages`, `chat_feedbacks`, `chat_retrieved_chunks` `kept`, dans
l'ordre du prompt), les rejoue en traces MLflow sans rappeler aucun modèle (le span retriever rend
exactement les passages envoyés au modèle), puis les note avec les scorers MLflow (pertinence,
ancrage dans les passages, langue, limites avouées ; juge GPT-5.6 Luna). Une ligne par tour et par
scorer dans `dev_landingzone.qualibot.chat_trace_scores`, jointe à Lakebase par `vsi_trace_id`.
L'ancien `score_production_qa.py` et ses jobs DEV / UAT sont archivés (`archive/evaluation/`). La
tâche d'import du job d'export UAT (`import_chatbot_tables_to_uat_job.py`) charge toujours
`chat_turns`, `chat_retrieved_chunks` et `errors` dans `uat_landingzone.qualibot`, pour les
tableaux de bord.

## Vérifié

- Tests : `tests/test_lakebase_logging.py` (chaque clé écrite par le moteur est bien une colonne,
  forme des INSERT, audits impact / analyse), `tests/test_chat_vsi.py` (journal d'un tour : propre,
  recherche partielle, reranker refusé, reformulation en échec, recherche en panne, modèle de
  secours, plafond), `tests/test_chat_route.py` (tour enregistré une fois, en erreur, interrompu).
- Sur un PostgreSQL 16 local : schéma d'avant ce changement créé, puis le nouveau appliqué deux fois
  (montée de version + idempotence), puis écriture et relecture d'un tour, de ses passages, de ses
  erreurs, d'une analyse et d'une impact search.

Pas instrumenté : `/compare/summarize` (sa durée est dans `summary_cache.result_json`), les
impact searches servies depuis le cache (aucune ligne `impact_requests`, comme avant).
