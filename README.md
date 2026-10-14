# QualiBOT

Application FastAPI + React déployée sur Databricks Apps. Elle donne accès à une
base commune de documents qualité et techniques (aéronautique) et réunit trois
fonctions :

| Fonction | Ce qu'elle fait | Documentation |
|---|---|---|
| **Comparaison** (`/compare`) | Compare deux versions d'un document et produit un résumé ou un tableau des changements. | [`client/src/components/compare/README.md`](client/src/components/compare/README.md) |
| **Recherche d'impact** (onglet Comparaison) | Cherche dans la base les autres documents que les changements touchent, avec les passages concernés. | [`client/src/components/compare/README.md`](client/src/components/compare/README.md) |
| **Chat** (`/chat`) | Assistant qui répond à partir de la base documentaire, avec des citations numérotées et un filtre par division (ALL, AS, IS). | [`client/src/components/chat/README.md`](client/src/components/chat/README.md) |

La base documentaire est construite chaque nuit par la chaîne de parsing
([`utils/parsing_pipeline/README.md`](utils/parsing_pipeline/README.md)). Le chat
et la recherche d'impact l'interrogent par le même index Vector Search.

## Organisation du dépôt

### Application

- `server/` : backend FastAPI. `app.py` est le point d'entrée et sert le frontend
  construit ; `routers/` expose les routes sous `/api` (chat, comparaison,
  configuration, exports, retours, santé, historique, aperçu) ; `services/`
  contient le moteur du chat (`chat/`), les processeurs et analyses de comparaison
  (`compare/`) et les accès partagés à Lakebase, aux droits, à Vector Search et au
  catalogue (`infra/`) ; `prompts/chat/` contient les prompts du chat, en fichiers ;
  `assets/fonts/` contient la police Inter des exports PDF ; `tracing.py` configure le
  suivi MLflow des traces du chat.
- `client/` : frontend React et TypeScript (Vite). `src/` contient les pages et les
  composants (`chat/`, `compare/`, `layout/`, `shared/`, `lib/`) ; `out/` est le
  build servi par le backend.
- `start.sh` : démarrage de l'application. Il charge `target_config.env` s'il existe.
- `app.yaml` : variables d'environnement de l'application, avec leurs valeurs par
  défaut et leur rôle.

### Déploiement et exploitation

- `databricks.yml` : bundle `qualibot`, cibles `dev`, `qualibot-uat` (cible par
  défaut), `qualibot-uat-test` et `qualibot-prod`, variables et exclusions de
  synchronisation.
