# Plan: Intégration de VSI dans l'app à la place du KA

> Status: DONE (2026-10-06), étapes 1 à 6 prouvées. Détail et preuves : `docs/dev-journal.md`, `docs/evidence/`.

- **Branche :** `ka_to_vsi` (locale).
- **Remplace :** `docs/plan/chat-vsi-ka-replacement.md` (trop lourd, voir `docs/dev-conception.md`).
- **Principe :** chaque étape s'appuie sur des prérequis vérifiés, et on ne passe à la suivante qu'après la preuve de fin.
- **Point de départ :** le VSI v0 mesuré au niveau du KA sur le golden de Jules (`dev_landingzone.qualibot.qualibot_eval_golden`, 21 cas) :
  - Correctness : KA 13/21, VSI 15/21 ;
  - Guidelines : KA 20/21, VSI 19/21 ;
  - documents attendus retrouvés : KA 59 %, VSI 63 %.

Légende : ✅ vérifié · ❌ manquant · ⏳ en attente · ⚠️ non vérifiable maintenant.
Prérequis vérifiés le 2026-10-06, en lecture seule.

## Étape 1 — Instructions des 3 divisions dans le repo

**Action :** créer `server/config/chat_vsi/instructions_{all,as,is}.md` à partir des instructions des KA en service, sans la note parasite de AS.

**Prérequis :**
- ✅ Instructions lisibles en direct : ALL 6 429, AS 6 724, IS 6 212 caractères.
- ✅ Note parasite (« ▎ Note : j'ai intégré l'ordre de citation QP→MI… ») présente dans AS uniquement.

**Preuve de fin :** fichiers identiques aux instructions en service, note mise à part (diff).

## Étape 2 — Module `server/services/chat_vsi.py`

**Action :** porter le `vsi_predict` mesuré (`scripts/qualibot_golden_ka_vs_vsi.py:130`) en `stream_chat_vsi(host, token, division, messages)`. Il émet le contrat de `stream_chat()` :
- deltas `response.output_text.delta` ;
- `sources` + `citations` ;
- `metadata` ;
- `error` ;
- `[DONE]`.

