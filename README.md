# QualiBOT

Application FastAPI + React déployée sur Databricks Apps. Elle donne accès à une
base commune de documents qualité et techniques (aéronautique). Deux
fonctionnalités, une seule application, un seul déploiement :

| Onglet | Ce qu'il fait | Documentation |
|---|---|---|
| **Comparaison** (`/compare`) | Compare deux versions d'un document, produit un résumé ou un tableau des changements, puis cherche dans la base les autres documents que ces changements touchent | [`client/src/components/compare/README.md`](client/src/components/compare/README.md) |
| **Chat** (`/chat`) | Assistant qui répond à partir de la base documentaire, avec des citations numérotées et un filtre par division (ALL, AS, IS) | [`client/src/components/chat/README.md`](client/src/components/chat/README.md) |

La base documentaire est construite chaque nuit par la chaîne de parsing
([`utils/parsing_pipeline/README.md`](utils/parsing_pipeline/README.md)). Les
deux fonctionnalités l'interrogent par le même index Vector Search.

## Modèles et index

| Étape | Modèle ou index | Réglage |
|---|---|---|
| Comparaison : analyse (résumé et tableau des changements) | Modèle configuré dans `app.yaml` | `COMPARE_ANALYSIS_ENDPOINT` |
| Comparaison : recherche d'impact | Index Vector Search | `COMPARE_IMPACT_INDEX` |
| Comparaison : jugement de chaque document candidat | `databricks-gpt-5-6-luna` | `COMPARE_IMPACT_ENDPOINT` |
| Comparaison : résumé d'un document (texte, image) | `databricks-gpt-5-6-luna` | `COMPARE_SUMMARY_ENDPOINT`, `COMPARE_SUMMARY_IMAGE_ENDPOINT` |
| Chat : réécriture des requêtes et réponse | `databricks-gpt-6-luna`, secours `databricks-gpt-5-6-luna` | `CHAT_REWRITE_ENDPOINT`, `CHAT_LLM_ENDPOINT` et leurs secours |
| Chat : pont de traduction (désactivé) | `databricks-gpt-5-6-luna` | `CHAT_TRANSLATE_ENDPOINT` |
| Base : description des images (vision) | `databricks-gpt-5-6-luna` | `PARSING_LLM_ENDPOINT` |
| Base : embeddings des passages | `databricks-qwen3-embedding-0-6b` (index géré par Databricks) | `embedding_model` (tâche `5_sync_index`) |

Les valeurs exactes par cible sont dans `app.yaml` et dans
`utils/deploy/target_env.json`. Sur les cibles DEV et `uat-test`, la réécriture
du chat utilise `databricks-gemini-3-1-flash-lite`.

## Architecture

```
server/
  app.py                  application FastAPI, routes sous /api, sert client/out
  routers/                chat/, compare/, config.py, exports.py, feedback.py,
                          health.py, history.py, preview.py
  services/
    chat/                 engine/ (moteur du chat), llm.py (appels au modèle),
                          prompts.py, turn_log.py (journal d'un tour),
                          mlflow_turns.py (traces), translation_bridge.py
    compare/              processors/ (un par type de fichier, moteurs de diff),
                          chunked_analysis.py, impact_queries.py, summarize.py,
                          conversion.py, soffice.py, export_helpers.py
    infra/                lakebase/ (connexion, schéma, journal, caches),
                          user.py (droits), credentials.py, vector_search.py,
                          vs_gate.py (appels Vector Search), doc_catalog.py,
                          streaming.py, pdf_report/, error_origin.py
  prompts/chat/           les prompts du chat, en fichiers
  config/processors.yml   version du processeur par type de fichier
  data/                   doc_catalog.json (repli), lid.176.ftz (langue)
  assets/fonts/           police Inter, pour les PDF
client/src/
  App.tsx                 routes ; pages/ (ComparePage, ChatPage, SharedChatPage)
  components/chat/        interface du chat
  components/compare/     interface de la comparaison
  components/layout/      barre supérieure
  components/shared/      composants communs
  lib/                    configuration, export PDF
utils/                    outils hors application (voir utils/README.md)
```

Au démarrage, l'application ouvre la connexion à Lakebase et charge le catalogue
des documents, rechargé toutes les 30 minutes. Si Lakebase est injoignable,
l'application fonctionne sans historique.

## Accès

L'identité du visiteur est fournie par Databricks Apps (en-tête
`x-forwarded-user`). Les droits se vérifient en direct, sans table intermédiaire :
l'application demande à Databricks, au nom du visiteur (jeton OBO), s'il
appartient à l'un des groupes de compte autorisés :

