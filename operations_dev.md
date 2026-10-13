# Opérations manuelles — copie DEV de l'UAT (Jules)

Même règle que `OPERATIONS.md` : Claude n'a pas accès à Databricks, chaque étape à faire à la main est listée ici,
prête à copier-coller. Cocher et dater une fois faite. Ce fichier ne couvre **que** l'environnement DEV ;
`OPERATIONS.md` reste la référence pour UAT / uat-test / PROD.

Branche : **`feature/chat-vsi-merged-on-impact-search`** (= `claude/hopeful-bardeen-57zeur`). Pas de PR ; le zip à
déployer est celui de cette branche.

Mis à jour le 2026-10-13 : le fichier est réduit à ce qui reste vrai et utile. **Suivre le bloc 0 (ordre complet).**
Les anciens blocs (Knowledge Assistants, tables `_v1`, notation `score_production_qa`, comparaison KA contre VSI, test de
découpage) sont supprimés du fichier ; ils sont dans l'historique du dépôt (commit `f545093`).
Rien de ce qui suit n'a été confirmé comme fait par Claude : seul ce qui est coché l'est.

## Décisions (2026-10-05, mises à jour)

| Sujet | Choix |
|---|---|
| Workspace | DEV — `https://dbc-c623749d-731b.cloud.databricks.com`, profil CLI `DEV` |
| Cible bundle | `dev` (en miroir de `qualibot-uat`) |
| App | `qualibot` (même nom qu'en UAT, workspace différent), SP de l'app `8e411164-a7e8-46ff-8013-8c56af2c3656` |
| Catalog / schema | `dev_landingzone.qualibot` (ce que le pipeline et l'app écrivent), `dev_proj.qualibot` (projet : évaluation, prompts, traces) |
| Suffixe tables / index | aucun depuis le 2026-10-08 (un seul index `chunks_index`, filtre de division) |
| Identité des jobs (`run_as`) | SP DEV `job-runner-sa-dev`, `fde6ff28-739f-4a41-b61e-604a298c8478` |
| Lakebase | projet `qualibot`, branche `production`, base `doccompare` (l'app crée ses tables) |
| Corpus | copie UAT → DEV via le volume `uat_landingzone.qualibot.staging` : chunks + état du pipeline. **Aucun run complet du parsing** |
| Phase archive avant 2018 | ignorée en DEV (plafond 0, fiches hors RAG, pas de `chunks_full`) |
| Vector Search | endpoint `qualibot`, embeddings `databricks-qwen3-embedding-0-6b` |
| Contrôle par groupe (can_chat / can_compare) | **désactivé en DEV seulement** (`CAPS_BYPASS=true`) |
| Accès à l'app | mêmes groupes qu'en UAT + `jules.gourio.external@latecoere.aero` + `mehdi.lamrani@databricks.com` + SP DEV |
| Plannings | tous en PAUSED, sauf `lakebase_import_uat_to_dev` |
| Bitbucket | pipelines manuelles `deploy-dev` / `deploy-dev-jobs`, environnement de déploiement **Development** |
| LibreOffice (aperçu PDF exact) | archive copiée depuis l'UAT par le job de copie |
| Modèles Docling | volume DEV existant `/Volumes/dev_landingzone/qualibot/docling_models/docling_models` |

## Ressources DEV

Workspace : https://dbc-c623749d-731b.cloud.databricks.com (id `2865348338307293`).

| Quoi | Lien / commande |
|---|---|
| App `qualibot` (utilisateurs) | https://qualibot-2865348338307293.aws.databricksapps.com |
| App — gestion | https://dbc-c623749d-731b.cloud.databricks.com/apps/qualibot · `databricks apps get qualibot --profile DEV` |
| Schema `dev_landingzone.qualibot` | https://dbc-c623749d-731b.cloud.databricks.com/explore/data/dev_landingzone/qualibot · `databricks tables list dev_landingzone qualibot --profile DEV` |
| Volumes `doc_compare`, `test`, `images`, `staging`, `docling_models` | `databricks volumes list dev_landingzone qualibot --profile DEV` |
| Schema projet `dev_proj.qualibot` | https://dbc-c623749d-731b.cloud.databricks.com/explore/data/dev_proj/qualibot |
| Vector Search, endpoint `qualibot`, index `chunks_index` | https://dbc-c623749d-731b.cloud.databricks.com/compute/vector-search/qualibot · `databricks vector-search-indexes get-index dev_landingzone.qualibot.chunks_index --profile DEV` |
| Lakebase, projet `qualibot` (« Qualibot History »), branche `production`, base `doccompare` | `databricks postgres get-project projects/qualibot --profile DEV` ; rôles : `databricks postgres list-roles projects/qualibot/branches/production --profile DEV` |
| Bundle (fichiers, état) | https://dbc-c623749d-731b.cloud.databricks.com/browse/folders/Workspace/Shared/.bundle/qualibot/dev · `databricks bundle summary -t dev --profile DEV` |
| Jobs | https://dbc-c623749d-731b.cloud.databricks.com/jobs?filter=qualibot |

Jobs du bundle `dev` : `Z_1_Qualibot_Copy_Uat_To_Dev` (manuel), `D_1_Qualibot_Parsing_Pipeline_dev` (PAUSED),
`D_1_Qualibot_Lakebase_Import_Uat_To_Dev` (actif), `D_1_Qualibot_Lakebase_Export_To_Volume_dev` (PAUSED),
`Z_1_Qualibot_Grant_App_Access_dev` (manuel), `Z_1_Qualibot_Migrate_Lakebase_dev` (manuel),
`Z_2_…_Create_Chat_Experiment`, `Z_3_…_Register_Chat_Prompts`, `D_2_Qualibot_Score_Chat_Traces_dev`,
`D_1_Qualibot_Apps_Stop_Nightly_Dev`, `W_1_Qualibot_Stop_Weekend_Dev`, `W_1_Qualibot_Start_Weekend_Dev` (PAUSED).

## Prérequis

- [x] **P1. Profil CLI `DEV`** _(OK 2026-10-05)_

  ```powershell
  databricks auth login --host https://dbc-c623749d-731b.cloud.databricks.com --profile DEV
  databricks current-user me --profile DEV
  ```

- [x] **P2. Le SP DEV existe** _(OK 2026-10-05)_ et vous avez le rôle « Service principal: User » dessus (sinon `bundle deploy`
  refuse le `run_as`) :

  ```powershell
  databricks service-principals list --profile DEV --filter "applicationId eq 'fde6ff28-739f-4a41-b61e-604a298c8478'"
  ```

- [ ] **P3. Droits Unity Catalog du SP DEV.** Vérifier ce qu'il a déjà (peut-être via un groupe) :

  ```powershell
  $sp = 'fde6ff28-739f-4a41-b61e-604a298c8478'
  "=== dev_landingzone";          databricks grants get-effective catalog dev_landingzone --principal $sp --profile DEV
  "=== dev_landingzone.qualibot"; databricks grants get-effective schema dev_landingzone.qualibot --principal $sp --profile DEV
  "=== dev_proj";                 databricks grants get-effective catalog dev_proj --principal $sp --profile DEV
  "=== uat_landingzone.qualibot"; databricks grants get-effective schema uat_landingzone.qualibot --principal $sp --profile DEV
  "=== prod_bronze.intraqual";    databricks grants get-effective schema prod_bronze.intraqual --principal $sp --profile DEV
  "=== prod_landingzone.intraqual"; databricks grants get-effective schema prod_landingzone.intraqual --principal $sp --profile DEV
  ```

  Il lui faut : `USE CATALOG` sur `dev_landingzone`, `dev_proj`, `uat_landingzone`, `prod_bronze`, `prod_landingzone` ; sur
  `dev_landingzone.qualibot` : `USE SCHEMA`, `CREATE TABLE`, `CREATE VOLUME`, `SELECT`, `MODIFY`, `READ VOLUME`,
  `WRITE VOLUME` ; `READ VOLUME` sur `uat_landingzone.qualibot.staging` ; `SELECT` sur `prod_bronze.intraqual` et
  `prod_landingzone.intraqual`, `READ VOLUME` sur `prod_landingzone.intraqual.intraqual_documents`.
  Le niveau schema `dev_landingzone.qualibot`, vous pouvez le donner (vous en êtes owner) :

  ```sql
  GRANT USE SCHEMA, CREATE TABLE, CREATE VOLUME, SELECT, MODIFY, READ VOLUME, WRITE VOLUME
    ON SCHEMA dev_landingzone.qualibot TO `fde6ff28-739f-4a41-b61e-604a298c8478`;
  ```

  Le reste (`USE CATALOG`, droits sur `uat_landingzone` / `prod_*`, owner de `dev_proj` : `leap-core-service_accounts-dev`)
  est à demander à un admin des catalogs, pour ce qui manque seulement.

- [x] **P4. Groupes de compte visibles en DEV** _(OK 2026-10-05)_ : `Role-Project-LEAP-CoreAdmin`, `Role-Project-LEAP-CoreDev`,
  `Role-Project-LEAP-End-users-Qualibot-DocCompare`, `Role-Project-LEAP-End-users-Qualibot-ChatBot`,
  `leap-qualibot-service-accounts`, utilisateur `mehdi.lamrani@databricks.com` (sinon le `bundle deploy` échoue sur la permission
  correspondante) :

  ```powershell
  databricks groups list --profile DEV --filter "displayName sw 'Role-Project-LEAP'"
  databricks groups list --profile DEV --filter "displayName eq 'leap-qualibot-service-accounts'"
  databricks users list --profile DEV --filter "userName eq 'mehdi.lamrani@databricks.com'"
  ```

## À faire

PowerShell, depuis la racine du projet, sur la machine de déploiement.

### 0. Ordre complet du DEV (2026-10-12)

Toutes les commandes sont ici, dans l'ordre où il faut les lancer. Cocher et dater au fur et à mesure.

- [ ] **0.1 Session PowerShell** (au début de chaque session) :

  ```powershell
  Remove-Item Env:DATABRICKS_TOKEN -ErrorAction SilentlyContinue
  Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
  databricks auth profiles
  ```

  Le profil `DEV` doit apparaître comme valide. Sinon : `databricks auth login --profile DEV`.
- [ ] **0.2 Code** : extraire le zip de la branche dans un **dossier vide** (le code a été réorganisé : `server/services/{chat,compare,infra}/`,
  prompts dans `server/prompts/chat/`, tests renommés ; extrait par-dessus l'ancien dossier, les anciens fichiers resteraient et seraient
  redéployés). Si tu dois quand même copier par-dessus, supprimer d'abord les anciens :

  ```powershell
  Remove-Item server\services\chat_vsi.py, server\services\chat_vsi_llm.py, server\services\turn_log.py, server\services\mlflow_turns.py, server\services\translation_bridge.py -ErrorAction SilentlyContinue
  Remove-Item server\services\lakebase.py, server\services\user.py, server\services\vector_search.py, server\services\vs_gate.py, server\services\error_origin.py, server\services\doc_catalog.py, server\services\streaming.py -ErrorAction SilentlyContinue
  Remove-Item server\services\chunked_analysis.py, server\services\impact_queries.py, server\services\summarize.py, server\services\conversion.py, server\services\soffice.py, server\services\export_helpers.py -ErrorAction SilentlyContinue
  Remove-Item -Recurse server\services\processors, server\config\chat_vsi, config, scripts, archive, mlruns -ErrorAction SilentlyContinue
  Remove-Item server\config\capabilities_snapshot.json, tests\test_chat_vsi.py, tests\test_chat_vsi_llm.py, mlflow.db -ErrorAction SilentlyContinue
  Remove-Item utils\user_capabilities\build_capabilities_snapshot.py, utils\evaluation\score_production_qa.py -ErrorAction SilentlyContinue
  Remove-Item utils\deploy\watch_uat.ps1, utils\deploy\pull_from_uat.ps1, utils\lakebase_sync\export_lakebase_uat_to_volume.py -ErrorAction SilentlyContinue
  Remove-Item -Recurse utils\dev, docs\archive, docs\evidence -ErrorAction SilentlyContinue
  Remove-Item resources\chat_experiment.yml, resources\evaluation.yml -ErrorAction SilentlyContinue
  Remove-Item client\src\lib\downloadAsPdf.ts -ErrorAction SilentlyContinue
  ```

  Vérifier :

  ```powershell
  Get-Content server\services\chat\engine.py -TotalCount 1
  Test-Path server\prompts\chat\system\base.md
  Test-Path resources\chat_mlflow.yml
  ```

  Attendu : `"""Chat — the chat engine: answers from the Vector Search index, no Knowledge Assistant."""`, puis `True`, `True`.
  Si `bundle validate` dit `dev: no such target` ou `unknown field`, le `databricks.yml` local est abîmé : le remplacer par celui de la branche.
- [ ] **0.3 L'index `chunks_index` est ONLINE avec toutes ses lignes** (l'embedding prend 30 à 60 min) :

  ```powershell
  databricks vector-search-indexes get-index dev_landingzone.qualibot.chunks_index --profile DEV
  ```

  Chercher `"ready": true` (ou l'état `ONLINE`) et `indexed_row_count` ; éditeur SQL DEV, doit donner le même nombre :

  ```sql
  SELECT count(*) AS chunks FROM dev_landingzone.qualibot.chunks;
  ```

  Si l'index n'existe pas, le créer (sous votre identité, propriétaire de l'endpoint `qualibot` et de la table `chunks`) ; même
  spécification que l'UAT, la création lance la première synchronisation :

  ```powershell
  $json = '{"name": "dev_landingzone.qualibot.chunks_index", "endpoint_name": "qualibot", "primary_key": "chunk_id", "index_type": "DELTA_SYNC", "delta_sync_index_spec": {"source_table": "dev_landingzone.qualibot.chunks", "pipeline_type": "TRIGGERED", "embedding_source_columns": [{"name": "chunk_text", "embedding_model_endpoint_name": "databricks-qwen3-embedding-0-6b"}]}}'
  [IO.File]::WriteAllText("$PWD\chunks_index.json", $json)
  databricks vector-search-indexes create-index --json "@chunks_index.json" --profile DEV
  Remove-Item chunks_index.json
  ```

  **Ne pas continuer** avant qu'il soit ONLINE (le chat et le contrôle 0.12 en ont besoin). La table `chunks` doit avoir
  `delta.enableChangeDataFeed = true` et 60 jours de rétention (`delta.deletedFileRetentionDuration`, `delta.logRetentionDuration`).
- [ ] **0.4 Déployer le bundle** (jobs de création de l'expérience, d'enregistrement des prompts, de notation ; job de droits étendu à
  `dev_proj`) :

  ```powershell
  databricks bundle deploy -t dev --profile DEV
  ```

  Les plannings restent en pause. Contrôle (UI DEV → Jobs & Pipelines, filtre `Qualibot`) : aucun planning actif en dehors de
  `lakebase_import_uat_to_dev`. Pour prévisualiser sans rien modifier : `databricks bundle validate -t dev --profile DEV` puis
  `databricks bundle plan -t dev --profile DEV` (chaque ressource doit être `create` ou `update`, rien en `delete` / `recreate` sans accord).
- [ ] **0.5 Droits** — le job donne au SP de l'app et au SP des jobs ce qu'ils utilisent (volumes, index, endpoints LLM, `dev_proj.qualibot`) :

  ```powershell
  databricks bundle run grant_app_access_dev -t dev --profile DEV
  ```

  Attendu : une ligne `OK:` par droit, aucune `FAILED`. Pour changer ce qui est accordé : les listes `volumes` / `indexes` /
  `serving_endpoints` du job `grant_app_access_dev` dans `databricks.yml` (`base_parameters`). Une ligne `FAILED` sur `dev_proj` : exécuter à
  la main (éditeur SQL DEV, compte qui peut donner ces droits) puis relancer le job :

  ```sql
  GRANT USE CATALOG ON CATALOG dev_proj TO `fde6ff28-739f-4a41-b61e-604a298c8478`;
  GRANT USE SCHEMA, CREATE TABLE, CREATE FUNCTION, EXECUTE, SELECT, MODIFY ON SCHEMA dev_proj.qualibot TO `fde6ff28-739f-4a41-b61e-604a298c8478`;
  GRANT USE CATALOG ON CATALOG dev_proj TO `Role-Project-LEAP-CoreDev`;
  GRANT USE SCHEMA, CREATE TABLE, SELECT, MODIFY ON SCHEMA dev_proj.qualibot TO `Role-Project-LEAP-CoreDev`;
  ```

  Une ligne `FAILED: CAN_QUERY on serving endpoint …` : le SP des jobs n'a pas CAN_MANAGE sur cet endpoint LLM (souvent réservé à un
  admin du workspace). Faire accorder le droit par un admin, ou :

  ```powershell
  $id = (databricks serving-endpoints get databricks-gpt-6-luna --profile DEV -o json | ConvertFrom-Json).id
  databricks serving-endpoints get-permissions $id --profile DEV
  databricks serving-endpoints update-permissions $id --profile DEV --json '{\"access_control_list\":[{\"service_principal_name\":\"8e411164-a7e8-46ff-8013-8c56af2c3656\",\"permission_level\":\"CAN_QUERY\"}]}'
  ```

  (même chose pour `databricks-gpt-5-6-luna`.) En cas de 403 de l'app : « Document search failed (Vector Search returned 403) » = index,
  « Chat failed … endpoint returned 403 » = endpoint LLM ; `databricks apps logs qualibot --profile DEV` donne la ligne.
- [ ] **0.6 Tables d'évaluation dans `dev_proj.qualibot`**. Éditeur SQL DEV, **une instruction à la fois** ; une table qui n'existe pas
  donne `TABLE_OR_VIEW_NOT_FOUND` : passer à la suivante. Les droits sur `dev_proj` viennent de 0.5. Règle : `dev_landingzone.qualibot`
  garde ce que le pipeline et l'app écrivent, `dev_proj.qualibot` reçoit ce qui définit ou mesure le projet (les notebooks de
  `utils/evaluation/` ont `dev_proj.qualibot.*` comme défauts). Les tables sont copiées, pas déplacées :

  ```sql
  CREATE TABLE IF NOT EXISTS dev_proj.qualibot.qualibot_eval_golden DEEP CLONE dev_landingzone.qualibot.qualibot_eval_golden;
  CREATE TABLE IF NOT EXISTS dev_proj.qualibot.eval_retrieval_runs DEEP CLONE dev_landingzone.qualibot.eval_retrieval_runs;
  CREATE TABLE IF NOT EXISTS dev_proj.qualibot.eval_pairwise_runs DEEP CLONE dev_landingzone.qualibot.eval_pairwise_runs;
  CREATE TABLE IF NOT EXISTS dev_proj.qualibot.eval_pairwise_runs_cache DEEP CLONE dev_landingzone.qualibot.eval_pairwise_runs_cache;
  CREATE TABLE IF NOT EXISTS dev_proj.qualibot.eval_load_runs DEEP CLONE dev_landingzone.qualibot.eval_load_runs;
  CREATE TABLE IF NOT EXISTS dev_proj.qualibot.eval_vs_load_runs DEEP CLONE dev_landingzone.qualibot.eval_vs_load_runs;
  CREATE TABLE IF NOT EXISTS dev_proj.qualibot.synthetic_retrieval_questions DEEP CLONE dev_landingzone.qualibot.synthetic_retrieval_questions;
  CREATE TABLE IF NOT EXISTS dev_proj.qualibot.feedback_failure_cases DEEP CLONE dev_landingzone.qualibot.feedback_failure_cases;
  ```

  Si `synthetic_retrieval_questions` / `feedback_failure_cases` n'existent pas dans `dev_landingzone`, les créer depuis l'UAT :

  ```sql
  CREATE TABLE IF NOT EXISTS dev_proj.qualibot.synthetic_retrieval_questions AS SELECT * FROM uat_landingzone.qualibot.synthetic_retrieval_questions_v2;
  CREATE TABLE IF NOT EXISTS dev_proj.qualibot.feedback_failure_cases AS SELECT * FROM uat_landingzone.qualibot.feedback_failure_cases;
  ```

  Contrôle : `SELECT count(*) FROM dev_proj.qualibot.qualibot_eval_golden;` donne le même nombre que dans `dev_landingzone.qualibot`.
- [ ] **0.7 Supprimer les 3 Knowledge Assistants DEV** (sans effet sur le reste) : UI DEV → **Agents** → `qualibot_ALL_v2`, `qualibot_AS_v2`,
  `qualibot_IS_v2` → ⋮ → **Delete**. Si l'UI refuse, dans une cellule Python d'un notebook DEV serverless :

  ```python
  from databricks.sdk import WorkspaceClient
  w = WorkspaceClient()
  for ka in w.knowledge_assistants.list_knowledge_assistants():
      print(ka.name, ka.display_name, ka.endpoint_name)
      if ka.display_name in ('qualibot_ALL_v2', 'qualibot_AS_v2', 'qualibot_IS_v2'):
          w.knowledge_assistants.delete_knowledge_assistant(name=ka.name)
          print('  deleted')
  ```

  `PERMISSION_DENIED` : lancer la cellule en job sous le SP DEV. Méthode absente : `%pip install -U databricks-sdk` puis
  `dbutils.library.restartPython()`.
- [ ] **0.8 Expérience MLflow du chat** — propriétaire = le SP DEV, type GenAI :

  ```powershell
  databricks bundle run create_chat_experiment_dev -t dev --profile DEV
  ```

  Attendu : `experiment /Shared/qualibot/chat_turns created (id …)` puis `app qualibot (service principal …) can edit the experiment`.
  Si une expérience du même nom existe déjà à ton nom (essai manuel), la supprimer d'abord dans l'UI (Experiments → ⋮ → Delete).
- [ ] **0.9 Prompts dans le registre** — enregistre aussi chaque version en tag `chat_prompt.<clé>` de l'expérience (qui doit exister : 0.8) ;
  l'app lit ces tags, jamais le registre :

  ```powershell
  databricks bundle run register_chat_prompts_dev -t dev --profile DEV
  ```

  Attendu : une ligne `registered version … alias deployed` par prompt (8 prompts `dev_proj.qualibot.chat_*`). **À refaire après chaque
  déploiement qui change un fichier de `server/prompts/chat/`** : tant que le texte enregistré diffère du fichier, l'app n'attache pas la
  version à la trace (log `prompt … differs from the registered "deployed" version`).
- [ ] **0.10 Déployer l'app** (un seul déploiement pour tout ; l'app crée ses nouvelles colonnes — `chat_turns.mlflow_trace_id`,
  `doc_catalog.revision`/`doc_date`, `errors.origin`/`fingerprint`/`user_message`… — à son démarrage ; `CHAT_MLFLOW_EXPERIMENT` vient de
  `utils/deploy/target_env.json`) :

  ```powershell
  .\utils\deploy\deploy_qualibot.ps1 -AppEnv dev
  ```

  Logs de l'app au démarrage : `Lakebase schema ready` (une erreur juste avant : me l'envoyer). Les nouveaux champs du chat sont décrits dans
  `docs/lakebase_schema.md`.