**Parsing de la réponse en streaming** (partie de l'étape 2) :
- **Pourquoi :** le `vsi_predict` mesuré analyse les marqueurs `[n]` une fois la réponse complète, sans streaming. Dans l'app, la réponse arrive en flux et le front affiche les deltas bruts au fur et à mesure (contrat D2). Les deltas émis ne doivent donc contenir aucun marqueur.
- **Action :** un parseur en streaming des marqueurs `[n]`, qui :
  - garde en tampon un `[` incomplet tant qu'on ne sait pas si c'est un marqueur ;
  - émet des deltas sans marqueurs ;
  - calcule `citations {n, pos}` au fil de l'eau (`pos` = longueur du texte propre déjà émis) ;
  - mappe chaque `n` de document vers `{title: REF, url}`, numéroté par ordre de première apparition.
- **Aval inchangé :** `chat_ws` applique déjà `_apply_citation_markers`, `augment_sources` et `_number_sources`, et le front affiche `⟦n⟧` et les pastilles (vérifié dans le code).

**Réglages VSI** (partie de l'étape 2) :
- **Action :** variables lues par le module, avec en valeur par défaut ce qui a été mesuré dans le v0 :
  - `CHAT_VSI_ENABLED` ;
  - `CHAT_VSI_INDEX_ALL` / `_AS` / `_IS` = `dev_landingzone.qualibot.chunks_index_v1` / `chunks_as_index_v1` / `chunks_is_index_v1` ;
  - `CHAT_VSI_LLM_ENDPOINT` = `databricks-claude-sonnet-4-6` ;
  - `CHAT_VSI_NUM_RESULTS` = 10.
- **Où :** dans `app.yaml` (déployé) et dans `.env.local` (local).
- **Test :** test unitaire de la correspondance division → index (AS, IS et ALL ; division inconnue → ALL). Un index non configuré produit un événement `error` explicite, pas une exception.

**Prérequis :**
- ✅ `vector_search._fetch_chunks(host, token, index_name, query_text, num_results, max_query_chars)` est réutilisable. Ses colonnes incluent `chunk_id`, `REF`, `url`, `chunk_text`.
- ✅ `streaming.stream_analysis(...)` stream `databricks-claude-sonnet-4-6` et émet déjà `{"type": "response.output_text.delta"}`, le même type que le contrat. Ses événements usage / warning / error / `[DONE]` sont à filtrer ou relayer.
- ✅ Index AS et IS interrogeables (testés sous mon identité).
- ✅ Accès du SP de l'app au LLM : vérifié à l'étape 5 (le LLM répond dans l'app déployée).

**Preuve de fin :**
- Tests unitaires du parseur :
  - marqueur `[n]` coupé entre deux deltas ;
  - marqueurs groupés `[2][5]` ;
  - numéro inconnu ;
  - `[` qui n'est pas un marqueur (lien Markdown, texte) ;
  - aucune citation.
- Équivalence : sur les réponses du golden, le parseur en streaming donne exactement le même texte et les mêmes citations que l'analyse après coup du v0.
- `metadata` : un seul événement `metadata` par tour, émis avant `[DONE]`, avec un `trace_id` non vide (test unitaire).
- `error` (tests unitaires, Vector Search et LLM simulés) :
  - Vector Search en erreur (HTTP 4xx/5xx, timeout) → un événement `error` puis `[DONE]`, sans exception remontée ;
  - LLM en erreur avant ou pendant le flux → un événement `error` puis `[DONE]` ;
  - dans les deux cas, aucun delta partiel avec des marqueurs.
- Golden rejoué avec le module de la branche, synchronisé sur le workspace (sans déploiement) : métriques ≥ VSI v0. Ça prouve que le portage ne dégrade pas le v0.

## Étape 3 — Choix du moteur dans `chat.py`

**Action :**
- même handler WebSocket, qui appelle `stream_chat` (KA) ou `stream_chat_vsi` (VSI) selon le moteur ;
- nouvelle route `/api/chat-vsi/ws` ;
- les tours VSI sont enregistrés avec `endpoint_name = vsi-<division>`. Ils restent ainsi distinguables des tours KA dans la base Lakebase de l'app dev, qui est partagée.

**Prérequis :**
- ✅ Point d'injection unique : `chat.py:644` (appel à `stream_chat`).
- ✅ `tests/test_chat.py` existe ; pytest 9.0.2 installé.
- ✅ Base de référence (2026-10-06, venv Python 3.11) : **114 passés, 1 ignoré, 2 en échec**. Les 2 échecs sont dans `tests/test_deploy_config.py`, qui lit `utils/deploy/target_env.json`, absent de la copie locale. Ils sont préexistants et sans lien avec le chat. `tests/test_chat.py` passe.

**Preuve de fin :**
- Tests existants : toujours 114 passés. Seuls restent les 2 échecs préexistants de `test_deploy_config.py`.
- Nouveaux tests, sur le modèle de `tests/test_chat.py` (moteurs simulés) :
  - `/api/chat/ws` appelle `stream_chat`, jamais `stream_chat_vsi` ;
  - `/api/chat-vsi/ws` appelle `stream_chat_vsi` avec la bonne division ;
  - sur la route VSI, le `done` contient le texte avec `⟦n⟧` et les sources numérotées : même post-traitement que le KA ;
  - la route VSI applique le même contrôle d'accès (`can_chat`) que la route KA ;
  - `CHAT_VSI_ENABLED=false` → un événement `error` et la fermeture du WebSocket ;
  - un `error` du moteur VSI est relayé au navigateur et le tour est enregistré en `status=error` ;
  - le tour VSI est enregistré avec `endpoint_name = vsi-<division>`.

## Étape 4 — Front

**Action :** `ChatView` reçoit une prop `engine` qui choisit le chemin WebSocket, aujourd'hui en dur (`ChatView.tsx:30`). `ChatVsiPage` affiche `ChatView engine="vsi"`, à la place du placeholder.

**Prérequis :**
- ✅ Chemin WebSocket en dur localisé ; placeholder `ChatVsiPage` existant.
- ✅ Build réussi deux fois via le proxy npm cloud (`npm-proxy.cloud.databricks.com`) ; `tsc` et `vite` installés.

**Test de bout en bout en local** (partie de l'étape 4), selon la procédure « Local development » du `README.md` : backend `uvicorn` sur :8000 + front Vite sur :3000.

**Prérequis du test local** (vérifiés le 2026-10-06) :
- ✅ Venv `.venv` (Python 3.11, gitignoré) avec `requirements.txt` + `fastapi`, `uvicorn`, `python-dotenv`, `pytest`, `mlflow[databricks]`. **Deux dépendances absentes de `requirements.txt` étaient requises en local :**
  - `python-multipart` (FastAPI, formulaires de Compare) ;
  - `websockets` (uvicorn, routes WebSocket du chat).
  `server.app` s'importe sans erreur.
- ✅ `fasttext-wheel` installé sans problème sous Python 3.11.
- ✅ `ws: true` ajouté au proxy `/api` de `client/vite.config.ts`.
- ✅ `.env.local` créé (gitignoré) : profil `latecoere`, KA dev, `LAKEBASE_PROJECT_ID` vide (pas de Lakebase en local, le projet partagé n'est pas touché). Les réglages VSI y seront ajoutés à l'étape 2 (voir « Réglages VSI »).
- ✅ **Lancement : charger `.env.local` dans l'environnement avant uvicorn** (`set -a; . ./.env.local; set +a`). `server/app.py` importe les routers (l.20) avant `load_dotenv` (l.34), et `chat.py` lit `CHAT_*` à l'import : sans ça, le chat local répond « CHAT_ENDPOINT not configured » (vérifié). Pas d'impact en production.
- ✅ **Vérifié de bout en bout (2026-10-06)** : Chat KA en local à travers le proxy Vite, en WebSocket. Réponse complète : 196 deltas, 6 citations, sources QP-1518, Q0451MQ, Q0197QP_FR…
- ✅ Lakebase injoignable en local : sans effet bloquant. Timeout de 5 s, puis fonctionnement sans historique (`README.md`).

**Preuve de fin :**
- le build passe ;
- en local, une question par division (ALL, AS, IS) dans Chat VSI :
  - la réponse arrive en flux, sans marqueur `[n]` visible pendant le streaming ;
  - les `[n]` apparaissent en exposant à la fin ;
  - les pastilles de sources pointent vers les bons documents Intraqual ;
- en local, avec un index VSI volontairement invalide dans `.env.local` : Chat VSI affiche un message d'erreur, et non un « Thinking » infini ;
- Chat KA fonctionne toujours, sans régression.

## Étape 5 — Déploiement de `qualibot-custom`

**Décision (2026-10-06) :** `qualibot-custom` reste branché sur la base Lakebase de l'app dev de Jules (`doccompare`), puisque c'est un environnement de dev. Les tours VSI y sont identifiables par `endpoint_name = vsi-<division>` (étape 3).

**Action :**
1. `databricks sync`, puis `apps deploy`.
2. Démarrer l'app.

**Prérequis :**
- ✅ Rôle Lakebase `app-qualibot-custom-sp` présent ; logs du déploiement du 2026-10-05 12:17 : « Lakebase ready ».
- ✅ `sync` + `apps deploy` déjà réussis 4 fois.
- ✅ App arrêtée (compute `STOPPED`) ; j'en suis le créateur, je peux la démarrer.
- **Droits du SP `f4d62db4-bbbb-4fb5-882f-4543d7e17abc` :**
  - ✅ USE_SCHEMA sur `dev_landingzone.qualibot` ;
  - ✅ SELECT sur `chunks_index_v1` ;
  - ✅ SELECT sur `chunks_as_index_v1` et `chunks_is_index_v1` (posé et vérifié le 2026-10-06) ;
  - ✅ USE_CATALOG sur `dev_landingzone` : vérifié par le fonctionnement à l'étape 5 (les 3 index répondent dans l'app, appelés avec le token du SP). La lecture directe des droits reste impossible pour moi (pas de READ METADATA).

**Preuve de fin :**
- Chat VSI répond dans l'app sur les 3 divisions, sans erreur Vector Search ni LLM dans les logs. Ça confirme aussi l'accès du SP au LLM et USE_CATALOG.
- Les logs « chat turn done » des tours VSI affichent `endpoint=vsi-<division>`.
- Chat KA répond toujours dans l'app.

## Étape 6 — Non-régression face au KA

**Action :** rejouer le golden sur le code tel qu'il est déployé, après les étapes 3 à 5, comparé au KA, dans la même expérience MLflow. C'est le contrôle final. La différence avec l'étape 2 : le code évalué est celui qui tourne dans l'app, après les modifications de `chat.py` et du front.

**Prérequis :**
- ✅ Notebook `qualibot_golden_ka_vs_vsi` (workspace + `scripts/`) et expérience MLflow `qualibot-vsi-vs-ka` (id `197255780315823`) existants.
- ✅ L'import du code de l'app depuis `/Workspace/Shared/qualibot-custom` a fonctionné sur 2 runs.

**Preuve de fin :** métriques ≥ VSI v0 (15/21, 19/21, 63 %).

## Points bloquants à ce jour (mis à jour le 2026-10-06)

| Point | Responsable | Bloque |
|---|---|---|
| ~~USE_CATALOG sur `dev_landingzone` pour le SP de l'app~~ : en place, vérifié à l'étape 5 | Jules | — |
