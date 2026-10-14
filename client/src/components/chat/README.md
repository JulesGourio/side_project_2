# Chatbot

Assistant qui répond aux questions à partir de la base documentaire de
l'entreprise. Un index Vector Search fournit les passages pertinents ; le
modèle de langage GPT-6 Luna répond à partir de ces passages seulement, avec
des citations numérotées. Le chat sert trois périmètres : ALL, AS et IS (filtre
sur la colonne `division` de l'index, un seul index pour les trois).

Le code serveur est dans `server/services/chat/` (moteur, appel du modèle,
journal du tour) et `server/routers/chat/` (route WebSocket, sessions,
persistance). L'interface est dans ce dossier et dans les pages
`client/src/pages/ChatPage.tsx` et `SharedChatPage.tsx`. Le choix de chaque
réglage et les mesures qui le justifient sont dans
[`docs/chat_tests.md`](../../../../docs/chat_tests.md).

## Déroulement d'un tour

Une question arrive sur la route WebSocket `/api/chat/ws` avec l'historique, la
division et l'identifiant de session. Le tour se déroule ainsi :

1. **Historique** : `_trim_history` garde les `CHAT_MAX_HISTORY` derniers
   messages (10 par défaut). La question en cours est toujours conservée.
2. **Date** : `_with_today_date` ajoute `[Date: AAAA-MM-JJ]` en tête du dernier
   message utilisateur, dans une copie. Le modèle peut ainsi situer les dates
   des documents par rapport à aujourd'hui.
3. **Requêtes de recherche** (`engine/queries.py`) :
   - par défaut, la question est réécrite en une requête française et une
     requête anglaise (sigles et acronymes développés quand ils sont certains),
     par le modèle `CHAT_REWRITE_ENDPOINT`. Les noms de société et les mots de
     type de document que la conversation n'utilise pas sont retirés de la
     réécriture (`strip_added_noise`) ;
   - si `CHAT_TRANSLATE_ONLY` est activé, la première question d'une
     conversation en français ou en anglais n'est pas réécrite : elle est
     seulement traduite dans l'autre langue. Les suites de conversation et les
     autres langues gardent la réécriture.
4. **Recherche** (`engine/search.py`) : la question telle quelle, puis les
   requêtes de réécriture, sont envoyées en recherche hybride à l'index. Chaque
   requête remonte les `CHAT_RERANK_TOP_K` premiers passages, reclassés par le
   reranker Databricks. La requête française reçoit en plus les
   `CHAT_RAW_TOP_K` premiers passages bruts (`CHAT_RAW_ON`). Tous les résultats
   sont fusionnés par meilleur rang, puis coupés à `CHAT_MAX_SEARCH_PASSAGES` si
   ce plafond est fixé (0 : pas de plafond). Chaque appel est une requête
   Vector Search avec ses réessais (`vs_gate.py`).
5. **Documents nommés** (`engine/retrieval.py`) : les REF cités dans la question
   ou dans les autres tours de la conversation (6 au plus) reçoivent une recherche filtrée sur
   leurs passages (8 passages au total). Ces passages sont classés en premier.
6. **Une langue par document** : seuls les passages de la variante linguistique
   la mieux classée d'un document sont gardés.
7. **Prompt** (`engine/answer.py`) : les instructions de la division
   (`server/prompts/chat/system/<division>.md`), la base commune
   (`system/base.md`), les documents numérotés `[n]` avec leur révision et leur
   date quand le catalogue les connaît, la question, puis une ligne qui nomme
   la langue de la réponse (quand la langue de la question est sûre).
8. **Réponse** (`chat/llm.py`) : streaming par GPT-6 Luna, GPT-5.6 Luna en
   secours (voir plus bas). Le modèle écrit des marqueurs `[n]`.
9. **Citations** (`engine/citations.py`) : `CitationStreamParser` retire les
   marqueurs du flux et garde leur position. À la fin de la réponse, le contenu
   enregistré reçoit les marqueurs `⟦n⟧` à leurs positions, et l'évènement
   `sources` liste les documents cités dans l'ordre d'apparition.
10. **Persistance et journal** : le tour est enregistré dans Lakebase (voir
    [Persistance](#persistance-et-journal)), même quand il échoue ou que le
    navigateur part en cours de réponse.

## Réponse et reprises (`chat/llm.py`)

| Situation | Comportement |
|---|---|
| Erreur sans texte reçu (429, 5xx, timeout, réponse vide, requête refusée, modèle absent) | Modèle suivant de la chaîne |
| Coupure après un début de réponse | Demande de reprise au même modèle ou au suivant, 2 fois au plus ; l'utilisateur voit une seule réponse |
| Premier texte attendu | Dans les 90 s (`CHAT_FIRST_TOKEN_TIMEOUT_S`) |
| Flux sans texte | Abandonné après 60 s (`CHAT_STALL_TIMEOUT_S`) |
| Nouvelle tentative | Interdite au-delà de 240 s (`CHAT_LLM_DEADLINE_S`) |
| Passes sur la chaîne | 3 (`CHAT_LLM_ROUNDS`), avec une attente de 2 s, 8 s puis 20 s, ou le `Retry-After` du modèle s'il est plus court |
| Modèle en erreur 429 ou 5xx | Mis en dernier pendant 30 s (`CHAT_COOLDOWN_S`) ; 10 min pour une erreur 404 |
| Réponses simultanées | 32 au plus par instance (`CHAT_MAX_CONCURRENT_ANSWERS`) ; les autres attendent, avec des signaux de vie |

Quand tout a échoué, l'utilisateur voit un seul message, `TIRED_MESSAGE`
(même texte côté serveur et dans `chatStream.ts`). La cause technique va dans
la table `errors`, avec son origine dans le code.

La réécriture a sa propre chaîne de secours (`CHAT_REWRITE_*`). Si toutes les
réécritures échouent, la recherche se fait avec la question seule.

## Langue de la réponse

La langue de la question est devinée localement avec fastText
(`server/data/lid.176.ftz`, `translation_bridge._fast_lang_guess`). Si la
devinette est sûre, le rappel de langue nomme cette langue. Sinon, le modèle
choisit la langue de la question sans consigne nommée.

Le pont de traduction (`CHAT_TRANSLATE_BRIDGE_ENABLED`) traduit les questions
écrites dans une autre langue que le français et l'anglais, puis traduit la
réponse. Il est désactivé dans `app.yaml` : les questions sont traitées telles
quelles.

## Citations et documents

- Chaque passage commence par `[Source: REF | Title: … | …]`. Les titres affichés
  viennent de ce préfixe.
- Le catalogue des documents (table Lakebase `doc_catalog` : REF, titre, lien,
  division, révision, date de publication) est reconstruit par la tâche
  `6_update_kb_metadata` de la chaîne de parsing, puis rechargé par l'application
  toutes les 30 minutes (`DOC_CATALOG_REFRESH_S`). Il sert à trouver les REF
  nommés, à afficher les titres et les liens des sources, et à donner au modèle
  la révision et la date de chaque document.
- Tant que la table est vide (premier démarrage, tests), le fichier
  `server/data/doc_catalog.json` sert de repli. C'est un instantané figé, sans
  mise à jour.
- Les documents antérieurs à 2018 ne sont pas dans l'index du chat : ils sont
  dans la table d'archive de la chaîne de parsing (voir son README).

## Sessions, partage et feedback

Routes (`server/routers/chat/sessions.py`, `feedback`) :

| Route | Rôle |
|---|---|
| `WS /api/chat/ws` | Un tour de chat (seul canal de réponse : le WebSocket évite le tampon du proxy des applications Databricks) |
| `POST /api/chat/feedback` | Pouce haut ou bas, commentaire facultatif, sur un message |
| `GET /api/chat/sessions` | Liste des conversations de l'utilisateur |
| `GET /api/chat/sessions/{id}` | Une conversation, si elle appartient à l'utilisateur (404 sinon) |
| `DELETE /api/chat/sessions/{id}` | Suppression logique (`deleted`, `deleted_at`) |
| `POST /api/chat/sessions/{id}/share` | Crée le jeton de partage, ou renvoie le jeton existant |
| `GET /api/chat/shared/{token}` | Lecture seule, pour tout utilisateur qui a le lien |
| `POST /api/chat/shared/{token}/duplicate` | Copie la conversation partagée dans une conversation de l'utilisateur |

Un lien partagé est vivant : le lecteur voit les messages actuels de la
conversation. Il n'y a pas d'expiration ni de révocation. La vue partagée
ne permet pas de voter. Dupliquer crée une conversation neuve à
l'utilisateur ; la conversation d'origine n'est jamais modifiée.

## Accès

Les routes HTTP du chat exigent `require_chat` ; la route WebSocket vérifie
la même condition et renvoie `Chat access not granted` sans ouvrir le tour.
L'accès est vérifié en direct : l'application demande à Databricks, au nom du
visiteur (jeton OBO) et par l'entrepôt `CAPS_WAREHOUSE_ID`, s'il appartient à un
des groupes de compte autorisés au chat (ChatBot, CoreDev, CoreAdmin). Le résultat
est gardé `CAPS_TTL_S` secondes (600 par défaut). Sans jeton, sans entrepôt ou si la
vérification échoue, l'accès est refusé, et l'échec n'est pas gardé en cache.
`CAPS_BYPASS=true` ouvre le chat à tous : ce réglage n'est activé que sur la cible
DEV.

## Persistance et journal

Tables Lakebase (`server/services/infra/lakebase/schema.py`, colonnes dans
[`docs/lakebase_schema.md`](../../../../docs/lakebase_schema.md)) :

| Table | Contenu |
|---|---|
| `chat_sessions` | Une ligne par conversation : nom, jeton de partage, suppression logique |
| `chat_messages` | Une ligne par message (`user` et `assistant`) : contenu, `sources_json` (titre, révision, date, citations), `status`, `division`, `trace_id` |
| `chat_feedbacks` | Votes et commentaires sur les messages |
| `chat_turns` | Une ligne par tour : chaque étape, sa durée, la configuration prévue, le modèle qui a répondu, `status` et les codes d'avertissement |
| `chat_retrieved_chunks` | Une ligne par passage retrouvé : son texte, sa place dans le prompt, si le modèle l'a cité, et la raison de son écart (`other_language`, `over_cap`) |
| `errors` | Une ligne par avertissement ou erreur : `stage`, `origin` (fichier, ligne, fonction), `fingerprint`, `user_message` |
| `doc_catalog` | Catalogue des documents (voir plus haut) |
| `knowledge_base_metadata` | Date des documents affichée dans l'interface (`GET /api/config/knowledge-base-date`) |
| `users` | Identité des utilisateurs ayant écrit un tour de chat |

`status` de `chat_turns` : `ok` (réponse, tout tourné comme prévu),
`degraded` (réponse, mais au moins une étape a échoué ou a été dégradée :
réécriture, recherche partielle, modèle de secours, réponse coupée), `error`
(pas de réponse), `aborted` (le navigateur est parti en cours de réponse).

`trace_id` (`chat-…`) relie `chat_turns`, `chat_retrieved_chunks`, `errors` et
les deux lignes de `chat_messages` du tour.

Chaque tour est aussi reconstruit en une trace MLflow dans l'expérience
`CHAT_MLFLOW_EXPERIMENT` (vide : désactivé ; `services/chat/mlflow_turns.py`).
Les votes deviennent une évaluation `user_vote` sur cette trace. Le scoring
des traces est fait par des jobs, jamais pendant un tour.

## Interface (`client/src/components/chat/`)

| Fichier | Rôle |
|---|---|
| `ChatView.tsx` | Mise en page : liste des messages, barre latérale, bouton de partage, ouverture d'une conversation dupliquée (`?session=`) |
| `useChatConversation.ts` | Messages, tour en cours, chargement d'une session, partage |
| `useChatSessions.ts` | Liste des conversations |
| `chatStream.ts` | Connexion WebSocket, événements `delta`, `done`, `error`, `ping`, message d'erreur `TIRED_MESSAGE` |
| `ChatSidebar.tsx` | Conversations, « Nouvelle conversation », choix de la division |
| `ChatInput.tsx` | Zone de saisie et bouton d'arrêt |
| `ChatMessage.tsx` | Bulle de message, citations en exposant |
| `ChatSources.tsx` | Pastilles des sources, les variantes linguistiques d'un même document regroupées |
| `ChatFeedback.tsx` | Votes sur un message |
| `DownloadMenu.tsx`, `chatExport.ts` | Export Markdown et PDF de la conversation |
| `WelcomeState.tsx` | Écran d'accueil |
| `division.tsx` | Sélecteur ALL / AS / IS |
| `message.ts` | Types partagés |

`ChatPage.tsx` affiche le chat quand `CHAT_ENABLED` est vrai et que
l'utilisateur y a accès. `SharedChatPage.tsx` (route `/chat/shared/:token`)
réutilise `ChatMessage` sans votes.

## Configuration

Valeurs de `app.yaml`, surchargées par cible dans `utils/deploy/target_env.json`.

| Variable | Valeur dans `app.yaml` | Rôle |
|---|---|---|
| `CHAT_ENABLED` | `true` | Active le chat |
| `CHAT_INDEX` | `dev_proj.qualibot.chunks_index` | Index interrogé (UAT : `uat_proj.qualibot.chunks_index`) |
| `CHAT_LLM_ENDPOINT` | `databricks-gpt-6-luna` | Modèle de réponse |
| `CHAT_LLM_FALLBACK_ENDPOINTS` | `databricks-gpt-5-6-luna` | Modèles de secours, séparés par des virgules |
| `CHAT_REWRITE_ENDPOINT` | `databricks-gpt-6-luna` | Modèle de réécriture et de traduction des requêtes |
| `CHAT_REWRITE_FALLBACK_ENDPOINTS` | `databricks-gpt-5-6-luna` | Secours de la réécriture |
| `CHAT_REWRITE_REASONING_EFFORT` | `low` | Effort de raisonnement de la réécriture (vide : celui du modèle) |
| `CHAT_REWRITE_MAX_TOKENS` / `CHAT_ANSWER_MAX_TOKENS` | `2000` / `8000` | Plafonds de sortie (le raisonnement compte dedans) |
| `CHAT_TRANSLATE_ONLY` | `off` | Première question traduite au lieu d'être réécrite |
| `CHAT_PAIR_RERANK_TOP_K` / `CHAT_PAIR_RAW_TOP_K` | `16` / `16` | Tailles de recherche en mode traduction |
| `CHAT_TRANSLATE_BRIDGE_ENABLED` | `false` | Pont de traduction (langues autres que FR et EN) |
| `CHAT_TRANSLATE_ENDPOINT` | `databricks-gpt-5-6-luna` | Modèle du pont de traduction |
| `CHAT_MAX_HISTORY` | `10` | Messages d'historique renvoyés au modèle (0 : sans limite) |
| `CHAT_MLFLOW_EXPERIMENT` | vide | Expérience MLflow des traces (vide : désactivé) |
| `CAPS_TTL_S` | `600` | Durée du cache des droits d'accès |
| `CAPS_WAREHOUSE_ID` | vide, renseigné par cible | Entrepôt SQL de la vérification des groupes |
| `CAPS_BYPASS` | `false` | Ouvre le chat à tous (DEV seulement) |

Réglages lus par le moteur sans être dans `app.yaml` (valeurs du code) :
`CHAT_RERANK_TOP_K` (12), `CHAT_RAW_TOP_K` (12), `CHAT_RAW_ON` (`fr`),
`CHAT_MAX_SEARCH_PASSAGES` (0), `CHAT_MAX_CONCURRENT_ANSWERS` (32),
`CHAT_FIRST_TOKEN_TIMEOUT_S` (90), `CHAT_STALL_TIMEOUT_S` (60),
`CHAT_LLM_DEADLINE_S` (240), `CHAT_LLM_ROUNDS` (3), `CHAT_COOLDOWN_S` (30),
`VS_MAX_CONCURRENT_QUERIES` (8), `VS_QUERY_RETRIES` (5),
`DOC_CATALOG_REFRESH_S` (1800).

Par cible (`utils/deploy/target_env.json`) :

| Cible | Surcharges de chat |
|---|---|
| `dev` | `CHAT_INDEX` = `dev_proj.qualibot.chunks_index` ; réécriture sur `databricks-gemini-3-1-flash-lite` ; `CHAT_TRANSLATE_ONLY` = `on` ; `CHAT_MLFLOW_EXPERIMENT` = `/Shared/qualibot/chat_turns` ; `CAPS_BYPASS` = `true` |
| `uat` | `CHAT_INDEX` = `uat_proj.qualibot.chunks_index` |
| `uat-test` | `CHAT_INDEX` = `uat_proj.qualibot.chunks_index_test` ; réécriture sur `databricks-gemini-3-1-flash-lite` ; `CHAT_TRANSLATE_ONLY` = `on` ; `CHAT_MLFLOW_EXPERIMENT` = `/Shared/qualibot/chat_turns_test` |
| `prod` | `CHAT_INDEX` = `prod_proj.qualibot.chunks_index` |

## Base documentaire

La réponse ne vient que de l'index `chunks_index`, alimenté par la table
`chunks` de la chaîne de parsing (tâche `5_sync_index`, index en mode
TRIGGERED, modèle d'embedding `databricks-qwen3-embedding-0-6b`). Le découpage,
les descriptions d'images et la construction de l'index sont décrits dans
[`utils/parsing_pipeline/README.md`](../../../../utils/parsing_pipeline/README.md).

## Tests et évaluation

Tests (`tests/`) : `test_chat_route.py` (un tour WebSocket, moteur simulé),
`test_chat_engine.py` (moteur : recherche, filtres, prompt, erreurs,
citations), `test_chat_llm.py` (secours, reprises, continuation, file
d'attente), `test_chat.py` (sessions, partage, feedback, droits d'accès),
`test_citation_markers.py`, `test_lakebase_logging.py` (colonnes du journal),
`test_mlflow_turns.py`, `test_chat_scorers.py`, `test_doc_catalog.py`,
`test_register_chat_prompts.py`, `test_prompt_noise.py`,
`test_streaming_temperature.py`.

```
python -m pytest tests/test_chat*.py tests/test_citation_markers.py tests/test_doc_catalog.py
```

Évaluation (`utils/evaluation/`) : `retrieval_eval.py` (qualité de la recherche
seule), `pairwise_answers.py` (comparaison de réponses),
`load_test_chat.py` et `load_test_vector_search.py` (charge),
`chat_scorers.py` et `score_chat_traces.py` (scores des traces),
`register_chat_prompts.py` (prompts dans le registre MLflow), et
`create_chat_experiment.py`. Les résultats et les choix de configuration sont
dans [`docs/chat_tests.md`](../../../../docs/chat_tests.md).

## Prompts

Les prompts sont des fichiers dans `server/prompts/chat/` (`system/base.md`,
`system/all.md`, `system/as.md`, `system/is.md`, `rewrite.md`,
`translate_query.md`, `language_reminder*.md`), lus par `chat/prompts.py`.
Un prompt n'est jamais une chaîne Python. Après une modification, la tâche
d'enregistrement des prompts doit être relancée pour que les traces MLflow
pointent sur la bonne version (voir [operations_dev.md](../../../../operations_dev.md)).