- `resources/` : jobs et application déclarés par cible, un fichier par sujet.
  `parsing_pipeline.job.yml` (chaîne quotidienne de parsing et d'indexation),
  `chat_mlflow.yml` (traces et prompts du chat), `dev_copy.yml` (copie des données
  UAT vers DEV), `grants.yml` (droits), `lakebase_sync.yml` (export et import
  Lakebase), `app_schedules.yml` (arrêt et démarrage de l'application).
- `utils/` : outils hors application (déploiement, parsing, évaluation, droits,
  Lakebase, alertes). La carte du dossier est dans [`utils/README.md`](utils/README.md).
- `runbooks/` : étapes à réaliser à la main dans Databricks. `uat.md`, `prod.md` et
  `dev.md` ; l'index est dans [`runbooks/README.md`](runbooks/README.md).
  `retest.md` liste ce qu'il faut relancer après un changement de chemin ou de
  notebook. `scripts/` contient les scripts ponctuels, lancés à la main.
- `bitbucket-pipelines.yml` : déploiement par Bitbucket (DEV, UAT, PROD).
- `pyproject.toml` et `requirements.txt` : dépendances Python.
- Fichiers de configuration racine : `.databricksignore` (exclusions de
  synchronisation), `.gitignore`, `.gitattributes`, `.gitleaksignore`, `.vscode/`.

### Données et tests

- `tests/` : suite pytest. `golden/` et `fixtures/` contiennent les sorties attendues
  et les réponses enregistrées ; `diff_corpus.py` et `prompt_noise.py` sont des jeux
  de données et des détecteurs utilisés par les tests.
- `data/` : `lid.176.ftz` (modèle fastText de détection de langue) et
  `vendor/antiword/` (binaire antiword, pour convertir les fichiers `.doc`).
- `leap_utils/` : copie de la bibliothèque partagée LEAP (écriture et fusion de
  tables, contrôles qualité). Ne pas la modifier dans ce dépôt.

### Documentation

- `docs/` : [`lakebase_schema.md`](docs/lakebase_schema.md), colonnes du journal
  Lakebase. C'est le seul document de référence de ce dossier.
- `archive/` : documentation remplacée (`archive/docs/`, `archive/readmes/`).
  Elle n'est jamais déployée.

Les dossiers `runbooks/`, `docs/` et `archive/` sont exclus de la synchronisation du
bundle (`databricks.yml`) : ils ne sont pas lus à l'exécution.

## Modèles et index

| Étape | Modèle ou index | Réglage |
|---|---|---|
| Comparaison : analyse (résumé et tableau des changements) | Modèle configuré dans `app.yaml` | `COMPARE_ANALYSIS_ENDPOINT` |
| Comparaison : recherche d'impact | Index Vector Search | `COMPARE_IMPACT_INDEX` |
| Comparaison : jugement de chaque document candidat | `databricks-gpt-5-6-luna` | `COMPARE_IMPACT_ENDPOINT` |
| Comparaison : résumé d'un document (texte, image) | `databricks-gpt-5-6-luna` | `COMPARE_SUMMARY_ENDPOINT`, `COMPARE_SUMMARY_IMAGE_ENDPOINT` |
| Chat : réponse | `databricks-gpt-6-luna`, secours `databricks-gpt-5-6-luna` | `CHAT_LLM_ENDPOINT`, `CHAT_LLM_FALLBACK_ENDPOINTS` |
| Chat : réécriture des requêtes | `databricks-gpt-6-luna`, secours `databricks-gpt-5-6-luna` ; sur toutes les cibles déployées, `databricks-gemini-3-1-flash-lite` | `CHAT_REWRITE_ENDPOINT`, `CHAT_REWRITE_FALLBACK_ENDPOINTS` |
| Chat : pont de traduction (désactivé) | `databricks-gpt-5-6-luna` | `CHAT_TRANSLATE_ENDPOINT` |
| Base : description des images (vision) | `databricks-gpt-5-6-luna` | `PARSING_LLM_ENDPOINT` |
| Base : embeddings des passages | `databricks-qwen3-embedding-0-6b` (index géré par Databricks) | `embedding_model` (tâche `5_sync_index`) |

Les valeurs exactes par cible sont dans `app.yaml` et dans
`utils/deploy/target_env.json`.

## Démarrage

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
   journalise `Starting in development mode` si `.env.local` est chargé.

Dépendances Python : `pip install -r requirements.txt`.

## Tests

```
python -m pytest
```

La suite est dans `tests/` (`testpaths` dans `pyproject.toml`). Les appels à
Databricks et aux modèles sont remplacés par des doubles ; les tests Spark sont
ignorés si pyspark ou la JVM manquent. Chaque README de fonctionnalité indique
les tests qui la couvrent.

Le frontend se vérifie par le lint et le build :

```
cd client
bun run lint
bun run build
```

## Déploiement

| Environnement (`-AppEnv`) | Cible Databricks | Application | Profil CLI | Workspace |
|---|---|---|---|---|
| `dev` | `dev` | `qualibot` | `DEV` | `dbc-c623749d-731b` |
| `uat` | `qualibot-uat` | `qualibot` | `UAT` | `dbc-3a17bfce-9e88` |
| `uat-test` | `qualibot-uat-test` | `qualibot-uat-test` | `UAT` | `dbc-3a17bfce-9e88` |
| `prod` | `qualibot-prod` | `qualibot` | `qualibot-prod` | `dbc-77b87d5a-6bcb` |

Les hôtes complets sont dans `databricks.yml`. Le profil CLI de la cible doit
être authentifié. Les étapes à faire dans Databricks (authentification, premier
déploiement, jobs à lancer ou à activer, droits) sont dans
[runbooks/uat.md](runbooks/uat.md) pour l'UAT, [runbooks/prod.md](runbooks/prod.md)
pour la PROD et [runbooks/dev.md](runbooks/dev.md) pour la DEV.

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

## Ressources par environnement

| Environnement | Tables (`<env>_proj.qualibot`) | Volume de l'application | Index du chat | Base Lakebase |
|---|---|---|---|---|
| DEV | `dev_proj.qualibot` | `dev_proj.qualibot.doc_compare` | `dev_proj.qualibot.chunks_index` | `doccompare` |
| UAT | `uat_proj.qualibot` | `uat_proj.qualibot.doc_compare` | `uat_proj.qualibot.chunks_index` | `doccompare` |
| UAT-TEST | `uat_proj.qualibot`, suffixe `_test` | `uat_proj.qualibot.doc_compare_test` | `uat_proj.qualibot.chunks_index_test` | `doccompare_test` |
| PROD | `prod_proj.qualibot` | `prod_proj.qualibot.doc_compare` | `prod_proj.qualibot.chunks_index` | `doccompare` |

Les tables Delta, les volumes et les index Vector Search sont dans le schéma
`<env>_proj.qualibot`. La base Lakebase est celle du projet `qualibot`. La cible
`uat-test` est un environnement de test isolé : elle ne touche ni aux données ni
aux index de l'UAT.

Tables de l'application dans Lakebase : `messages`, `feedbacks`, `llm_requests`,
`errors` (comparaison et journal commun), `impact_requests`,
`impact_document_results`, `impact_feedbacks`, `impact_cache`, `summary_cache`
(comparaison), `chat_sessions`, `chat_messages`, `chat_feedbacks`, `chat_turns`,
`chat_retrieved_chunks` (chat), `doc_catalog`, `knowledge_base_metadata`, `users`.
Le schéma est créé et complété par l'application au démarrage. Le détail des
colonnes est dans [docs/lakebase_schema.md](docs/lakebase_schema.md).

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
| [docs/lakebase_schema.md](docs/lakebase_schema.md) | Colonnes du journal Lakebase |
| [runbooks/README.md](runbooks/README.md) | Index des étapes à réaliser dans Databricks |
| [runbooks/retest.md](runbooks/retest.md) | Ce qu'il faut relancer après un changement de chemins ou de notebooks |