- [ ] **0.11 Catalogue des documents avec les révisions** — la tâche 6 seule (rien n'est parsé), puis redémarrage de l'app (sinon l'app recharge
  le catalogue au plus tard 30 min après) :

  ```powershell
  databricks bundle run parsing_pipeline -t dev --profile DEV --only 6_update_kb_metadata
  databricks apps stop qualibot --profile DEV
  databricks apps start qualibot --profile DEV
  ```

  Attendu : `doccompare: doc_catalog N -> N documents`, **sans** la ligne `has no revision/doc_date columns` ; dans les logs de l'app :
  `doc_catalog: loaded N documents from Lakebase`. Si la ligne `has no revision/doc_date columns` apparaît, la ligne
  `cannot add revision/doc_date as … (table owner: …)` juste avant dit qui possède la table ; la vérifier (éditeur SQL Lakebase, base `doccompare`) :

  ```sql
  SELECT tableowner FROM pg_tables WHERE tablename = 'doc_catalog';
  SELECT COUNT(*) AS docs, COUNT(revision) AS with_revision, COUNT(doc_date) AS with_date FROM doc_catalog;
  SELECT ref, revision, doc_date FROM doc_catalog WHERE ref IN ('MI-14059', 'QP-1518', 'MIT-1087');
  ```

  Si la tâche 6 échoue (droits Lakebase du SP), l'app marche quand même avec l'ancien instantané `doc_catalog.json` (log `using the bundled snapshot`).
- [ ] **0.12 Contrôle de la recherche** (≈ 1 €, ≈ 10 min) : notebook `/Workspace/Shared/.bundle/qualibot/dev/files/utils/evaluation/retrieval_eval`,
  calcul **serverless**, Run all avec les défauts (`indexes = chat`). Attendu : environ 83 % des documents attendus trouvés. **M'envoyer le dernier tableau.**
  Les résultats vont dans `dev_proj.qualibot.eval_retrieval_runs` ; seules les questions pas encore réussies pour une configuration sont relancées.
- [ ] **0.12b Mesurer « le premier message est seulement traduit »** (option `CHAT_TRANSLATE_ONLY`, **éteinte par défaut depuis le 2026-10-12** : allumée,
  elle perdait les développements d'acronymes et de synonymes de la réécriture, la question « qui est le CEO de Latécoère » ne retrouvait plus les
  documents qui disent « PDG »). Même notebook que 0.12, widget `indexes` à `chat,translate|translate=on`, Run all, ≈ 2 €. **M'envoyer le dernier tableau.**
  On ne l'allume (`CHAT_TRANSLATE_ONLY=on`, je change la valeur, tu redéploies) que si `translate` est au moins aussi bon que `chat` sur les
  documents attendus trouvés **et** que la question du CEO redonne la bonne réponse.
- [ ] **0.12c Accélérer la réécriture** (2026-10-12). Trois leviers dans le code : `CHAT_REWRITE_REASONING_EFFORT=low` (par défaut ; un endpoint
  qui refuse le paramètre est rappelé sans, une seule fois, log `refused reasoning_effort`) ; la recherche reclassée de la question telle quelle part
  **pendant** la réécriture ; un modèle qui ne réfléchit pas (`databricks-gemini-3-1-flash-lite`, à mesurer). Pour comparer qualité **et** vitesse : même
  notebook, widget `indexes` à
  `chat,effort-off|effort=off,minimal|effort=minimal,flash|rewrite=databricks-gemini-3-1-flash-lite`, Run all, ≈ 6 €. **M'envoyer le dernier tableau**
  (documents attendus trouvés **et** `latency_s`) : on garde la configuration la plus rapide qui ne perd rien ; `flash` ne devient la réécriture
  (`CHAT_REWRITE_ENDPOINT`) que s'il ne perd rien.
- [ ] **0.13 Tests dans l'app** — une question normale en AS, une en IS, une avec un numéro de document (ex. `MI-14242`), une hors sujet (attendu :
  refus poli), une en espagnol ; « donne-moi le lien de l'OPEX Sharepoint » (→ l'URL de INAQ-742) ; 👍 sur une réponse et 👎 sur une autre ;
  un seul onglet **Chat** ; les documents cités sont bien de la division demandée. Bouton **Télécharger → Response — Markdown** (le fichier finit
  par la liste **Sources** `[1] REF — titre — Version … — lien`), PDF du chat (**Response — PDF**, **Full session — PDF**) et du compare
  (**Download PDF** du Change Summary et du résumé d'un document). « Judge Impacted Docs » dans Compare sur deux révisions connues. La question
  NDT (« quels documents parlent des exigences de qualification CND ? ») : plus de « version not stated », pastilles `REF · B`, titre et « Version B
  (jj/mm/aaaa) » au survol ; un fil d'hier affiche « Yesterday ». Puis (1 min après) l'expérience : DEV → **Experiments** → `/Shared/qualibot/chat_turns`
  → **Traces** : une trace par question, l'arbre `chat_turn → retrieval → answer`, les passages dans la sortie de `retrieval`, 4 prompts liés,
  `user_vote` sur les deux réponses votées. Éditeur SQL Lakebase (base `doccompare`) :

  ```sql
  SELECT created_at, trace_id, mlflow_trace_id, status, warnings, answer_endpoint, first_token_ms, total_ms
  FROM chat_turns ORDER BY id DESC LIMIT 6;

  SELECT t.trace_id, COUNT(*) AS passages, SUM(CASE WHEN c.kept THEN 1 ELSE 0 END) AS sent,
         SUM(CASE WHEN c.cited THEN 1 ELSE 0 END) AS cited
  FROM chat_turns t JOIN chat_retrieved_chunks c ON c.turn_id = t.id
  GROUP BY t.id, t.trace_id ORDER BY t.id DESC LIMIT 6;

  SELECT created_at, endpoint, severity, stage, error_type, origin, fingerprint, user_message, LEFT(error_msg, 120) AS msg
  FROM errors ORDER BY id DESC LIMIT 20;
  ```

  Attendu : une ligne par question (`ok` ou `degraded`, `aborted` si l'onglet est fermé avant la fin), une trentaine de passages par tour. **M'envoyer** :
  une capture d'une trace ouverte, les trois résultats SQL, une capture de la réponse NDT. Si rien n'apparaît dans MLflow : les lignes `mlflow_turns:` des
  logs de l'app. Pour voir le message « Qualibot is a bit tired… » : me demander de mettre un index inexistant dans `CHAT_INDEX` pour `dev`
  (`utils/deploy/target_env.json`), redéployer, poser une question (attendu : une ligne `errors` `stage = search`), puis remettre la valeur.
- [ ] **0.14 Notation des traces, à blanc puis réelle** — d'abord l'estimation (ne juge rien) :

  ```powershell
  databricks bundle run score_chat_traces_dev -t dev --profile DEV
  ```

  UI → Jobs → `D_2_Qualibot_Score_Chat_Traces_dev` → dernier run : par juge, le nombre de traces lues et `estimated input tokens`. **M'envoyer
  ces lignes.** Puis, hors des heures d'usage du chat (le juge partage les quotas de tokens de l'endpoint Luna), le run réel (plafonds :
  `max_input_tokens` = 2 000 000, 2 appels en parallèle, 2 appels par seconde, reprises sur 429) :

  ```powershell
  databricks bundle run score_chat_traces_dev -t dev --profile DEV --params dry_run=false
  ```

  **M'envoyer** le tableau final du notebook (table `dev_proj.qualibot.chat_trace_scores`) et les lignes `failed` de « Quality Checks ». Quand les juges
  sont validés, on baisse les échantillons (`chat_scorers.TARGET_SAMPLE_RATES`) et on active le planning (PAUSED aujourd'hui).
- [ ] **0.15 Nettoyage** (seulement après 0.12 et 0.13 réussis) :
  - ancien job de notation, si encore présent : UI DEV → Jobs → `D_1_Qualibot_Score_Production_Qa` → ⋮ → **Delete** (tables `chat_quality_scores` et
    `chat_quality_scoring_runs` : rien ne les supprime) ;
  - anciens index, puis leurs tables (ignorer « does not exist » pour ce qui n'a pas été construit) :

    ```powershell
    databricks vector-search-indexes delete-index dev_landingzone.qualibot.chunks_index_v1    --profile DEV
    databricks vector-search-indexes delete-index dev_landingzone.qualibot.chunks_as_index_v1 --profile DEV
    databricks vector-search-indexes delete-index dev_landingzone.qualibot.chunks_is_index_v1 --profile DEV
    databricks vector-search-indexes delete-index dev_landingzone.qualibot.chunks_index_v2a   --profile DEV
    databricks vector-search-indexes delete-index dev_landingzone.qualibot.chunks_index_v2b   --profile DEV
    databricks vector-search-indexes delete-index dev_landingzone.qualibot.chunks_index_v2c   --profile DEV
    ```

    ```sql
    DROP TABLE IF EXISTS dev_landingzone.qualibot.chunks_v1;
    DROP TABLE IF EXISTS dev_landingzone.qualibot.src_chunks_as_v1;
    DROP TABLE IF EXISTS dev_landingzone.qualibot.src_chunks_is_v1;
    DROP TABLE IF EXISTS dev_landingzone.qualibot.chunks_v2a;
    DROP TABLE IF EXISTS dev_landingzone.qualibot.chunks_v2b;
    DROP TABLE IF EXISTS dev_landingzone.qualibot.chunks_v2c;
    ```

  - copies d'évaluation restées dans `dev_landingzone` (après un run d'évaluation réussi depuis `dev_proj`) :

    ```sql
    DROP TABLE IF EXISTS dev_landingzone.qualibot.qualibot_eval_golden;
    DROP TABLE IF EXISTS dev_landingzone.qualibot.eval_retrieval_runs;
    DROP TABLE IF EXISTS dev_landingzone.qualibot.eval_pairwise_runs;
    DROP TABLE IF EXISTS dev_landingzone.qualibot.eval_pairwise_runs_cache;
    DROP TABLE IF EXISTS dev_landingzone.qualibot.eval_load_runs;
    DROP TABLE IF EXISTS dev_landingzone.qualibot.eval_vs_load_runs;
    DROP TABLE IF EXISTS dev_landingzone.qualibot.synthetic_retrieval_questions;
    DROP TABLE IF EXISTS dev_landingzone.qualibot.feedback_failure_cases;
    DROP TABLE IF EXISTS dev_landingzone.qualibot.eval_golden_runs;
    ```

    (`UNDROP TABLE` reste possible 7 jours.) À **garder** dans `dev_landingzone.qualibot` : les tables écrites chaque jour par
    `lakebase_import_uat_to_dev` (`chat_feedbacks`, `chat_messages`, `chat_sessions`, `errors`, `feedbacks`, `impact_cache`, `impact_document_results`,
    `impact_requests`, `knowledge_base_metadata`, `llm_requests`, `messages`, `summary_cache`, `users`), `chat_quality_scores` /
    `chat_quality_scoring_runs`, les tables de l'application Translator (`dnt_rules`, `glossary_terms`) et celles du pipeline (`chunks`, `chunks_index`,
    `_pipeline_checkpoint`, `processed_files`, `image_metadata`, `parse_manifest`, `category_reference`). Les tables `eval_*` gardent les anciens
    noms d'essai dans leurs lignes : c'est l'historique des mesures (`docs/chat_tests.md`).

**Facultatif, à ta décision** : T (nombre de passages), U / V (tests de charge), traces MLflow en tables Delta Unity Catalog (plus adapté à un gros
volume, requêtable en SQL ; exige MLflow ≥ 3.11.1 dans l'app et un entrepôt SQL ; non modifiable ensuite pour cette expérience ; non testé : me le demander).
**Pas maintenant** : tout ce qui touche `qualibot-uat`, `qualibot-uat-test` ou `prod` (`OPERATIONS.md`, dont D5 qui déploie cette branche en UAT) : il faut
d'abord que le DEV soit validé.

### T. Combien de passages garder ? (2026-10-08)

Aujourd'hui : 3 requêtes × (12 passages reclassés + 10 bruts), fusionnés sans plafond. Les passages bruts viennent d'un test fait avec les anciens
passages de 4 000 caractères : à remesurer sur le découpage actuel. Les défauts du code ne changent pas tant que ce test n'a pas tranché.

- [ ] **T1. Recherche seule** (≈ 4 €, ≈ 30 min) : notebook `utils/evaluation/retrieval_eval.py`, widget `indexes` =

  ```text
  chat,rerank-only|raw=0,raw5|raw=5,cap40|cap=40,cap25|cap=25,rerank8-only|rerank=8|raw=0
  ```

  puis Run all. **M'envoyer les deux derniers tableaux** (par source et global : documents trouvés, dans les 5 premiers, tokens de contexte).
- [ ] **T2. Réponses** (≈ 2 €, ≈ 20 min, après T1) : notebook `pairwise_answers.py`, widgets `indexes` = la même liste que T1, `reference` =
  `databricks-gpt-6-luna@chat`, `contenders` = `databricks-gpt-6-luna@<label retenu en T1>`, `eval_id` = `passages-<label>`. **M'envoyer les trois tableaux.**

### U. Tests de charge (2026-10-08)

Mesures déjà faites : `docs/chat_tests.md` (§ 5.8). Le chat tient 40 questions simultanées avec la file Vector Search commune
(`server/services/infra/vs_gate.py`, 8 requêtes en cours au plus, relances sur 429) ; sans elle, Vector Search refusait (429) dès 20.

- [ ] **U1. Chat** : app DEV démarrée, notebook `utils/evaluation/load_test_chat.py` en serverless, Run all (défauts : paliers de 5, 10, 20, 40, 80
  questions en cours, ≈ 620 questions, ≈ 2 €, 30 à 60 min ; s'arrête au premier palier où plus de la moitié échoue). La cellule « Mode » dit si le test passe
  par l'app (`MODE = app`) ou, si l'app refuse le jeton du notebook, par le moteur appelé dans le notebook (`MODE = engine`). **M'envoyer** les deux
  tableaux de la dernière cellule, la ligne `MODE = …`, et les lignes `chat_llm:` des logs de l'app (bascule vers le modèle de secours, relances).
- [ ] **U2. Vector Search seul** (≈ 20 min, aucun appel LLM) : notebook `utils/evaluation/load_test_vector_search.py`, serverless, Run all. **M'envoyer** la
  sortie de la cellule « endpoint and index » (type d'endpoint, `scaling_info`) et les tableaux de la dernière cellule.

### E. Export du corpus UAT (workspace UAT, run ponctuel)

Lecture seule sur les tables UAT ; écrit uniquement dans `/Volumes/uat_landingzone/qualibot/staging/dev_copy/`. La cible `qualibot-uat` du bundle
n'est pas touchée. Tables exportées : chunks, `src_chunks_as`, `src_chunks_is`, `_pipeline_checkpoint`, `processed_files`, `image_metadata`,
`parse_manifest`, `category_reference` + l'archive LibreOffice. Rien de la phase archive avant 2018.

- [x] **E1. Importer le notebook dans votre dossier et lancer le run** _(2026-10-05 : chunks 75 086, as 59 897, is 11 241, checkpoint 7 097, processed_files 18 610, image_metadata 49 342, parse_manifest 4 942, category_reference 18 587)_

  ```powershell
  databricks workspace mkdirs /Users/jules.gourio.external@latecoere.aero/qualibot_dev_copy --profile UAT
  databricks workspace import /Users/jules.gourio.external@latecoere.aero/qualibot_dev_copy/export_uat_to_staging `
    --file utils\dev_copy\export_uat_to_staging.py --format SOURCE --language PYTHON --overwrite --profile UAT
  databricks jobs submit --json "@utils/dev_copy/export_uat_submit.json" --profile UAT
  ```

  Compute serverless, sous votre identité. Si l'écriture Delta dans le volume est refusée, relancer en Parquet : ajouter dans le JSON, sous
  `notebook_task`, `"base_parameters": {"FORMAT": "parquet"}` (l'import DEV lit le format dans le manifeste).
- [ ] **E2. Vérifier le manifeste** (nombre de lignes par table), depuis UAT puis **depuis DEV** (preuve que DEV lit ce volume) :

  ```powershell
  databricks fs cat dbfs:/Volumes/uat_landingzone/qualibot/staging/dev_copy/manifest.json --profile UAT
  databricks fs ls  dbfs:/Volumes/uat_landingzone/qualibot/staging/dev_copy/tables --profile DEV
  ```

### I. Infra DEV (`bundle deploy -t dev`, fait le 2026-10-05)

Crée l'app `qualibot`, le projet Lakebase `qualibot` (base `doccompare`), le schema `dev_proj.qualibot`, les volumes, les rôles Postgres et les jobs.

- [x] **I1b. Prévisualiser sans rien modifier** _(25 create, 5 update, 0 delete)_ :

  ```powershell
  python utils/deploy/render_target_config_env.py dev target_config.env
  databricks bundle validate -t dev --profile DEV
  databricks bundle plan -t dev --profile DEV
  ```

- [x] **I2. Déployer l'infra, puis le code de l'app** sous votre identité (profil `DEV`) : vous devenez owner de ce qui est créé ; le SP DEV a
  CAN_MANAGE sur l'app, le projet Lakebase et chaque job propre à DEV, pour que la pipeline Bitbucket (qui déploie en tant que SP) puisse les mettre à jour.

  ```powershell
  databricks bundle deploy -t dev --profile DEV
  .\utils\deploy\deploy_qualibot.ps1 -AppEnv dev
  ```

  Erreurs possibles (le déploiement s'arrête, relançable après correction) : `run_as` refusé → rôle « Service principal: User » sur
  `job-runner-sa-dev` (Settings ▸ Identity and access ▸ Service principals ▸ job-runner-sa-dev ▸ Permissions) ; création de `dev_proj.qualibot`
  refusée → CREATE SCHEMA sur `dev_proj` ; création du projet Lakebase refusée → droit de création Lakebase. Aucun volume n'est rattaché à
  l'app DEV (rattacher un volume exige MANAGE sur le catalog) : les droits de son SP passent par le job de droits (0.5).
- [ ] **I3. Plannings** : `D_1_Qualibot_Parsing_Pipeline_dev`, `D_1_Qualibot_Lakebase_Export_To_Volume_dev`, `D_1_Qualibot_Apps_Stop_Nightly_Dev`,
  `W_1_Qualibot_Stop_Weekend_Dev`, `W_1_Qualibot_Start_Weekend_Dev` = PAUSED (UI Jobs DEV, filtre `qualibot`) ; `D_1_Qualibot_Lakebase_Import_Uat_To_Dev` reste actif.
- [ ] **I5. Logs de l'app** (`databricks apps logs qualibot --profile DEV`) : `Starting in production mode`, `Database "doccompare" created`
  (1er démarrage seulement), `Lakebase schema ready`, `Lakebase ready` ; pas de `history feature disabled`. Mehdi (hors groupes End-users) ouvre l'app :
  Compare et Chat visibles, pas d'« Access denied » (`CAPS_BYPASS`). Compare : deux révisions → Change Summary, Change Table, exports Excel et PDF ;
  « History » recharge la comparaison ; aperçu « Exact (PDF) » d'un DOCX une fois la copie C finie (elle copie l'archive LibreOffice).

### C. Copie du corpus + index Vector Search (job DEV)

Job `Z_1_Qualibot_Copy_Uat_To_Dev`, déclenchement manuel, serverless, sous **votre** identité (il faut READ VOLUME sur `uat_landingzone.qualibot.staging` ;
le SP DEV ne lit pas ce volume) : `1_import_tables` (snapshot → tables `dev_landingzone.qualibot.*`, rétention 60 jours, Change Data Feed sur les tables de
chunks, archive LibreOffice → volume `doc_compare`) → `2_vector_search_endpoint` (crée l'endpoint `qualibot` s'il manque) → `3_sync_indexes` (crée les index
déclarés et les synchronise — embedding de tout le corpus, peut être long). **Le pipeline de parsing n'est pas lancé.**

```powershell
databricks bundle run copy_uat_to_dev -t dev --profile DEV
```

Une table déjà présente n'est pas écrasée (message « left as is »). Pour forcer : `--params overwrite=true`, puis supprimer l'index de chaque table de
chunks remplacée (UI Vector Search) et relancer : `3_sync_indexes` le recrée. Si le job s'arrête avant la fin de l'embedding (« Still running after
120 min »), ce n'est pas une erreur : la synchronisation continue côté serveur.

### M. Accès développeur pour Mehdi (`mehdi.lamrani@databricks.com`, 2026-10-05)

Objets **gérés par le bundle** (app, projet Lakebase, rôles Postgres, jobs DEV) : la liste de permissions est l'état complet, un droit ajouté à la
main dans l'UI est **effacé au prochain `bundle deploy`** → Mehdi est déclaré dans `databricks.yml`. Objets **hors bundle** (Unity Catalog, endpoint Vector
Search) : à la main, ça reste.

- [ ] **M1. Bundle** : app CAN_MANAGE, projet Lakebase CAN_MANAGE, rôle Postgres `mehdi-lamrani` (SUPERUSER sur la base `doccompare`), CAN_MANAGE sur les jobs DEV :

  ```powershell
  databricks bundle plan -t dev --profile DEV     # permissions en update, create postgres_roles.mehdi, 0 to delete
  databricks bundle deploy -t dev --profile DEV
  ```

- [ ] **M2. Unity Catalog — catalogs, volumes, index** (USE CATALOG demande des droits que vous n'avez pas : on passe par le job de droits, sous le SP DEV) :

  ```powershell
  databricks bundle run grant_app_access_dev -t dev --profile DEV --params app_service_principal=mehdi.lamrani@databricks.com
  ```

- [ ] **M3. Unity Catalog — tables** (éditeur SQL DEV ; vous êtes owner de `dev_landingzone.qualibot` et `dev_proj.qualibot`). **Attention** :
  `dev_landingzone.qualibot` contient aussi la copie des conversations UAT réelles (`chat_messages`, `chat_sessions`, `users`, `feedbacks`…). Choisir :

  ```sql
  -- (a) tout le schema, données de chat UAT comprises
  GRANT ALL PRIVILEGES ON SCHEMA dev_landingzone.qualibot TO `mehdi.lamrani@databricks.com`;

  -- (b) seulement le corpus et l'état du pipeline
  GRANT CREATE TABLE ON SCHEMA dev_landingzone.qualibot TO `mehdi.lamrani@databricks.com`;
  GRANT SELECT, MODIFY ON TABLE dev_landingzone.qualibot.chunks               TO `mehdi.lamrani@databricks.com`;
  GRANT SELECT, MODIFY ON TABLE dev_landingzone.qualibot._pipeline_checkpoint TO `mehdi.lamrani@databricks.com`;
  GRANT SELECT, MODIFY ON TABLE dev_landingzone.qualibot.processed_files      TO `mehdi.lamrani@databricks.com`;
  GRANT SELECT, MODIFY ON TABLE dev_landingzone.qualibot.image_metadata       TO `mehdi.lamrani@databricks.com`;
  GRANT SELECT, MODIFY ON TABLE dev_landingzone.qualibot.parse_manifest       TO `mehdi.lamrani@databricks.com`;
  GRANT SELECT, MODIFY ON TABLE dev_landingzone.qualibot.category_reference   TO `mehdi.lamrani@databricks.com`;

  -- schema projet
  GRANT ALL PRIVILEGES ON SCHEMA dev_proj.qualibot TO `mehdi.lamrani@databricks.com`;
  ```

  Pour `dev_proj`, USE CATALOG : propriétaire `leap-core-service_accounts-dev` (demander, ou étendre le job de M2).
- [ ] **M4. Endpoint Vector Search `qualibot`** — UI : Compute ▸ Vector Search ▸ `qualibot` ▸ Permissions ▸ Mehdi « Can manage ». Ou :

  ```powershell
  $ep = (databricks vector-search-endpoints get-endpoint qualibot --profile DEV -o json | ConvertFrom-Json).id
  databricks permissions update vector-search-endpoints $ep --profile DEV `
    --json '{\"access_control_list\":[{\"user_name\":\"mehdi.lamrani@databricks.com\",\"permission_level\":\"CAN_MANAGE\"}]}'
  ```

- [ ] **M5. Pour qu'il puisse déployer lui-même** (`bundle deploy -t dev`) : rôle « Service principal: User » sur `job-runner-sa-dev` ; accès au repo
  Bitbucket ; le dossier du bundle `/Workspace/Shared/.bundle/qualibot/dev` est déjà accessible à tous.
- [ ] **M6. Job de parsing `D_1_Qualibot_Parsing_Pipeline_dev`** : sa définition est partagée avec UAT/PROD, Mehdi n'y est pas déclaré. Un droit ajouté
  à la main tient jusqu'au prochain `bundle deploy`. Solution durable : l'ajouter au groupe `Role-Project-LEAP-CoreDev` (CAN_MANAGE partout) — action d'un admin.

### B. Bitbucket — pipelines DEV

- [ ] **B1. Créer l'environnement de déploiement « Development »** (Repository settings ▸ Deployments) avec la variable sécurisée `DATABRICKS_TOKEN` =
  PAT du SP DEV `fde6ff28-…` (optionnel : `DATABRICKS_HOST`, défaut `https://dbc-c623749d-731b.cloud.databricks.com`). Créer le PAT (admin du workspace DEV) :

  ```powershell
  databricks token-management create-obo-token fde6ff28-739f-4a41-b61e-604a298c8478 --lifetime-seconds 7776000 --comment "bitbucket qualibot dev" --profile DEV
  ```

  Le SP doit pouvoir déployer le bundle : CAN_MANAGE sur l'app et le projet Lakebase (déclaré), droits UC de P3, écriture sur
  `/Workspace/Shared/.bundle/qualibot/dev`.
- [ ] **B2. Premier essai** : Pipelines ▸ Run pipeline ▸ branche voulue ▸ `deploy-dev-jobs` (ne touche pas l'app), puis `deploy-dev` (bundle + redéploiement de l'app).

### Plus tard — non fait, sur décision

- Réactiver le contrôle par groupe en DEV : passer `CAPS_BYPASS` à `false` (ou retirer la clé) dans le bloc `dev` de `utils/deploy/target_env.json`, redéployer.
- Activer les plannings DEV (parsing quotidien, export Lakebase, stop/start de l'app) : me dire lesquels, je passe `pause_status` / `parsing_schedule_pause` à `UNPAUSED`.
- Si le pipeline de parsing DEV est un jour réactivé (sous le SP DEV) : vérifier que le SP peut synchroniser les index et utiliser l'endpoint `qualibot`
  créés sous votre identité (CAN_MANAGE sur l'endpoint au besoin).
- Copier aussi les images du volume `uat_landingzone.qualibot.images` (seulement si un run de parsing DEV doit retravailler des images déjà extraites).

## Fait

_(rien de confirmé pour l'instant)_