| Fonctionnalité | Groupes autorisés |
|---|---|
| Comparaison | `Role-Project-LEAP-End-users-Qualibot-DocCompare`, `Role-Project-LEAP-CoreDev`, `Role-Project-LEAP-CoreAdmin` |
| Chat | `Role-Project-LEAP-End-users-Qualibot-ChatBot`, `Role-Project-LEAP-CoreDev`, `Role-Project-LEAP-CoreAdmin` |

La vérification passe par un entrepôt SQL (`CAPS_WAREHOUSE_ID`, renseigné par
cible dans `utils/deploy/target_env.json`). Le résultat est gardé
`CAPS_TTL_S` secondes (600 par défaut). Sans jeton, sans entrepôt ou si la
vérification échoue, l'accès est refusé, et l'échec n'est pas gardé en cache.
`CAPS_BYPASS=true` ouvre les deux fonctionnalités à tous : ce réglage n'est
activé que sur la cible DEV. En local (`ENV=development`), une requête sans en-tête
d'identité est acceptée.

## Configuration

Trois sources, dans cet ordre de priorité :

1. `utils/deploy/target_env.json` : surcharges par cible. Le script de
   déploiement et le pipeline Bitbucket les écrivent dans `target_config.env`,
   que `start.sh` charge au démarrage de l'application.
2. `app.yaml` : valeur par défaut de chaque variable, avec un commentaire sur son
   rôle. Les variables du chat, de la comparaison, de Lakebase et des droits y
   sont.
3. Le code : valeur de repli quand la variable est absente.

Le fichier `app.yaml` fait foi pour ce qui est déployé. Les valeurs par défaut du
code peuvent différer, et les READMEs des fonctionnalités indiquent les deux.

## Lancer en local

L'application locale appelle les vrais points d'accès Databricks sous votre
identité : aucun déploiement n'est nécessaire.

1. Créer un fichier `.env.local` à la racine (ignoré par git). Y mettre
   `ENV=development`, `DATABRICKS_CONFIG_PROFILE` (profil CLI de l'environnement
   visé), `COMPARE_ENABLED=true`, `CHAT_ENABLED=true`, et les variables
   `CHAT_INDEX`, `COMPARE_IMPACT_INDEX`, `LAKEBASE_PROJECT_ID`, `LAKEBASE_DATABASE`
   avec les valeurs de la cible `uat-test` de `utils/deploy/target_env.json`.
   `LAKEBASE_PROJECT_ID` se renseigne même en local : si le port 5432 est
   fermé, la connexion échoue vite (`LAKEBASE_CONNECT_TIMEOUT_S`, 5 s) et
   l'application fonctionne sans historique.
2. Retirer `DATABRICKS_TOKEN` de l'environnement s'il est défini : il est prioritaire
   sur le profil CLI. Sous PowerShell : `Remove-Item Env:\DATABRICKS_TOKEN`.
3. Lancer le backend et le frontend, chacun dans son terminal :

   ```
   python -m uvicorn server.app:app --host 127.0.0.1 --port 8000 --reload
   cd client
   bun install
   bun run dev
   ```

   Le serveur de développement écoute sur `http://localhost:3000` et transmet
   `/api` au port 8000. Ouvrir `/compare` ou `/chat`. Au démarrage, le backend
   affiche `Starting in development mode` si `.env.local` est chargé.

Dépendances Python : `pip install -r requirements.txt`. Pour les tests :
`pip install pytest pytest-asyncio`.

## Déploiement

| Environnement (`-AppEnv`) | Cible Databricks | Application | Profil CLI | Workspace |
|---|---|---|---|---|
| `dev` | `dev` | `qualibot` | `DEV` | `dbc-c623749d-731b` |
| `uat` | `qualibot-uat` | `qualibot` | `UAT` | `dbc-3a17bfce-9e88` |
| `uat-test` | `qualibot-uat-test` | `qualibot-uat-test` | `UAT` | `dbc-3a17bfce-9e88` |
| `prod` | `qualibot-prod` | `qualibot` | `qualibot-prod` | `dbc-77b87d5a-6bcb` |

Les hôtes complets sont dans `databricks.yml`. Le profil CLI de la cible doit
être authentifié : les étapes sont dans OPERATIONS.md et operations_dev.md.

Déploiement de l'application, à la racine du dépôt :

```powershell
.\utils\deploy\deploy_qualibot.ps1 -AppEnv dev
.\utils\deploy\deploy_qualibot.ps1 -AppEnv uat
.\utils\deploy\deploy_qualibot.ps1 -AppEnv uat-test
.\utils\deploy\deploy_qualibot.ps1 -AppEnv prod
```

Le script construit le frontend (`bun run build`), écrit `target_config.env`
à partir de `target_env.json`, synchronise le code vers le workspace, démarre
l'application si elle est arrêtée (`uat-test` se démarre à la main), puis la
redéploie. Options :

