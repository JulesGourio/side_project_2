# Opérations manuelles — DEV

Ce fichier couvre **uniquement** l'environnement DEV (copie de l'UAT). Pour l'UAT, `qualibot-uat-test` et la PROD, voir `runbooks/uat.md` et `runbooks/prod.md`.

Chaque étape à faire à la main est listée ici, prête à copier-coller, dans l'ordre. Une étape est cochée et datée une fois confirmée ; ce qui n'est pas coché n'est pas confirmé.

Branche : **`feature/chat-vsi-merged-on-impact-search`**. Pas de PR ; le zip à déployer est celui de cette branche.

## Paramètres DEV

| Sujet | Valeur |
|---|---|
| Workspace | DEV — `https://dbc-c623749d-731b.cloud.databricks.com`, profil CLI `DEV` |
| Cible bundle | `dev` (en miroir de `qualibot-uat`) |
| App | `qualibot`, SP de l'app `8e411164-a7e8-46ff-8013-8c56af2c3656` |
| Schéma | `dev_proj.qualibot` : tables, volumes et index du pipeline et de l'app, tables d'évaluation, registre des prompts |
| Suffixe tables / index | aucun (`_test` seulement pendant une validation de parsing, voir `retest.md`) |
| Identité des jobs (`run_as`) | SP DEV `job-runner-sa-dev`, `fde6ff28-739f-4a41-b61e-604a298c8478` |
| Lakebase | projet `qualibot`, branche `production`, base `doccompare` (l'app crée ses tables) |
| Corpus | copie UAT → DEV via le volume `uat_proj.qualibot.staging` : chunks et état du pipeline. **Aucun run complet du parsing** |
| Phase archive avant 2018 | ignorée en DEV (plafond 0, fiches hors RAG, pas de `chunks_full`) |
| Vector Search | endpoint `qualibot`, embeddings `databricks-qwen3-embedding-0-6b` |
| Contrôle par groupe (can_chat / can_compare) | désactivé en DEV seulement (`CAPS_BYPASS=true`, `utils/deploy/target_env.json`) |
| Accès à l'app | groupes de l'UAT, plus les utilisateurs déclarés dans le bundle (`databricks.yml`, `resources/*.yml`), plus le SP DEV |
| Plannings | tous en PAUSED, sauf `lakebase_import_uat_to_dev` |
| Bitbucket | pipelines manuelles `deploy-dev` / `deploy-dev-jobs`, environnement de déploiement **Development** |
| LibreOffice (aperçu PDF exact) | archive copiée depuis l'UAT par le job de copie |
| Modèles Docling | `/Volumes/dev_proj/qualibot/docling_models/docling_models` |

## Ressources DEV

Workspace : https://dbc-c623749d-731b.cloud.databricks.com (id `2865348338307293`).

| Quoi | Lien / commande |
|---|---|
| App `qualibot` (utilisateurs) | https://qualibot-2865348338307293.aws.databricksapps.com |
| App — gestion | https://dbc-c623749d-731b.cloud.databricks.com/apps/qualibot · `databricks apps get qualibot --profile DEV` |
| Schéma `dev_proj.qualibot` | https://dbc-c623749d-731b.cloud.databricks.com/explore/data/dev_proj/qualibot · `databricks tables list dev_proj qualibot --profile DEV` |
| Volumes `doc_compare`, `test`, `images`, `staging`, `docling_models` | `databricks volumes list dev_proj qualibot --profile DEV` |
| Vector Search, endpoint `qualibot`, index `chunks_index` | https://dbc-c623749d-731b.cloud.databricks.com/compute/vector-search/qualibot · `databricks vector-search-indexes get-index dev_proj.qualibot.chunks_index --profile DEV` |
| Lakebase, projet `qualibot`, branche `production`, base `doccompare` | `databricks postgres get-project projects/qualibot --profile DEV` ; rôles : `databricks postgres list-roles projects/qualibot/branches/production --profile DEV` |
| Bundle (fichiers, état) | https://dbc-c623749d-731b.cloud.databricks.com/browse/folders/Workspace/Shared/.bundle/qualibot/dev · `databricks bundle summary -t dev --profile DEV` |
| Jobs | https://dbc-c623749d-731b.cloud.databricks.com/jobs?filter=qualibot |

Jobs du bundle `dev` :

| Job | Déclenchement |
|---|---|
| `Z_1_Qualibot_Copy_Uat_To_Dev` (`copy_uat_to_dev`) | manuel |
| `Z_1_Qualibot_Grant_App_Access_dev` (`grant_app_access_dev`) | manuel |
| `D_1_Qualibot_Lakebase_Import_Uat_To_Dev` (`lakebase_import_uat_to_dev`) | actif |
| `D_1_Qualibot_Lakebase_Export_To_Volume_dev` (`lakebase_export_dev_to_volume`) | PAUSED |
| `D_1_Qualibot_Parsing_Pipeline_dev` (`parsing_pipeline`) | PAUSED |
| `Z_2_Qualibot_Create_Chat_Experiment_dev` (`create_chat_experiment_dev`) | manuel |
| `Z_3_Qualibot_Register_Chat_Prompts_dev` (`register_chat_prompts_dev`) | manuel |
| `D_2_Qualibot_Score_Chat_Traces_dev` (`score_chat_traces_dev`) | PAUSED |
| `D_1_Qualibot_Apps_Stop_Nightly_Dev` (`apps_stop_nightly_dev`) | PAUSED |
| `W_1_Qualibot_Stop_Weekend_Dev` (`qualibot_stop_weekend_dev`) | PAUSED |
| `W_1_Qualibot_Start_Weekend_Dev` (`qualibot_start_weekend_dev`) | PAUSED |

## Prérequis

- [x] **P1. Profil CLI `DEV`** _(2026-10-05)_

  ```powershell
  databricks auth login --host https://dbc-c623749d-731b.cloud.databricks.com --profile DEV
  databricks current-user me --profile DEV
  ```

- [x] **P2. Le SP DEV existe** _(2026-10-05)_ et vous avez le rôle « Service principal: User » dessus (sinon `bundle deploy`
  refuse le `run_as`) :

  ```powershell
  databricks service-principals list --profile DEV --filter "applicationId eq 'fde6ff28-739f-4a41-b61e-604a298c8478'"
  ```

- [ ] **P3. Droits Unity Catalog du SP DEV.** Vérifier ce qu'il a déjà (peut-être via un groupe) :

  ```powershell
  $sp = 'fde6ff28-739f-4a41-b61e-604a298c8478'
  "=== dev_proj";          databricks grants get-effective catalog dev_proj --principal $sp --profile DEV
  "=== dev_proj.qualibot"; databricks grants get-effective schema dev_proj.qualibot --principal $sp --profile DEV
  "=== uat_proj.qualibot"; databricks grants get-effective schema uat_proj.qualibot --principal $sp --profile DEV
  "=== prod_bronze.intraqual";    databricks grants get-effective schema prod_bronze.intraqual --principal $sp --profile DEV
  "=== prod_landingzone.intraqual"; databricks grants get-effective schema prod_landingzone.intraqual --principal $sp --profile DEV
  ```

  Droits attendus : `USE CATALOG` sur `dev_proj`, `uat_proj`, `prod_bronze`, `prod_proj` ; sur `dev_proj.qualibot` :
  `USE SCHEMA`, `CREATE TABLE`, `CREATE VOLUME`, `SELECT`, `MODIFY`, `READ VOLUME`, `WRITE VOLUME` ; `READ VOLUME` sur
  `uat_proj.qualibot.staging` ; `SELECT` sur `prod_bronze.intraqual` et `prod_landingzone.intraqual`, `READ VOLUME` sur
  `prod_landingzone.intraqual.intraqual_documents` (source Intraqual).
  Le niveau schéma `dev_proj.qualibot` peut être donné par son propriétaire :

  ```sql
  GRANT USE SCHEMA, CREATE TABLE, CREATE VOLUME, SELECT, MODIFY, READ VOLUME, WRITE VOLUME
    ON SCHEMA dev_proj.qualibot TO `fde6ff28-739f-4a41-b61e-604a298c8478`;
  ```

  Le reste (`USE CATALOG`, droits sur `uat_proj` / `prod_*`, owner de `dev_proj` : `leap-core-service_accounts-dev`)
  est à demander à un admin des catalogs, pour ce qui manque seulement.

- [x] **P4. Groupes de compte visibles en DEV** _(2026-10-05)_ : `Role-Project-LEAP-CoreAdmin`, `Role-Project-LEAP-CoreDev`,
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

### 00. Volumes, corpus et tables (DEV, détail des commandes)

Tout objet qualibot (tables, volumes, index) vit dans `dev_proj.qualibot`. Les volumes sont créés par le déploiement (00.2), sauf
`docling_models` (00.2a), puis leurs données sont copiées (00.2b). `dev_lab` n'est plus utilisé. L'ordre compte : la copie DEV lit
les tables UAT, donc la copie UAT de `runbooks/uat.md` étape 0 doit être faite avant.

**00.1 — Inventaire DEV (PowerShell, profil `DEV`)**

```powershell
databricks tables list dev_proj qualibot --profile DEV -o json | ConvertFrom-Json | ForEach-Object { $_.full_name }
databricks volumes list dev_proj qualibot --profile DEV -o json | ConvertFrom-Json | ForEach-Object { $_.full_name }
databricks vector-search-indexes list-indexes qualibot --profile DEV -o json | ConvertFrom-Json | ForEach-Object { $_.name }
```

**00.2 — Déployer le bundle DEV (crée le schéma, les volumes, l'app et les jobs)**

```powershell
databricks bundle deploy -t dev --profile DEV
```

Le job `copy_uat_to_dev` et l'app pointent alors sur `dev_proj.qualibot.chunks_index`, qui n'existe pas encore : c'est attendu, le job
le crée (00.4). Les volumes `doc_compare`, `test`, `images` et `staging` sont créés à ce déploiement.

**00.2a — Volume `docling_models` (DEV)** : pas une ressource du bundle en DEV.

```powershell
databricks volumes create dev_proj qualibot docling_models MANAGED --profile DEV
```

**00.2b — Copie des volumes depuis l'ancien emplacement DEV (PowerShell, profil `DEV`)**

```powershell
databricks fs cp -r dbfs:/Volumes/dev_landingzone/qualibot/docling_models dbfs:/Volumes/dev_proj/qualibot/docling_models --profile DEV
databricks fs cp -r dbfs:/Volumes/dev_landingzone/qualibot/doc_compare dbfs:/Volumes/dev_proj/qualibot/doc_compare --profile DEV
databricks fs cp -r dbfs:/Volumes/dev_landingzone/qualibot/test dbfs:/Volumes/dev_proj/qualibot/test --profile DEV
databricks fs cp -r dbfs:/Volumes/dev_landingzone/qualibot/images dbfs:/Volumes/dev_proj/qualibot/images --profile DEV
```

Le volume `staging` n'est pas copié (échanges transitoires).

**00.3 — Export du corpus depuis UAT** : notebook `runbooks/scripts/dev_copy/export_uat_to_staging.py`, lancé dans le workspace UAT
(voir bloc E). Son source par défaut est `uat_proj.qualibot` : la copie UAT de `runbooks/uat.md` étape 0 doit être faite avant.

**00.4 — Import et index (job DEV)**

```powershell
databricks bundle run -t dev copy_uat_to_dev --profile DEV
```

Le job charge les tables dans `dev_proj.qualibot`, crée `dev_proj.qualibot.chunks_index` s'il n'existe pas et le synchronise (tâche
`3_sync_indexes`). Attendre la fin avant la suite.

**00.5 — Droits (job DEV)**

```powershell
databricks bundle run -t dev grant_app_access_dev --profile DEV
```

Accorde les volumes, l'index et les tables de `dev_proj.qualibot` au SP de l'app et au SP des jobs.

**00.6 — Tables chat pour le tableau de bord d'usage**

```powershell
databricks bundle run -t dev lakebase_import_uat_to_dev --profile DEV
```

Recharge `compare_messages`, `compare_feedbacks`, `users`, `chat_*`… dans `dev_proj.qualibot` depuis le volume de staging.

**00.7 — Contrôle**

Chat et comparaison d'impact sur l'URL de l'app DEV, puis :

```powershell
databricks vector-search-indexes get-index dev_proj.qualibot.chunks_index --profile DEV -o json | ConvertFrom-Json | Select-Object name, status
```

**00.8 — `dev_lab` (à supprimer sur accord, les données sont détruites)**

Vérifier, puis supprimer :

```sql
SHOW SCHEMAS IN dev_lab;
SHOW TABLES IN dev_lab.lab_jules;
DROP SCHEMA IF EXISTS dev_lab.lab_jules CASCADE;
```

Si plus aucun schéma utilisateur ne reste dans `dev_lab`, un admin metastore peut supprimer le catalogue : `DROP CATALOG IF EXISTS dev_lab;`.

### 0. Ordre complet du DEV

Toutes les commandes sont ici, dans l'ordre où il faut les lancer. Cocher et dater au fur et à mesure.

- [ ] **0.1 Session PowerShell** (au début de chaque session) :

  ```powershell
  Remove-Item Env:DATABRICKS_TOKEN -ErrorAction SilentlyContinue
  Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
  databricks auth profiles
  ```

  Le profil `DEV` doit apparaître comme valide. Sinon : `databricks auth login --profile DEV`.

- [ ] **0.2 Code** : extraire le zip de la branche dans un **dossier vide** (ne pas extraire par-dessus un ancien dossier). Vérifier :

  ```powershell
  Get-Content server\services\chat\engine\__init__.py -TotalCount 1
  Test-Path server\prompts\chat\system\base.md
  Test-Path resources\chat_mlflow.yml
  ```

  Attendu : `"""Chat engine: answers a question from the Vector Search index.`, puis `True`, `True`.
  Si `bundle validate` dit `dev: no such target` ou `unknown field`, le `databricks.yml` local est abîmé : le remplacer par celui de la branche.

- [ ] **0.3 L'index `chunks_index` est ONLINE avec toutes ses lignes** (l'embedding prend 30 à 60 min) :

  ```powershell
  databricks vector-search-indexes get-index dev_proj.qualibot.chunks_index --profile DEV
  ```

  Chercher `"ready": true` (ou l'état `ONLINE`) et `indexed_row_count` ; éditeur SQL DEV, doit donner le même nombre :

  ```sql
  SELECT count(*) AS chunks FROM dev_proj.qualibot.chunks;
  ```

  Si l'index n'existe pas, le créer (sous votre identité, propriétaire de l'endpoint `qualibot` et de la table `chunks`) ; même
  spécification que l'UAT, la création lance la première synchronisation :

  ```powershell
  $json = '{"name": "dev_proj.qualibot.chunks_index", "endpoint_name": "qualibot", "primary_key": "chunk_id", "index_type": "DELTA_SYNC", "delta_sync_index_spec": {"source_table": "dev_proj.qualibot.chunks", "pipeline_type": "TRIGGERED", "embedding_source_columns": [{"name": "chunk_text", "embedding_model_endpoint_name": "databricks-qwen3-embedding-0-6b"}]}}'
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

- [ ] **0.5 Droits** — le job donne au SP de l'app et au SP des jobs ce qu'ils utilisent (volumes, index, `dev_proj.qualibot`). Les endpoints LLM ne sont jamais accordés : tout utilisateur du workspace peut déjà les interroger.

  ```powershell
  databricks bundle run grant_app_access_dev -t dev --profile DEV
  ```

  Attendu : une ligne `OK:` par droit, aucune `FAILED`. Pour changer ce qui est accordé : les listes `volumes` et `indexes` du job
  `grant_app_access_dev` dans `resources/grants.yml` (`base_parameters`). Une ligne `FAILED` sur `dev_proj` : exécuter à la main
  (éditeur SQL DEV, compte qui peut donner ces droits) puis relancer le job :

  ```sql
  GRANT USE CATALOG ON CATALOG dev_proj TO `fde6ff28-739f-4a41-b61e-604a298c8478`;
  GRANT USE SCHEMA, CREATE TABLE, CREATE FUNCTION, EXECUTE, SELECT, MODIFY ON SCHEMA dev_proj.qualibot TO `fde6ff28-739f-4a41-b61e-604a298c8478`;
  GRANT USE CATALOG ON CATALOG dev_proj TO `Role-Project-LEAP-CoreDev`;
  GRANT USE SCHEMA, CREATE TABLE, SELECT, MODIFY ON SCHEMA dev_proj.qualibot TO `Role-Project-LEAP-CoreDev`;
  ```

  En cas de 403 de l'app : `databricks apps logs qualibot --profile DEV` donne la ligne.

- [ ] **0.6 Tables d'évaluation dans `dev_proj.qualibot`**. Les notebooks de `utils/evaluation/` écrivent dans `dev_proj.qualibot` par défaut : aucune copie à faire. Contrôle, éditeur SQL DEV :

  ```sql
  SHOW TABLES IN dev_proj.qualibot LIKE 'eval_*';
  ```

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
  Si une expérience du même nom existe déjà à votre nom (essai manuel), la supprimer d'abord dans l'UI (Experiments → ⋮ → Delete).

- [ ] **0.9 Prompts dans le registre** — enregistre aussi chaque version en tag `chat_prompt.<clé>` de l'expérience (qui doit exister : 0.8) ;
  l'app lit ces tags, jamais le registre :

  ```powershell
  databricks bundle run register_chat_prompts_dev -t dev --profile DEV
  ```

  Attendu : une ligne `registered version … alias deployed` par prompt (`dev_proj.qualibot.chat_*`). **À refaire après chaque
  déploiement qui change un fichier de `server/prompts/chat/`** : tant que le texte enregistré diffère du fichier, l'app n'attache pas la
  version à la trace (log `prompt … differs from the registered "deployed" version`).

- [ ] **0.10 Déployer l'app** (un seul déploiement pour tout ; l'app crée ses nouvelles colonnes au démarrage — `chat_turns.mlflow_trace_id`,
  `doc_catalog.revision`/`doc_date`, `app_errors.origin`/`fingerprint`/`user_message`… ; `CHAT_MLFLOW_EXPERIMENT` vient de
  `utils/deploy/target_env.json`) :

  ```powershell
  .\utils\deploy\deploy_qualibot.ps1 -AppEnv dev
  ```

  Logs de l'app au démarrage : `Lakebase schema ready`. Une erreur juste avant doit être notée (ligne et message) et transmise au
  développeur. Les colonnes du chat sont décrites dans `server/services/infra/lakebase/schema.md`.

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

  Si la tâche 6 échoue (droits Lakebase du SP), corriger les droits et la relancer : tant que `doc_catalog` est vide, l'app n'a pas de catalogue (REF, titres, liens).

- [ ] **0.12 Contrôle de la recherche** (≈ 1 €, ≈ 10 min) : notebook `/Workspace/Shared/.bundle/qualibot/dev/files/utils/evaluation/retrieval_eval`,
  calcul **serverless**, Run all avec les défauts (`indexes = chat`). Attendu : environ 83 % des documents attendus trouvés. Transmettre le dernier tableau.
  Les résultats vont dans `dev_proj.qualibot.eval_retrieval_runs` ; seules les questions pas encore réussies pour une configuration sont relancées.

- [ ] **0.12b Mesurer « le premier message est seulement traduit »** (option `CHAT_TRANSLATE_ONLY`, **éteinte par défaut** : elle fait perdre
  les développements d'acronymes et de synonymes de la réécriture ; la question « qui est le CEO de Latécoère » ne retrouve plus les documents
  qui disent « PDG »). Même notebook que 0.12, widget `indexes` à `chat,translate|translate=on`, Run all, ≈ 2 €. Transmettre le dernier tableau.
  On ne l'allume (`CHAT_TRANSLATE_ONLY=on`, changer la valeur dans `utils/deploy/target_env.json`, redéployer) que si `translate` est au moins
  aussi bon que `chat` sur les documents attendus trouvés **et** que la question du CEO redonne la bonne réponse.

- [ ] **0.12c Accélérer la réécriture**. Trois leviers dans le code : `CHAT_REWRITE_REASONING_EFFORT=low` (par défaut ; un endpoint
  qui refuse le paramètre est rappelé sans, une seule fois, log `refused reasoning_effort`) ; la recherche reclassée de la question telle quelle part
  **pendant** la réécriture ; un modèle qui ne réfléchit pas (`databricks-gemini-3-1-flash-lite`, à mesurer). Pour comparer qualité **et** vitesse : même
  notebook, widget `indexes` à
  `chat,effort-off|effort=off,minimal|effort=minimal,flash|rewrite=databricks-gemini-3-1-flash-lite`, Run all, ≈ 6 €. Transmettre le dernier tableau
  (documents attendus trouvés **et** `latency_s`) : on garde la configuration la plus rapide qui ne perd rien ; `flash` ne devient la réécriture
  (`CHAT_REWRITE_ENDPOINT`) que s'il ne perd rien.

- [ ] **0.13 Tests dans l'app** — une question normale en AS, une en IS, une avec un numéro de document (ex. `MI-14242`), une hors sujet (attendu :
  refus poli), une en espagnol ; « donne-moi le lien de l'OPEX Sharepoint » (→ l'URL de INAQ-742) ; 👍 sur une réponse et 👎 sur une autre ;
  un seul onglet **Chat** ; les documents cités sont bien de la division demandée. Bouton **Télécharger → Response — Markdown** (le fichier finit
  par la liste **Sources** `[1] REF — titre — Version … — lien`), PDF du chat (**Response — PDF**, **Full session — PDF**) et du compare
  (**Download PDF** du Change Summary et du résumé d'un document). « Judge Impacted Docs » dans Compare sur deux révisions connues. La question
  NDT (« quels documents parlent des exigences de qualification CND ? ») : pas de « version not stated », pastilles `REF · B`, titre et « Version B
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
  FROM app_errors ORDER BY id DESC LIMIT 20;
  ```

  Attendu : une ligne par question (`ok` ou `degraded`, `aborted` si l'onglet est fermé avant la fin), une trentaine de passages par tour.
  Transmettre : une capture d'une trace ouverte, les trois résultats SQL, une capture de la réponse NDT. Si rien n'apparaît dans MLflow : les lignes
  `mlflow_turns:` des logs de l'app. Pour tester le message de repli « Qualibot is a bit tired… » : mettre temporairement un index inexistant
  dans `CHAT_INDEX` pour `dev` (`utils/deploy/target_env.json`), redéployer, poser une question (attendu : une ligne `app_errors` `stage = search`),
  puis remettre la valeur et redéployer.

- [ ] **0.14 Notation des traces, à blanc puis réelle** — d'abord l'estimation (ne juge rien) :

  ```powershell
  databricks bundle run score_chat_traces_dev -t dev --profile DEV
  ```

  UI → Jobs → `D_2_Qualibot_Score_Chat_Traces_dev` → dernier run : par juge, le nombre de traces lues et `estimated input tokens`. Transmettre
  ces lignes. Puis, hors des heures d'usage du chat (le juge partage les quotas de tokens de l'endpoint Luna), le run réel (plafonds :
  `max_input_tokens` = 2 000 000, 2 appels en parallèle, 2 appels par seconde, reprises sur 429) :

  ```powershell
  databricks bundle run score_chat_traces_dev -t dev --profile DEV --params dry_run=false
  ```

  Transmettre le tableau final du notebook (table `dev_proj.qualibot.chat_trace_scores`) et les lignes `failed` de « Quality Checks ».
  Quand les juges sont validés : baisser les échantillons (`TARGET_SAMPLE_RATES` dans `utils/evaluation/chat_scorers.py`) et activer le planning (PAUSED aujourd'hui).

- [ ] **0.15 Nettoyage** (seulement après 0.12 et 0.13 réussis) :
  - garder dans `dev_proj.qualibot` tout ce que liste `SHOW TABLES IN dev_proj.qualibot;`, sauf les tables de test `*_test`. Les tables
    d'évaluation (`eval_*`, `eval_golden_questions`, `eval_synthetic_questions`, `eval_feedback_failures`) sont les défauts des notebooks :
    ne pas les supprimer.

### T. Combien de passages garder ?

Aujourd'hui : 3 requêtes × (12 passages reclassés + 10 bruts), fusionnés sans plafond. Les défauts du code ne changent pas tant que ce test
n'a pas tranché.

- [ ] **T1. Recherche seule** (≈ 4 €, ≈ 30 min) : notebook `utils/evaluation/retrieval_eval.py`, widget `indexes` =

  ```text
  chat,rerank-only|raw=0,raw5|raw=5,cap40|cap=40,cap25|cap=25,rerank8-only|rerank=8|raw=0
  ```

  puis Run all. Transmettre les deux derniers tableaux (par source et global : documents trouvés, dans les 5 premiers, tokens de contexte).
- [ ] **T2. Réponses** (≈ 2 €, ≈ 20 min, après T1) : notebook `pairwise_answers.py`, widgets `indexes` = la même liste que T1, `reference` =
  `databricks-gpt-6-luna@chat`, `contenders` = `databricks-gpt-6-luna@<label retenu en T1>`, `eval_id` = `passages-<label>`. Transmettre les trois tableaux.

### U. Tests de charge

Mesures et configuration retenue : the chat measurement log in `archive/docs/chat_tests.md`, section 5.8. Le chat tient 40 questions simultanées avec la file Vector Search
commune (`server/services/infra/vs_gate.py`, 8 requêtes en cours au plus, relances sur 429).

- [ ] **U1. Chat** : app DEV démarrée, notebook `utils/evaluation/load_test_chat.py` en serverless, Run all (défauts : paliers de 5, 10, 20, 40, 80
  questions en cours, ≈ 620 questions, ≈ 2 €, 30 à 60 min ; s'arrête au premier palier où plus de la moitié échoue). La cellule « Mode » dit si le test passe
  par l'app (`MODE = app`) ou, si l'app refuse le jeton du notebook, par le moteur appelé dans le notebook (`MODE = engine`). Transmettre les deux
  tableaux de la dernière cellule, la ligne `MODE = …`, et les lignes `chat_llm:` des logs de l'app (bascule vers le modèle de secours, relances).
- [ ] **U2. Vector Search seul** (≈ 20 min, aucun appel LLM) : notebook `utils/evaluation/load_test_vector_search.py`, serverless, Run all. Transmettre la
  sortie de la cellule « endpoint and index » (type d'endpoint, `scaling_info`) et les tableaux de la dernière cellule.

### E. Export du corpus UAT (workspace UAT, run ponctuel)

Lecture seule sur les tables UAT ; écrit uniquement dans `/Volumes/uat_proj/qualibot/staging/dev_copy/`. La cible `qualibot-uat` du bundle
n'est pas touchée. Tables exportées : `chunks`, `parsing_checkpoint`, `parsed_documents`, `image_metadata`, `parse_manifest`,
`category_reference`, plus l'archive LibreOffice. Pas de tables d'archive avant 2018.

- [x] **E1. Importer le notebook dans votre dossier et lancer le run** _(2026-10-05)_

  ```powershell
  databricks workspace mkdirs /Users/jules.gourio.external@latecoere.aero/qualibot_dev_copy --profile UAT
  databricks workspace import /Users/jules.gourio.external@latecoere.aero/qualibot_dev_copy/export_uat_to_staging `
    --file runbooks\scripts\dev_copy\export_uat_to_staging.py --format SOURCE --language PYTHON --overwrite --profile UAT
  databricks jobs submit --json "@runbooks/scripts/dev_copy/export_uat_submit.json" --profile UAT
  ```

  Compute serverless, sous votre identité. Si l'écriture Delta dans le volume est refusée, relancer en Parquet : ajouter dans le JSON, sous
  `notebook_task`, `"base_parameters": {"FORMAT": "parquet"}` (l'import DEV lit le format dans le manifeste).

- [ ] **E2. Vérifier le manifeste** (nombre de lignes par table), depuis UAT puis **depuis DEV** (preuve que DEV lit ce volume) :

  ```powershell
  databricks fs cat dbfs:/Volumes/uat_proj/qualibot/staging/dev_copy/manifest.json --profile UAT
  databricks fs ls  dbfs:/Volumes/uat_proj/qualibot/staging/dev_copy/tables --profile DEV
  ```

### I. Infrastructure DEV (`bundle deploy -t dev`)

Crée l'app `qualibot`, le projet Lakebase `qualibot` (base `doccompare`), le schéma `dev_proj.qualibot`, les volumes, les rôles Postgres et les jobs.

- [x] **I1b. Prévisualiser sans rien modifier** _(2026-10-05)_ :

  ```powershell
  python utils/deploy/render_target_config_env.py dev target_config.env
  databricks bundle validate -t dev --profile DEV
  databricks bundle plan -t dev --profile DEV
  ```

- [x] **I2. Déployer l'infra, puis le code de l'app** sous votre identité (profil `DEV`) _(2026-10-05)_ : vous devenez owner de ce qui est créé ; le SP DEV a
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
- [ ] **I5. Logs de l'app** (`databricks apps logs qualibot --profile DEV`) : `Database "doccompare" created` (1er démarrage seulement),
  `Lakebase schema ready`, `Lakebase ready` ; pas de `history feature disabled`. Un développeur hors groupes d'utilisateurs ouvre l'app :
  Compare et Chat visibles, pas d'« Access denied » (`CAPS_BYPASS`). Compare : deux révisions → Change Summary, Change Table, exports Excel et PDF ;
  « History » recharge la comparaison ; aperçu « Exact (PDF) » d'un DOCX une fois la copie de l'archive LibreOffice finie.

### C. Copie du corpus + index Vector Search (job DEV)

Job `Z_1_Qualibot_Copy_Uat_To_Dev`, déclenchement manuel, serverless, sous **votre** identité (il faut READ VOLUME sur `uat_proj.qualibot.staging` ;
le SP DEV ne lit pas ce volume) : `1_import_tables` (snapshot → tables `dev_proj.qualibot.*`, rétention 60 jours, Change Data Feed sur les tables de
chunks, archive LibreOffice → volume `doc_compare`) → `2_vector_search_endpoint` (crée l'endpoint `qualibot` s'il manque) → `3_sync_indexes` (crée les index
déclarés et les synchronise — embedding de tout le corpus, peut être long). **Le pipeline de parsing n'est pas lancé.**

```powershell
databricks bundle run copy_uat_to_dev -t dev --profile DEV
```

Une table déjà présente n'est pas écrasée (message « left as is »). Pour forcer : `--params overwrite=true`, puis supprimer l'index de chaque table de
chunks remplacée (UI Vector Search) et relancer : `3_sync_indexes` le recrée. Si le job s'arrête avant la fin de l'embedding (« Still running after
120 min »), ce n'est pas une erreur : la synchronisation continue côté serveur.

### B. Bitbucket — pipelines DEV

- [ ] **B1. Créer l'environnement de déploiement « Development »** (Repository settings ▸ Deployments) avec la variable sécurisée `DATABRICKS_TOKEN` =
  PAT du SP DEV `fde6ff28-…` (optionnel : `DATABRICKS_HOST`, défaut `https://dbc-c623749d-731b.cloud.databricks.com`). Créer le PAT (admin du workspace DEV) :

  ```powershell
  databricks token-management create-obo-token fde6ff28-739f-4a41-b61e-604a298c8478 --lifetime-seconds 7776000 --comment "bitbucket qualibot dev" --profile DEV
  ```

  Le SP doit pouvoir déployer le bundle : CAN_MANAGE sur l'app et le projet Lakebase (déclaré), droits UC de P3, écriture sur
  `/Workspace/Shared/.bundle/qualibot/dev`.
- [ ] **B2. Premier essai** : Pipelines ▸ Run pipeline ▸ branche voulue ▸ `deploy-dev-jobs` (ne touche pas l'app), puis `deploy-dev` (bundle + redéploiement de l'app).

### M. Accès développeur supplémentaire (`mehdi.lamrani@databricks.com`)

Objets **gérés par le bundle** (app, projet Lakebase, rôles Postgres, jobs DEV) : la liste de permissions est l'état complet, un droit ajouté à la
main dans l'UI est **effacé au prochain `bundle deploy`** → le développeur est déclaré dans `databricks.yml` et `resources/*.yml`. Objets **hors bundle**
(Unity Catalog, endpoint Vector Search) : à la main, ça reste.

- [ ] **M1. Bundle** : app CAN_MANAGE, projet Lakebase CAN_MANAGE, rôle Postgres `mehdi-lamrani` (SUPERUSER sur la base `doccompare`), CAN_MANAGE sur les jobs DEV :

  ```powershell
  databricks bundle plan -t dev --profile DEV     # permissions en update, create postgres_roles.mehdi, 0 to delete
  databricks bundle deploy -t dev --profile DEV
  ```

- [ ] **M2. Unity Catalog — catalogs, volumes, index** (USE CATALOG demande des droits que vous n'avez pas : on passe par le job de droits, sous le SP DEV) :

  ```powershell
  databricks bundle run grant_app_access_dev -t dev --profile DEV --params app_service_principal=mehdi.lamrani@databricks.com
  ```

- [ ] **M3. Unity Catalog — tables** (éditeur SQL DEV ; owner de `dev_proj.qualibot`). **Attention** :
  `dev_proj.qualibot` contient aussi la copie des conversations UAT réelles (`chat_messages`, `chat_sessions`, `users`, `feedbacks`…). Choisir :

  ```sql
  -- (a) tout le schema, données de chat UAT comprises
  GRANT ALL PRIVILEGES ON SCHEMA dev_proj.qualibot TO `mehdi.lamrani@databricks.com`;

  -- (b) seulement le corpus et l'état du pipeline
  GRANT CREATE TABLE ON SCHEMA dev_proj.qualibot TO `mehdi.lamrani@databricks.com`;
  GRANT SELECT, MODIFY ON TABLE dev_proj.qualibot.chunks               TO `mehdi.lamrani@databricks.com`;
  GRANT SELECT, MODIFY ON TABLE dev_proj.qualibot.parsing_checkpoint TO `mehdi.lamrani@databricks.com`;
  GRANT SELECT, MODIFY ON TABLE dev_proj.qualibot.parsed_documents      TO `mehdi.lamrani@databricks.com`;
  GRANT SELECT, MODIFY ON TABLE dev_proj.qualibot.image_metadata       TO `mehdi.lamrani@databricks.com`;
  GRANT SELECT, MODIFY ON TABLE dev_proj.qualibot.parse_manifest       TO `mehdi.lamrani@databricks.com`;
  GRANT SELECT, MODIFY ON TABLE dev_proj.qualibot.category_reference   TO `mehdi.lamrani@databricks.com`;

  -- schema projet
  GRANT ALL PRIVILEGES ON SCHEMA dev_proj.qualibot TO `mehdi.lamrani@databricks.com`;
  ```

  Pour `dev_proj`, USE CATALOG : propriétaire `leap-core-service_accounts-dev` (demander, ou étendre le job de M2).

- [ ] **M4. Endpoint Vector Search `qualibot`** — UI : Compute ▸ Vector Search ▸ `qualibot` ▸ Permissions ▸ le développeur « Can manage ». Ou :

  ```powershell
  $ep = (databricks vector-search-endpoints get-endpoint qualibot --profile DEV -o json | ConvertFrom-Json).id
  databricks permissions update vector-search-endpoints $ep --profile DEV `
    --json '{\"access_control_list\":[{\"user_name\":\"mehdi.lamrani@databricks.com\",\"permission_level\":\"CAN_MANAGE\"}]}'
  ```

- [ ] **M5. Pour qu'il puisse déployer lui-même** (`bundle deploy -t dev`) : rôle « Service principal: User » sur `job-runner-sa-dev` ; accès au repo
  Bitbucket ; le dossier du bundle `/Workspace/Shared/.bundle/qualibot/dev` est déjà accessible à tous.
- [ ] **M6. Job de parsing `D_1_Qualibot_Parsing_Pipeline_dev`** : sa définition est partagée avec UAT/PROD, le développeur n'y est pas déclaré. Un droit ajouté
  à la main tient jusqu'au prochain `bundle deploy`. Solution durable : l'ajouter au groupe `Role-Project-LEAP-CoreDev` (CAN_MANAGE partout) — action d'un admin.

### Q. Pipeline de parsing : statuts, tentatives et modèles d'image

Comportement du code à valider sur DEV :

- **Documents en attente d'images** : un document sans texte mais avec des images à décrire reçoit le statut `IMAGES_PENDING` (`3_parse` ne le re-parse plus chaque jour). La tâche `4_describe_images` le fait passer à `SUCCESS` (ou `SKIPPED_EMPTY_IMAGES`) dès que sa dernière image est décrite.
- **Plafond de tentatives** : une image est tentée au plus une fois par run, et au plus `PARSING_IMAGE_MAX_ATTEMPTS` (10) runs. Un run compte comme tentative quand l'image échoue pour une autre raison qu'un 429 (quota) ; un 429 ne compte jamais. Au 10e échec elle passe à `FAILED_LIMIT` : elle n'est plus retentée automatiquement, elle apparaît dans le résumé du run avec son `last_error_kind`. Un run en mode `full` remet le compteur à 0 et repasse les images en `PENDING`.
- **Disjoncteur** : après 5 images d'affilée en échec sur un même modèle dans un run (timeout, 5xx, erreur de transport, réponse vide), ce modèle est écarté pour le reste du run. Une erreur 429 ne compte ni pour le disjoncteur ni pour le plafond.
- **Modèles** : `databricks-gpt-5-6-luna` accepte les images (modèle primaire de la description). `databricks-gpt-6-luna` n'est pas vérifié pour les images : le script `check_vision_endpoints.py` le teste.

Ce bloc ne touche pas aux données ; seul Q3 lance le pipeline (GPU, plusieurs heures selon le corpus).

- [ ] **Q1. Vérifier les modèles de vision** (lecture seule, quelques tokens par image) — depuis la racine du dépôt :

  ```powershell
  python utils/evaluation/check_vision_endpoints.py --profile DEV --images "<chemin du dossier d'images>"
  ```

  Remplacer `<chemin du dossier d'images>` par un dossier local de png / jpg / jpeg. Le script affiche une ligne par (modèle, image) et un
  résumé `ok / fail` par modèle. Pour n'interroger que certains modèles, ajouter `--endpoints databricks-gpt-5-6-luna,databricks-gpt-6-luna`.

- [ ] **Q2. Déployer le bundle DEV** (le job de parsing reste PAUSED) :

  ```powershell
  databricks bundle deploy -t dev --profile DEV
  ```

- [ ] **Q3. Lancer le pipeline de parsing DEV** (toutes les tâches dans l'ordre : `1_categories` → `2_manifest` → `3_parse` → `4_describe_images` → `5_sync_index` → `6_update_kb_metadata`) :

  ```powershell
  databricks bundle run -t dev parsing_pipeline --profile DEV
  ```

  Dans le job `D_1_Qualibot_Parsing_Pipeline_dev`, ouvrir les logs de `3_parse` (ligne `[INCREMENTAL] … IDDOCs already terminal`) et de `4_describe_images`
  (lignes `Endpoints  : …`, `Description summary:`, `Remaining to describe:`).

- [ ] **Q4. Contrôler les statuts des documents** — éditeur SQL du workspace DEV (warehouse serverless), une requête à la fois :

  ```sql
  SELECT parse_status, COUNT(*) AS documents
  FROM dev_proj.qualibot.parsed_documents
  GROUP BY parse_status
  ORDER BY documents DESC;
  ```

  ```sql
  SELECT COUNT(*) AS parked_without_retry_image
  FROM dev_proj.qualibot.parsed_documents p
  WHERE p.parse_status = 'IMAGES_PENDING'
    AND NOT EXISTS (
      SELECT 1 FROM dev_proj.qualibot.image_metadata i
      WHERE i.IDDOC = p.IDDOC AND i.status IN ('PENDING', 'ERROR')
    );
  ```

- [ ] **Q5. Contrôler les images** — mêmes règles :

  ```sql
  SELECT status, last_error_kind, COUNT(*) AS images, MAX(attempts) AS max_attempts
  FROM dev_proj.qualibot.image_metadata
  GROUP BY status, last_error_kind
  ORDER BY status, images DESC;
  ```

  ```sql
  SELECT COUNT(*) AS failed_limit
  FROM dev_proj.qualibot.image_metadata
  WHERE status = 'FAILED_LIMIT';
  ```

  ```sql
  SELECT COUNT(*) AS pending_past_limit
  FROM dev_proj.qualibot.image_metadata
  WHERE status = 'PENDING' AND attempts >= 10;
  ```

  ```sql
  SELECT IDDOC, image_id, last_error_kind, attempts, volume_path
  FROM dev_proj.qualibot.image_metadata
  WHERE status = 'FAILED_LIMIT'
  ORDER BY attempts DESC
  LIMIT 50;
  ```

**Résultats attendus** (une ligne par contrôle) :

- Q3, logs de `3_parse` : le nombre de `IDDOCs already terminal` inclut les documents `IMAGES_PENDING` (ils ne sont pas re-parsés).
- Q3, logs de `4_describe_images` : `Endpoints  : databricks-gpt-5-6-luna -> …` (la chaîne lue depuis le job) et une ligne `Description summary:`.
- Q4, première requête : `IMAGES_PENDING` apparaît s'il reste des documents sans texte avec des images à décrire ; aucun `EMPTY_TEXT` ne doit rester après le run.
- Q4, seconde requête : `parked_without_retry_image` = 0 (un document en `IMAGES_PENDING` a toujours une image `PENDING` ou `ERROR`).
- Q5, première requête : `PENDING` avec `last_error_kind` vide ou `rate_limit` / `timeout` / `http_5xx` / `circuit_open` selon la panne du moment ; `FAILED_LIMIT` ne doit pas apparaître avant le 10e run en échec hors 429.
- Q5, `pending_past_limit` = 0.
- Q5, `failed_limit` : 0 au premier run ; sinon la liste de la dernière requête donne les images à regarder (le `last_error_kind` dit pourquoi). Elles ne sont pas retentées : seul un run `full` les remet en attente.
- Un document dont toutes les images sont en `FAILED_LIMIT` passe à `SKIPPED_EMPTY_IMAGES` (aucune image exploitable) ; la cause est dans `image_metadata`.

### R0. Renommer les tables (à faire, avant le déploiement de cette branche sur DEV)

Ordre impératif : 1. SQL Delta ci-dessous, 2. migration Lakebase, 3. déploiement du code, 4. bloc R. Si le code est déployé avant les renommages, l'app crée des tables vides sous les nouveaux noms et l'historique reste sous les anciens.

1. Éditeur SQL DEV, **une instruction à la fois**. Une table absente donne `TABLE_OR_VIEW_NOT_FOUND` : passer à la suivante.

```sql
ALTER TABLE dev_proj.qualibot.processed_files RENAME TO parsed_documents;
ALTER TABLE dev_proj.qualibot.chunks_archive_notices RENAME TO archive_notices;
ALTER TABLE dev_proj.qualibot._pipeline_checkpoint RENAME TO parsing_checkpoint;
ALTER TABLE dev_proj.qualibot._rag_exclusions RENAME TO rag_exclusions;
ALTER TABLE dev_proj.qualibot.qualibot_eval_golden RENAME TO eval_golden_questions;
ALTER TABLE dev_proj.qualibot.synthetic_retrieval_questions RENAME TO eval_synthetic_questions;
ALTER TABLE dev_proj.qualibot.feedback_failure_cases RENAME TO eval_feedback_failures;
ALTER TABLE dev_proj.qualibot.eval_pairwise_runs_cache RENAME TO eval_pairwise_cache;
ALTER TABLE dev_proj.qualibot.eval_load_runs RENAME TO eval_load_chat_runs;
ALTER TABLE dev_proj.qualibot.eval_vs_load_runs RENAME TO eval_load_vector_search_runs;
SHOW TABLES IN dev_proj.qualibot;
```

2. Lakebase DEV, PowerShell à la racine du dépôt (les renommages sont les premières migrations) :

```powershell
python runbooks/scripts/lakebase/migrate_lakebase.py --env DEV
```

Contrôle, éditeur SQL Lakebase DEV : `SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' ORDER BY 1;` doit lister `compare_messages`, `compare_feedbacks`, `app_errors`, `catalog_metadata` et pas `messages`, `feedbacks`, `errors`, `knowledge_base_metadata`.

3. Relancer le job de droits DEV (`grant_app_access_dev`, runbook 0.5) : sa liste de tables porte les nouveaux noms.

### R. Renommer trois colonnes booléennes de `parsed_documents` (à faire)

Le code lit `is_in_rag`, `is_filtered_by_date` et `is_chunks_truncated` (préfixe `is_` pour les booléens, `CLAUDE.md`). Les tables DEV gardent les anciens noms tant que ce bloc n'est pas passé.

Ordre impératif : **1. SQL ci-dessous, 2. déploiement du code, 3. run de parsing ou de `copy_uat_to_dev`**. Si le code arrive avant le SQL, la tâche `2_manifest` ajoute les nouvelles colonnes à côté des anciennes (`merge_schema`) et les lignes se retrouvent avec deux colonnes pour une même information.

Dans l'éditeur SQL du workspace DEV (SQL warehouse, compte du déployeur) :

```sql
SHOW COLUMNS IN dev_proj.qualibot.parsed_documents;
```

Attendu : `include_in_rag`, `filtered_by_date`, `chunks_truncated` présentes et `is_*` absentes. Si un nom `is_*` existe déjà, arrêter et le signaler.

```sql
ALTER TABLE dev_proj.qualibot.parsed_documents SET TBLPROPERTIES (
  'delta.columnMapping.mode' = 'name',
  'delta.minReaderVersion' = '2',
  'delta.minWriterVersion' = '5'
);
ALTER TABLE dev_proj.qualibot.parsed_documents RENAME COLUMN include_in_rag TO is_in_rag;
ALTER TABLE dev_proj.qualibot.parsed_documents RENAME COLUMN filtered_by_date TO is_filtered_by_date;
ALTER TABLE dev_proj.qualibot.parsed_documents RENAME COLUMN chunks_truncated TO is_chunks_truncated;
SHOW COLUMNS IN dev_proj.qualibot.parsed_documents;
```

Attendu après : les trois `is_*` présentes, les anciens noms absents.

- Le `copy_uat_to_dev` recrée les tables depuis l'UAT, qui a encore les anciens noms : relancer ce bloc après chaque copie tant que l'UAT n'a pas été migré (`runbooks/uat.md`, à faire au moment de la bascule).
- Même migration pour la table de test `uat_proj.qualibot.parsed_documents_test` (`qualibot-uat-test`), avant son run de validation : remplacer `dev_proj.qualibot.parsed_documents` par `uat_proj.qualibot.parsed_documents_test` dans les quatre instructions.
- Les colonnes ne sont pas dans `chunks` ni dans l'index `chunks_index` : pas de resynchronisation d'index à prévoir.

### Plus tard — non fait, sur décision

- Traces MLflow en tables Delta Unity Catalog : adapté à un gros volume, requêtable en SQL ; exige MLflow ≥ 3.11.1 dans l'app et un entrepôt SQL ; non modifiable ensuite pour cette expérience ; non testé.
- Réactiver le contrôle par groupe en DEV : passer `CAPS_BYPASS` à `false` (ou retirer la clé) dans le bloc `dev` de `utils/deploy/target_env.json`, redéployer.
- Activer les plannings DEV (parsing quotidien, export Lakebase, stop/start de l'app) : passer les plannings `PAUSED` à `UNPAUSED` (`parsing_schedule_pause` dans `databricks.yml`, `pause_status` dans `resources/*.yml`), puis redéployer.
- Si le pipeline de parsing DEV est réactivé (sous le SP DEV) : vérifier que le SP peut synchroniser les index et utiliser l'endpoint `qualibot` créés sous votre identité (CAN_MANAGE sur l'endpoint au besoin).
- Copier aussi les images du volume `uat_proj.qualibot.images` (seulement si un run de parsing DEV doit retravailler des images déjà extraites).
- Ne pas toucher ici à `qualibot-uat`, `qualibot-uat-test` ou `prod` : voir `runbooks/uat.md` (dont le bloc D, qui déploie la branche du chatbot en UAT) et `runbooks/prod.md`. Il faut d'abord que le DEV soit validé.

## Fait

- P1, P2, P4 : profil CLI, SP DEV, groupes visibles (2026-10-05).
- E1 : export du corpus UAT (2026-10-05).
- I1b, I2 : prévisualisation et déploiement de l'infrastructure DEV (2026-10-05).