- `-SkipBuild` : pas de reconstruction du frontend (changement Python seul) ;
- `-SyncOnly` : synchronise le code sans redémarrer l'application ;
- `-Infra` : applique aussi le bundle (`databricks bundle deploy`, ressources,
  droits d'application et volumes). À utiliser quand les ressources ou les
  droits changent.

Pipeline Bitbucket (`bitbucket-pipelines.yml`) : un push sur `main` déploie
l'UAT. Les autres cibles se déploient à la main, par les pipelines « custom ».
PROD est déployé à la main, après l'UAT.

Les étapes à faire dans Databricks (authentification, premier déploiement, jobs
à lancer ou à activer, droits) sont dans [OPERATIONS.md](OPERATIONS.md) pour
l'UAT et la PROD, et dans [operations_dev.md](operations_dev.md) pour la DEV.

## Ressources par environnement

| Environnement | Tables (`<env>_proj.qualibot`) | Volume de l'application | Index du chat | Base Lakebase |
|---|---|---|---|---|
| DEV | `dev_proj.qualibot` | `dev_landingzone.qualibot.doc_compare` | `dev_proj.qualibot.chunks_index` | `doccompare` |
| UAT | `uat_proj.qualibot` | `uat_landingzone.qualibot.doc_compare` | `uat_proj.qualibot.chunks_index` | `doccompare` |
| UAT-TEST | `uat_proj.qualibot`, suffixe `_test` | `uat_landingzone.qualibot.test` | `uat_proj.qualibot.chunks_index_test` | `doccompare_test` |
| PROD | `prod_proj.qualibot` | `prod_landingzone.qualibot.doc_compare` | `prod_proj.qualibot.chunks_index` | `doccompare` |

Les tables Delta sont dans le schéma `<env>_proj.qualibot`. Les volumes sont dans
`<env>_landingzone.qualibot`. La base Lakebase est celle du projet `qualibot`.
La cible `uat-test` est un environnement de test isolé : elle ne touche ni aux
données ni aux index de l'UAT.

Tables de l'application dans Lakebase : `messages`, `feedbacks`,
`llm_requests`, `errors` (comparaison et journal commun), `impact_requests`,
`impact_document_results`, `impact_feedbacks`, `impact_cache`, `summary_cache`
(comparaison), `chat_sessions`, `chat_messages`, `chat_feedbacks`, `chat_turns`,
`chat_retrieved_chunks` (chat), `doc_catalog`, `knowledge_base_metadata`, `users`.
Le schéma est créé et complété par l'application au démarrage. Le détail des
colonnes est dans [docs/lakebase_schema.md](docs/lakebase_schema.md).

## Tests

```
python -m pytest
```

La suite est dans `tests/` (`testpaths` dans `pyproject.toml`). Les tests du chat,
de la comparaison et de la chaîne de parsing ont leur propre liste dans leur
README. Le frontend se vérifie par la compilation TypeScript et le build :

```
cd client
bun run build
bun run lint
```

## Maintenance

- Copier les tables Lakebase de l'UAT vers DEV, à partir d'un poste :
  [`utils/lakebase_sync/README.md`](utils/lakebase_sync/README.md).
- Alertes SQL de suivi (chat, parsing, erreurs, jobs) :
  [`utils/alerts/README.md`](utils/alerts/README.md).
- Outils, conventions et carte du dossier `utils/` :
  [`utils/README.md`](utils/README.md).

## Documentation

| Document | Contenu |
|---|---|
| [client/src/components/chat/README.md](client/src/components/chat/README.md) | Chatbot : moteur, réponses, partage, journal, configuration |
| [client/src/components/compare/README.md](client/src/components/compare/README.md) | Comparaison et recherche d'impact |
| [utils/parsing_pipeline/README.md](utils/parsing_pipeline/README.md) | Chaîne de parsing et d'indexation |
| [utils/README.md](utils/README.md) | Carte de `utils/` |
| [utils/lakebase_sync/README.md](utils/lakebase_sync/README.md) | Export, import et mise à jour du schéma de Lakebase |
| [utils/alerts/README.md](utils/alerts/README.md) | Alertes SQL |
| [docs/chat_tests.md](docs/chat_tests.md) | Mesures et choix de configuration du chat |
| [docs/lakebase_schema.md](docs/lakebase_schema.md) | Colonnes du journal Lakebase |
| [docs/ROADMAP.md](docs/ROADMAP.md) | Pistes non réalisées |
| [OPERATIONS.md](OPERATIONS.md) | Étapes à réaliser dans Databricks (UAT, PROD) |
| [operations_dev.md](operations_dev.md) | Étapes à réaliser dans Databricks (DEV) |
| [RETEST_CLEANUP.md](RETEST_CLEANUP.md) | Ce qu'il faut relancer après un changement de chemins ou de notebooks |
