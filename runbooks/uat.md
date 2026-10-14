# Opérations manuelles

Claude n'a pas accès à Databricks : chaque étape à faire à la main (côté
machine de déploiement ou UI Databricks) est listée ici, prête à
copier-coller. Cocher et dater une fois faite.

PowerShell, depuis la racine du projet sur la machine de déploiement.
Tout le code est sur la branche **`audit/doc-compare`** (= `main` + audit de la
comparaison + phase de test archive). Le zip à déployer est celui de cette
branche tant qu'elle n'est pas fusionnée.

Mis à jour le 2026-10-09 (journal Lakebase du chat : D5.8, D5.8b ; révisions et alertes : D5.8c, D5.8d). Rien de ce qui suit n'a encore
été confirmé comme fait.

La mise en place de l'environnement **DEV** a son propre fichier : `runbooks/dev.md`.

> **Branche `feature/chat-vsi-merged-on-impact-search` (2026-10-08) : ne la déployer sur
> `qualibot-uat` / `qualibot-uat-test` qu'au bloc D5, dans son ordre.** Son code attend des tables
> et un index **sans suffixe** (`chunks`, `chunks_index`, `_pipeline_checkpoint`…) et n'a plus de
> Knowledge Assistant. Un `bundle deploy` de cette branche avant D5 ferait repartir le pipeline
> UAT de tables vides (re-parsing GPU de tout le corpus) et l'app chercherait un index qui
> n'existe pas encore. Les blocs A à C ci-dessous restent sur `audit/doc-compare`.
> Exception depuis le 2026-10-12 : `qualibot-uat-test` sert de répétition de la bascule (tables
> `uat_proj.qualibot.*_test`, rien dans `uat_proj.qualibot`) ; étapes données dans le chat.

> **Nettoyage du code (2026-10-08)** : `utils/databricks_ops/` n'existe plus, le notebook `3_parse` est découpé, le client allégé. Tout ce qu'il faut rejouer est dans [`RETEST_CLEANUP.md`](RETEST_CLEANUP.md) (DEV d'abord).

## Vue d'ensemble

| Bloc | Quoi | Cible | Touche le chatbot ? |
|---|---|---|---|
| A | Valider l'app (impact search v2 + audit comparaison) | `qualibot-uat-test` | non |
| B | Pipeline : phase de test « documents d'avant 2018 » | `qualibot-uat` (job seul) | non |
| C | Mettre l'app en service sur `qualibot-uat` | `qualibot-uat` | non |
| D | Activations après validation (index complet, fiches, prompt) | `qualibot-uat` | **oui** pour D3/D4 |

A et B sont indépendants et peuvent se faire dans n'importe quel ordre.
D ne se fait qu'après B, étape par étape, sur décision explicite.

## À faire

### 0. Tables, volumes et index hors landingzone : migration vers `uat_proj.qualibot`

Règle : aucun objet qualibot (table, volume, index) dans `*_landingzone`. Tout vit dans `uat_proj.qualibot`. Les volumes ne se déplacent pas : on les crée dans `uat_proj` puis on copie leurs données (0.2b). Rien ne touche à l'app tant que tu n'as pas donné ton accord pour le déploiement (0.7).

**0.1 — Inventaire (PowerShell, profil `UAT`)**

```powershell
databricks tables list uat_landingzone qualibot --profile UAT -o json | ConvertFrom-Json | ForEach-Object { $_.full_name }
databricks volumes list uat_landingzone qualibot --profile UAT -o json | ConvertFrom-Json | ForEach-Object { $_.full_name }
databricks tables list uat_proj qualibot --profile UAT -o json | ConvertFrom-Json | ForEach-Object { $_.full_name }
databricks volumes list uat_proj qualibot --profile UAT -o json | ConvertFrom-Json | ForEach-Object { $_.full_name }
databricks vector-search-indexes list-indexes qualibot --profile UAT -o json | ConvertFrom-Json | ForEach-Object { $_.name }
```

**0.2 — Clonage des tables (SQL, éditeur SQL du workspace UAT, un utilisateur qui lit `uat_proj` et crée dans `uat_proj.qualibot`)**

Pour chaque table listée en 0.1 dans `uat_proj.qualibot` (sauf les volumes et les `*_v1`), une ligne `DEEP CLONE`. Si la table sans suffixe n'existe pas, cloner la version `_v1` correspondante. Liste de référence (issue du code) :

```sql
CREATE SCHEMA IF NOT EXISTS uat_proj.qualibot;

CREATE TABLE IF NOT EXISTS uat_proj.qualibot._pipeline_checkpoint   DEEP CLONE uat_landingzone.qualibot._pipeline_checkpoint_v1;
CREATE TABLE IF NOT EXISTS uat_proj.qualibot.processed_files        DEEP CLONE uat_landingzone.qualibot.processed_files_v1;
CREATE TABLE IF NOT EXISTS uat_proj.qualibot.image_metadata         DEEP CLONE uat_landingzone.qualibot.image_metadata_v1;
CREATE TABLE IF NOT EXISTS uat_proj.qualibot.parse_manifest         DEEP CLONE uat_landingzone.qualibot.parse_manifest_v1;
CREATE TABLE IF NOT EXISTS uat_proj.qualibot.category_reference     DEEP CLONE uat_landingzone.qualibot.category_reference_v1;
CREATE TABLE IF NOT EXISTS uat_proj.qualibot.parsing_run_health     DEEP CLONE uat_landingzone.qualibot.parsing_run_health_v1;
CREATE TABLE IF NOT EXISTS uat_proj.qualibot.document_change_log    DEEP CLONE uat_landingzone.qualibot.document_change_log_v1;
CREATE TABLE IF NOT EXISTS uat_proj.qualibot.audit_files_unified    DEEP CLONE uat_landingzone.qualibot.audit_files_unified_v1;
CREATE TABLE IF NOT EXISTS uat_proj.qualibot.chunks                 DEEP CLONE uat_landingzone.qualibot.chunks_v1;
CREATE TABLE IF NOT EXISTS uat_proj.qualibot.chunks_archive         DEEP CLONE uat_landingzone.qualibot.chunks_archive_v1;
CREATE TABLE IF NOT EXISTS uat_proj.qualibot.chunks_archive_notices DEEP CLONE uat_landingzone.qualibot.chunks_archive_notices_v1;
CREATE TABLE IF NOT EXISTS uat_proj.qualibot.chunks_full            DEEP CLONE uat_landingzone.qualibot.chunks_full_v1;
CREATE TABLE IF NOT EXISTS uat_proj.qualibot.synthetic_retrieval_questions_v2 DEEP CLONE uat_landingzone.qualibot.synthetic_retrieval_questions_v2;
CREATE TABLE IF NOT EXISTS uat_proj.qualibot.feedback_failure_cases DEEP CLONE uat_landingzone.qualibot.feedback_failure_cases;
```

Les tables chat (`chat_messages`, `chat_sessions`, `chat_feedbacks`, `chat_turns`, `chat_retrieved_chunks`, `errors`) ne se clonent pas : elles sont rechargées depuis Lakebase à l'étape 0.5.

Contrôle des volumes après clonage (doit donner les mêmes nombres que la source, à la date de copie près) :

```sql
SELECT 'chunks' AS t, (SELECT count(*) FROM uat_landingzone.qualibot.chunks_v1) AS source, (SELECT count(*) FROM uat_proj.qualibot.chunks) AS copie;
```

**0.2b — Volumes : créer dans `uat_proj` et copier les données (PowerShell, profil `UAT`)**

**0.2a — Volumes du test (`qualibot-uat-test`), suffixe `_test` (PowerShell, profil `UAT`)**

La cible `qualibot-uat-test` partage le schéma `uat_proj.qualibot` avec l'UAT. Ses volumes portent donc le suffixe `_test` : `doc_compare_test`, `test_test`, `images_test`. Avant 0.2b, il faut libérer les noms `doc_compare` et `test` si le test les occupe encore.

```powershell
databricks volumes list uat_proj qualibot --profile UAT -o json | ConvertFrom-Json | ForEach-Object { $_.full_name }
```

Si `uat_proj.qualibot.doc_compare` ou `uat_proj.qualibot.test` existent, ce sont les volumes du test. Les supprimer seulement sur ton accord (données de test) :

```powershell
databricks volumes delete uat_proj.qualibot.doc_compare --profile UAT
databricks volumes delete uat_proj.qualibot.test --profile UAT
```

Créer les volumes du test et, si tu veux, reprendre les documents de test de l'UAT :

```powershell
databricks volumes create uat_proj qualibot doc_compare_test MANAGED --profile UAT
databricks volumes create uat_proj qualibot test_test MANAGED --profile UAT
databricks volumes create uat_proj qualibot images_test MANAGED --profile UAT
databricks fs cp -r dbfs:/Volumes/uat_landingzone/qualibot/test dbfs:/Volumes/uat_proj/qualibot/test_test --profile UAT
```

Puis déployer la cible de test (libre, elle est jetable) pour que l'app lise les nouveaux chemins :

```powershell
databricks bundle deploy -t qualibot-uat-test --profile UAT
```

Les volumes de l'UAT (`doc_compare`, `test`, `images`, `docling_models`) ne portent pas de suffixe : aucun conflit avec la cible de test.

```powershell
databricks volumes create uat_proj qualibot docling_models MANAGED --profile UAT
databricks fs cp -r dbfs:/Volumes/uat_landingzone/qualibot/docling_models dbfs:/Volumes/uat_proj/qualibot/docling_models --profile UAT

databricks volumes create uat_proj qualibot doc_compare MANAGED --profile UAT
databricks fs cp -r dbfs:/Volumes/uat_landingzone/qualibot/doc_compare dbfs:/Volumes/uat_proj/qualibot/doc_compare --profile UAT

databricks volumes create uat_proj qualibot test MANAGED --profile UAT
databricks fs cp -r dbfs:/Volumes/uat_landingzone/qualibot/test dbfs:/Volumes/uat_proj/qualibot/test --profile UAT

databricks volumes create uat_proj qualibot images MANAGED --profile UAT
databricks fs cp -r dbfs:/Volumes/uat_landingzone/qualibot/images dbfs:/Volumes/uat_proj/qualibot/images --profile UAT
```

Le volume `staging` n'est pas copié (échanges transitoires). Les trois volumes déclarés dans le bundle pour `qualibot-uat` (`doc_compare`, `test`, `images`) doivent être liés au bundle avant le déploiement, sinon celui-ci essaie de les recréer :

```powershell
databricks bundle deployment bind qualibot_doc_compare uat_proj.qualibot.doc_compare -t qualibot-uat --profile UAT
databricks bundle deployment bind qualibot_test uat_proj.qualibot.test -t qualibot-uat --profile UAT
databricks bundle deployment bind qualibot_images uat_proj.qualibot.images -t qualibot-uat --profile UAT
```

**0.3 — Index Vector Search `uat_proj.qualibot.chunks_index`**

Les index sont recréés, pas copiés. Le notebook `update_proj_vector_indexes` crée l'index manquant avec le schéma voulu ; le plus sûr est de reprendre la définition de l'index actuel :

```powershell
databricks vector-search-indexes get-index uat_landingzone.qualibot.chunks_index --profile UAT -o json > chunks_index_def.json
```

Éditer `chunks_index_def.json` : remplacer `name` par `uat_proj.qualibot.chunks_index` et `primary_key`/`source_table` (le champ de la table source) par `uat_proj.qualibot.chunks`, puis :

```powershell
databricks vector-search-indexes create-index --json @chunks_index_def.json --profile UAT
databricks vector-search-indexes sync-index uat_proj.qualibot.chunks_index --profile UAT
```

Si la CLI refuse le format (`create-index` attend un corps différent de `get-index`), ne pas forcer : l'index sera créé par le job parsing au déploiement (étape 0.7, `parsing_vector_search_indexes` pointe déjà sur `uat_proj`). Dans ce cas, ne pas déployer avant que l'index soit `ONLINE`.

Même opération pour `chunks_full_index` si tu veux le garder (absent du déploiement aujourd'hui, voir `databricks.yml`).

**0.4 — Droits (SQL, éditeur SQL du workspace UAT)**

Remplacer `<APP_SP>` par le client ID de l'app : `databricks apps get qualibot --profile UAT -o json` (champ `service_principal_client_id`). Remplacer `<PIPELINE_SP>` par `3e5cd4e5-f765-4760-b974-ee7715258b39` (job-runner-sa-uat).

```sql
GRANT USE CATALOG ON CATALOG uat_proj TO `<APP_SP>`;
GRANT USE SCHEMA ON SCHEMA uat_proj.qualibot TO `<APP_SP>`;
GRANT SELECT ON TABLE uat_proj.qualibot.chunks_index TO `<APP_SP>`;

GRANT USE CATALOG ON CATALOG uat_proj TO `<PIPELINE_SP>`;
GRANT USE SCHEMA, CREATE TABLE, SELECT, MODIFY ON SCHEMA uat_proj.qualibot TO `<PIPELINE_SP>`;
```

**0.4b — Modèles de chat : `CAN_QUERY` pour l'app UAT (PowerShell, profil `UAT`)**

Même liste de modèles que le job de droits DEV. Remplacer `<APP_SP>` par le client ID de l'app (étape 0.4) :

```powershell
python -c "from databricks.sdk import WorkspaceClient; from databricks.sdk.service.serving import ServingEndpointAccessControlRequest as A, ServingEndpointPermissionLevel as L; w = WorkspaceClient(profile='UAT'); [w.serving_endpoints.update_permissions(w.serving_endpoints.get(n).id, access_control_list=[A(service_principal_name='<APP_SP>', permission_level=L.CAN_QUERY)]) for n in ['databricks-claude-sonnet-4-6','databricks-gpt-5-6-luna','databricks-gpt-6-luna','databricks-gemini-3-1-flash-lite']]; print('CAN_QUERY ok')"
```

**0.5 — Tables chat depuis Lakebase (run ponctuel)**

```powershell
databricks bundle run -t qualibot-uat lakebase_export_uat_to_volume --profile UAT
```

Ce job exporte Lakebase vers le volume puis recharge `chat_*` et `errors` dans `uat_proj.qualibot` (le notebook d'import a désormais ce catalogue par défaut).

**0.6 — Contrôle avant déploiement**

```powershell
databricks tables list uat_proj qualibot --profile UAT -o json | ConvertFrom-Json | ForEach-Object { $_.full_name }
databricks volumes list uat_proj qualibot --profile UAT -o json | ConvertFrom-Json | ForEach-Object { $_.full_name }
databricks vector-search-indexes get-index uat_proj.qualibot.chunks_index --profile UAT -o json | ConvertFrom-Json | Select-Object name, status
```

Attendu : toutes les tables de 0.2 et les tables chat présentes, `status.ready` à `true` pour l'index.

**0.7 — Déploiement (seulement après ton accord explicite)**

Le déploiement UAT n'est jamais lancé sans ton accord (`CLAUDE.md`). Une fois donné :

```powershell
databricks bundle deploy -t qualibot-uat --profile UAT
```

Puis, une fois le déploiement fait, les jobs MLflow de la cible (expérience `/Shared/qualibot/chat_turns` et registre des prompts) :

```powershell
databricks bundle run -t qualibot-uat create_chat_experiment_uat --profile UAT
databricks bundle run -t qualibot-uat register_chat_prompts_uat --profile UAT
```

Le job de scoring `score_chat_traces_uat` reste en pause ; son premier lancement se fait à la main, en `dry_run` (estimation seulement).

Puis l'app pointe sur `uat_proj.qualibot.chunks_index` (`utils/deploy/target_env.json`). Vérifier le chat et la comparaison d'impact sur l'URL de l'app.

**0.8 — Suppression du job de synchronisation des droits (user capabilities)**

Le job `D_1_Qualibot_Sync_User_Capabilities_Uat_Test` disparaît du bundle `qualibot-uat-test` au déploiement (étape 0.9). Le job posé hors bundle, `qualibot-sync-user-capabilities-uat`, n'est plus déployé par aucun script : il faut le supprimer à la main.

```powershell
databricks jobs list --profile UAT -o json | ConvertFrom-Json | Where-Object { $_.settings.name -match "Sync_User_Capabilities|sync-user-capabilities" } | ForEach-Object { "$($_.job_id) $($_.settings.name)" }
```

Pour chaque `job_id` affiché : `databricks jobs delete <JOB_ID> --profile UAT` (supprime le job, pas de données).

Ce DROP ne se lance qu'après le contrôle de 0.7 : chat et comparaison d'impact validés, logs de l'app sans `live group check failed`. Puis, dans l'éditeur SQL de la base Lakebase `doccompare` (projet `qualibot`, branche `production`), une fois que `qualibot-uat` n'utilise plus les colonnes :

```sql
ALTER TABLE users DROP COLUMN IF EXISTS can_chat, DROP COLUMN IF EXISTS can_compare, DROP COLUMN IF EXISTS groups;
```

Les droits sont désormais vérifiés à chaque requête par l'app (`is_account_group_member()` avec le token du visiteur, `CAPS_WAREHOUSE_ID` déjà renseigné sur `uat`, `uat-test` et `prod`). Après le déploiement, vérifier dans les logs de l'app l'absence de `live group check failed`.

**0.9 — Déploiement du test (libre, cible jetable)**

```powershell
databricks bundle deploy -t qualibot-uat-test --profile UAT
```

**0.10 — Nettoyage de `dev_lab`**

Rien ne doit vivre dans `dev_lab`. Vérifier, puis supprimer seulement après ton accord :

```sql
SHOW SCHEMAS IN dev_lab;
SHOW TABLES IN dev_lab.lab_jules;
```

```sql
DROP SCHEMA IF EXISTS dev_lab.lab_jules CASCADE;
```

(Les deux `DROP` détruisent les données : ne les lancer qu'après relecture de `SHOW TABLES`.)

**0.11 — Suppression des tables legacy dans `uat_proj` (après validation complète)**

Uniquement quand le chat, la comparaison d'impact, le parsing et le tableau de bord d'usage marchent depuis `uat_proj` :

```sql
DROP TABLE IF EXISTS uat_landingzone.qualibot.chunks_v1;
DROP TABLE IF EXISTS uat_landingzone.qualibot.chunks_archive_v1;
DROP TABLE IF EXISTS uat_landingzone.qualibot.chunks_archive_notices_v1;
DROP TABLE IF EXISTS uat_landingzone.qualibot.chunks_full_v1;
DROP TABLE IF EXISTS uat_landingzone.qualibot.processed_files_v1;
DROP TABLE IF EXISTS uat_landingzone.qualibot.image_metadata_v1;
DROP TABLE IF EXISTS uat_landingzone.qualibot.parse_manifest_v1;
DROP TABLE IF EXISTS uat_landingzone.qualibot.category_reference_v1;
DROP TABLE IF EXISTS uat_landingzone.qualibot.parsing_run_health_v1;
DROP TABLE IF EXISTS uat_landingzone.qualibot.document_change_log_v1;
DROP TABLE IF EXISTS uat_landingzone.qualibot.audit_files_unified_v1;
DROP TABLE IF EXISTS uat_landingzone.qualibot._pipeline_checkpoint_v1;
DROP TABLE IF EXISTS uat_landingzone.qualibot.src_chunks_as_v1;
DROP TABLE IF EXISTS uat_landingzone.qualibot.src_chunks_is_v1;
DROP TABLE IF EXISTS uat_landingzone.qualibot.synthetic_retrieval_questions_v2;
DROP TABLE IF EXISTS uat_landingzone.qualibot.feedback_failure_cases;
```

Volumes (après contrôle de la copie, sur ton accord) :

```powershell
databricks volumes delete uat_landingzone.qualibot.doc_compare --profile UAT
databricks volumes delete uat_landingzone.qualibot.test --profile UAT
databricks volumes delete uat_landingzone.qualibot.images --profile UAT
databricks volumes delete uat_landingzone.qualibot.docling_models --profile UAT
databricks volumes delete uat_landingzone.qualibot.staging --profile UAT
```

Puis `databricks vector-search-indexes delete-index uat_landingzone.qualibot.chunks_index_v1 --profile UAT` (et `chunks_as_index_v1`, `chunks_is_index_v1`, `chunks_index` s'ils existent encore après 0.3 : vérifier avec 0.1 avant).

### A. Valider l'app sur `qualibot-uat-test` (branche `audit/doc-compare`)

App seule, pas de `-Infra` : aucune table, aucun index, aucun droit à créer.
L'app y utilise l'index partagé `chunks_index_v1`. Le script ne démarre jamais
cette app : la démarrer d'abord.

- [ ] **A1. Déployer**

  ```powershell
  Remove-Item Env:DATABRICKS_TOKEN -ErrorAction SilentlyContinue
  databricks apps start qualibot-uat-test --profile UAT
  .\utils\deploy\deploy_qualibot.ps1 -AppEnv uat-test
  ```

- [ ] **A2. Impact search v2** (code du 2026-10-02) : comparer deux fichiers,
  puis « Judge Impacted Docs » — les documents arrivent un par un, « Show
  passages » montre la phrase surlignée, la vue « By change » et « Export
  Excel » fonctionnent ; « Cancel » pendant la recherche l'arrête.

- [ ] **A3. Audit de la comparaison** (2026-10-04, détail dans
  `docs/compare_audit_2026-10.md`) :
  - deux révisions connues : la Change Table contient au moins autant de lignes
    qu'avant (les changements d'un seul mot remontent) ; regarder si du bruit
    est apparu ;
  - deux révisions **PDF** avec tableaux : les lignes de tableau modifiées
    nomment leur colonne, pas de faux changements en bas/haut de page ;
  - deux révisions **DOCX**, dont une en suivi des modifications : les
    insertions suivies apparaissent ;
  - un PDF et un DOCX : message « must be of the same type » ;
  - impact search depuis le Change Summary, générer la Change Table, relancer :
    le résultat est recalculé (pas « retrieved from cache ») ;
  - charger une entrée de l'historique : plus de résultats d'une autre
    comparaison à l'écran ;
  - exports Excel et PDF toujours téléchargeables.

- [ ] **A4. Ajouts impact search du 2026-10-05** :
  - la Change Table a une colonne `#` (C1, C2…) ; dans « Show passages »,
    cliquer sur un numéro fait défiler la Change Table jusqu'à cette ligne ;
  - 👍/👎 à droite de chaque document, et « Was this impact search helpful? »
    en bas de la carte : après un vote, une ligne apparaît (la table est créée
    toute seule au démarrage de l'app) :

    ```sql
    -- base Lakebase doccompare_test (uat-test) ou doccompare (uat)
    SELECT created_at, ref, verdict_shown, vote, comment, impact_request_id
    FROM impact_feedbacks ORDER BY created_at DESC LIMIT 10;
    ```

  - lancer une impact search, recharger la comparaison depuis « History » :
    le résultat d'impact réapparaît sans relancer la recherche.

- [ ] **A5. Me dire si on fusionne `audit/doc-compare` dans `main`.**

### B. Pipeline de parsing — phase de test « documents d'avant 2018 »

Ce que fait le code, par défaut (aucun index modifié, rien de nouveau dans les
tables du chatbot) :

- **Plafond** `parsing_archive_max_docs` = nombre TOTAL de documents d'avant 2018
  autorisés à être parsés, les plus récents d'abord. `0` par défaut = aucun.
  `100` = les 100 plus récents ; `-1` = tous (~1 500, gros run GPU).
  C'est cumulatif : repasser à `0` ne supprime rien et ne reparse rien.
- **`chunks_archive_v1`** : chunks des vieux documents parsés. Lue par aucun index.
- **`chunks_archive_notices_v1`** : une fiche par vieux document (référence,
  titre, indice, type, date, « contenu non indexé »), parsé ou non. Réécrite à
  chaque run, sans GPU. Lue par aucun index.
- **`chunks_full_v1`** : `chunks_v1` + `chunks_archive_v1`. Construite, mais
  l'index `chunks_full_index_v1` n'est **pas** créé (retiré de la liste d'index).
- Corrige aussi un bug du 2026-10-02 : `2_manifest` plantait en fin de tâche.

Le job n'existe que sur `qualibot-uat`. `bundle deploy` seul met à jour le job
et ses notebooks ; il ne redéploie pas le code de l'app (pas de `apps deploy`).

- [ ] **B1. Déployer le job, plafond à 0**

  ```powershell
  Remove-Item Env:DATABRICKS_TOKEN -ErrorAction SilentlyContinue
  Remove-Item Env:BUNDLE_VAR_parsing_archive_max_docs -ErrorAction SilentlyContinue
  databricks bundle deploy --target qualibot-uat --profile UAT
  ```

- [ ] **B2. Run n°1, sans aucun vieux document parsé** (ou attendre le run de 4 h)

  ```powershell
  databricks bundle run parsing_pipeline --target qualibot-uat --profile UAT
  ```

- [ ] **B3. Vérifier le run n°1** (SQL editor, workspace UAT)

  ```sql
  -- manifeste : vieux documents connus, aucun autorisé au parsing
  SELECT doc_date < '2018-01-01' AS avant_2018, parse_content, count(*)
  FROM uat_landingzone.qualibot.parse_manifest_v1 GROUP BY 1, 2;

  -- fiches : une par vieux document, texte à relire
  SELECT count(*) FROM uat_landingzone.qualibot.chunks_archive_notices_v1;
  SELECT REF, doc_date, url, chunk_text
  FROM uat_landingzone.qualibot.chunks_archive_notices_v1 ORDER BY doc_date DESC LIMIT 20;

  -- aucune fiche dans les tables du chatbot (les trois doivent renvoyer 0)
  SELECT 'chunks' AS t, count(*) FROM uat_landingzone.qualibot.chunks_v1 WHERE chunk_content_type = 'archive_notice'
  UNION ALL SELECT 'as', count(*) FROM uat_landingzone.qualibot.src_chunks_as_v1 WHERE chunk_content_type = 'archive_notice'
  UNION ALL SELECT 'is', count(*) FROM uat_landingzone.qualibot.src_chunks_is_v1 WHERE chunk_content_type = 'archive_notice';

  -- chunks_full = chunks (l'archive est encore vide)
  SELECT (SELECT count(*) FROM uat_landingzone.qualibot.chunks_full_v1) AS full,
         (SELECT count(*) FROM uat_landingzone.qualibot.chunks_v1) AS chunks;
  ```

  Dans l'UI Vector Search (endpoint `qualibot`) : toujours 3 index, pas de
  `chunks_full_index_v1`.

- [ ] **B4. Run n°2 : les 100 vieux documents les plus récents**

  ```powershell
  $env:BUNDLE_VAR_parsing_archive_max_docs = '100'
  databricks bundle deploy --target qualibot-uat --profile UAT
  Remove-Item Env:BUNDLE_VAR_parsing_archive_max_docs
  databricks bundle run parsing_pipeline --target qualibot-uat --profile UAT
  ```

  Le plafond reste à 100 dans le job jusqu'au prochain `bundle deploy` : les
  runs de nuit ne parsent donc rien de plus.

- [ ] **B5. Vérifier le run n°2**

  ```sql
  -- 100 documents autorisés (les plus récents d'avant 2018)
  SELECT count(*), min(doc_date), max(doc_date)
  FROM uat_landingzone.qualibot.parse_manifest_v1
  WHERE doc_date < '2018-01-01' AND parse_content;

  -- résultat du parsing de ces 100 documents
  SELECT parse_status, count(*) FROM uat_landingzone.qualibot.processed_files_v1
  WHERE doc_date < '2018-01-01' GROUP BY parse_status;

  -- chunks d'archive produits
  SELECT count(DISTINCT IDDOC) AS docs, count(*) AS chunks
  FROM uat_landingzone.qualibot.chunks_archive_v1;

  -- chunks_full = chunks + archive
  SELECT (SELECT count(*) FROM uat_landingzone.qualibot.chunks_full_v1) AS full,
         (SELECT count(*) FROM uat_landingzone.qualibot.chunks_v1)
       + (SELECT count(*) FROM uat_landingzone.qualibot.chunks_archive_v1) AS attendu;

  -- aucun vieux document dans les tables du chatbot (doit renvoyer 0)
  SELECT count(*) FROM uat_landingzone.qualibot.chunks_v1 WHERE doc_date < '2018-01-01';
  ```

- [ ] **B6. Me donner les résultats** (durée du run, statuts en erreur, qualité
  des fiches). On décide alors du bloc D.

### C. Mettre l'app en service sur `qualibot-uat`

Après A5 (fusion décidée). App seule : pas de `-Infra`.

- [ ] **C1. Déployer**

  ```powershell
  Remove-Item Env:DATABRICKS_TOKEN -ErrorAction SilentlyContinue
  .\utils\deploy\deploy_qualibot.ps1 -AppEnv uat
  ```

- [ ] **C2. Refaire une comparaison connue + « Judge Impacted Docs »** : les
  documents apparaissent au fil de l'eau, « Show passages » surligne le texte.

### D. Activations après validation — ne rien faire avant décision

Chaque point est indépendant et demande une modification de ma part : me
prévenir, je change la valeur, vous redéployez.

- [ ] **D1. Parser le reste des vieux documents** : monter le plafond
  (`parsing_archive_max_docs` à `500`, puis `-1`), même procédure que B4.

- [ ] **D2. Créer l'index complet pour l'impact search** (après D5) : je rajoute
  `uat_proj.qualibot.chunks_full_index` à
  `parsing_vector_search_indexes` ; `bundle deploy` + un run le créent. Une fois
  l'index `ONLINE`, je bascule `COMPARE_IMPACT_INDEX` (uat + uat-test) dans
  `utils/deploy/target_env.json`, puis :

  ```powershell
  .\utils\deploy\deploy_qualibot.ps1 -AppEnv uat
  .\utils\deploy\deploy_qualibot.ps1 -AppEnv uat-test
  ```

  En cas d'erreur 403 sur l'index :

  ```sql
  GRANT SELECT ON TABLE uat_proj.qualibot.chunks_full_index TO `<application id du SP de l'app qualibot>`;
  ```

- [ ] **D3. Règle « documents archivés » dans le chatbot** (avant D4) : la règle écrite pour le
  KA (`ka_profiles.py`, supprimé du dépôt, voir le commit f545093) doit passer dans
  `server/prompts/chat/system/base.md`. Me prévenir : je l'ajoute, vous redéployez l'app.

- [ ] **D4. Mettre les fiches dans l'index du chatbot** (après D3 et D5) : je passe
  `parsing_archive_notices_in_rag` à `true` pour `qualibot-uat` ; `bundle deploy` + un run les
  ajoutent à `chunks` et l'index se synchronise. Tester ensuite dans le chatbot :
  - demander un vieux document par sa référence : il le signale, dit que le contenu n'est pas
    disponible, renvoie à Intraqual (lien sous la réponse) ;
  - poser une question sans rapport : aucune fiche citée.

  Retour arrière : repasser à `false`, `bundle deploy` + un run retirent les fiches.

- [ ] **D5. Passer l'UAT sur le chatbot retenu** : un seul chatbot (plus de KA), un seul index
  `chunks_index` avec filtre de division, le découpage retenu (150 / 300 / 450 tokens), plus aucun
  nom versionné. Tout est mesuré en DEV (`docs/chat_tests.md`, partie 1) et doit d'abord être
  passé en DEV (`runbooks/dev.md`, bloc 0). **Avec ton accord seulement** : c'est la vraie app.
  Les tables et index `_v1` restent en place jusqu'à D5.9 : l'app actuelle continue de tourner
  pendant D5.1 à D5.5, et le retour arrière reste possible.

  - [ ] **D5.1. Mettre en pause le planning du pipeline UAT** (UI UAT → Jobs → le job de parsing
    `qualibot` → *Pause*), pour qu'aucun run ne parte pendant les copies.

  - [ ] **D5.2. Inventaire** (éditeur SQL UAT) :

    ```sql
    SHOW TABLES IN uat_proj.qualibot;
    ```

  - [ ] **D5.3. Tables d'état du pipeline sans suffixe** : copies (pas de renommage : l'app et le
    job actuels gardent leurs tables jusqu'à D5.9), avec 60 jours d'historique :

    ```sql
    CREATE TABLE uat_proj.qualibot._pipeline_checkpoint DEEP CLONE uat_landingzone.qualibot._pipeline_checkpoint_v1;
    CREATE TABLE uat_proj.qualibot.processed_files      DEEP CLONE uat_landingzone.qualibot.processed_files_v1;
    CREATE TABLE uat_proj.qualibot.image_metadata       DEEP CLONE uat_landingzone.qualibot.image_metadata_v1;
    CREATE TABLE uat_proj.qualibot.parse_manifest       DEEP CLONE uat_landingzone.qualibot.parse_manifest_v1;
    CREATE TABLE uat_proj.qualibot.category_reference   DEEP CLONE uat_landingzone.qualibot.category_reference_v1;
    CREATE TABLE uat_proj.qualibot.parsing_run_health   DEEP CLONE uat_landingzone.qualibot.parsing_run_health_v1;
    CREATE TABLE uat_proj.qualibot.document_change_log  DEEP CLONE uat_landingzone.qualibot.document_change_log_v1;
    CREATE TABLE uat_proj.qualibot.audit_files_unified  DEEP CLONE uat_landingzone.qualibot.audit_files_unified_v1;
    ```

    (Une table absente de D5.2 : sauter sa ligne.) Puis, pour chacune :

    ```sql
    ALTER TABLE uat_proj.qualibot._pipeline_checkpoint SET TBLPROPERTIES (
      'delta.deletedFileRetentionDuration' = 'interval 60 days', 'delta.logRetentionDuration' = 'interval 60 days');
    ```

  - [ ] **D5.4. Re-découper le corpus dans `chunks`** (aucun re-parsing GPU : les fichiers déjà
    parsés sont relus depuis `_pipeline_checkpoint` ; les passages d'image sont reconstruits depuis
    les descriptions existantes, sans appel LLM). Copier le zip de cette branche, puis :

    ```powershell
    databricks bundle deploy -t qualibot-uat --profile UAT --var="parsing_run_mode=full"
    databricks bundle run parsing_pipeline -t qualibot-uat --profile UAT
    ```

    Le run écrit `chunks`, `chunks_archive`, `processed_files`, puis crée l'index
    `uat_proj.qualibot.chunks_index` et le synchronise (embedding complet, environ 1 h ; si
    la tâche s'arrête avant, la synchronisation continue côté serveur). Attendre `ONLINE`. Sa
    dernière tâche remplit aussi le catalogue des documents de l'app (table Lakebase `doc_catalog`
    de `doccompare` : REF, titre, lien). `qualibot-uat-test` (base `doccompare_test`) garde
    l'ancien instantané `doc_catalog.json` tant qu'on n'ajoute pas cette base à
    `parsing_lakebase_database` — à décider ensemble.

  - [ ] **D5.5. Revenir au mode quotidien** (le planning repart, sur les nouvelles tables) :

    ```powershell
    databricks bundle deploy -t qualibot-uat --profile UAT
    ```

  - [ ] **D5.6. Droits du SP de l'app UAT** (`qualibot`, et celui de `qualibot-uat-test`) :
    - `SELECT` sur l'index :

      ```sql
      GRANT SELECT ON TABLE uat_proj.qualibot.chunks_index TO `<application id du SP de l'app qualibot>`;
      ```

    - **Can Query** sur `databricks-gpt-6-luna` et `databricks-gpt-5-6-luna` (UI UAT → Serving →
      l'endpoint → Permissions).

  - [ ] **D5.7. Déployer l'app** (onglet unique « Chat », index `chunks_index` pour le chat et
    l'impact search) :

    ```powershell
    .\utils\deploy\deploy_qualibot.ps1 -AppEnv uat
    .\utils\deploy\deploy_qualibot.ps1 -AppEnv uat-test
    ```

  - [ ] **D5.8. Tester** comme `runbooks/dev.md` 0.13 (ALL / AS / IS, langue, hors sujet, lien,
    impact search). Retour arrière : redéployer l'ancien zip, puis `bundle deploy` de l'ancien zip
    (tables et index `_v1` intacts). Vérifier aussi le journal Lakebase des tours (`chat_turns`,
    `chat_retrieved_chunks`, colonnes ajoutées à `errors` : créés par l'app à son démarrage, rien à
    faire à la main) avec les requêtes de `runbooks/dev.md` 0.13, dans l'éditeur SQL Lakebase de
    l'UAT (base `doccompare` ; `doccompare_test` pour `qualibot-uat-test`). Référence :
    `docs/lakebase_schema.md`.

  - [ ] **D5.8b. Exporter le journal pour la notation** : lancer une fois le job d'export Lakebase
    (`D_1_Qualibot_Lakebase_Export_To_Volume_qualibot-uat`) ; il écrit toutes les tables, dont
    `chat_turns` et `chat_retrieved_chunks`, dans le volume de staging, et sa 2e tâche les charge
    aussi dans `uat_proj.qualibot` (avec `errors`) :

    ```powershell
    databricks bundle run lakebase_export_uat_to_volume -t qualibot-uat --profile UAT
    ```

    ```sql
    SELECT COUNT(*) FROM uat_proj.qualibot.chat_retrieved_chunks;
    ```

    Le job de notation `D_1_Qualibot_Score_Production_Qa_Uat` n'est plus dans le bundle (son code est
    supprimé du dépôt, voir le commit f545093) : le prochain `bundle deploy -t qualibot-uat` (avec ton
    accord) le supprime. Son remplaçant est `utils/evaluation/score_chat_traces.py`.

  - [ ] **D5.8c. Révisions des documents dans le catalogue** (après D5.7 : c'est l'app qui ajoute
    les colonnes `doc_catalog.revision` / `doc_date`, la tâche de D5.4 a donc écrit le catalogue
    sans elles) :

    ```powershell
    databricks bundle run parsing_pipeline -t qualibot-uat --profile UAT --only 6_update_kb_metadata
    ```

    Puis redémarrer l'app UAT (Apps → `qualibot` → Stop / Start), ou attendre 30 min. Vérifier
    comme `runbooks/dev.md` 0.11 et 0.13.

  - [ ] **D5.8d. Alertes SQL** (workspace UAT, après D5.8b : les tables Delta `chat_turns` et
    `errors` doivent exister). Fichiers `utils/alerts/01` à `06`, mode d'emploi dans
    `utils/alerts/README.md`. Pour chacun : SQL Editor → coller la requête en remplaçant
    `dev_proj` par `uat_proj` → **Save** sous `Qualibot - <but>` (warehouse
    serverless) → **Create alert** :

    | Fichier | Nom | Condition | Planning (Europe/Paris) |
    |---|---|---|---|
    | `01_parsed_without_chunks.sql` | Qualibot - documents sans passages | `docs_without_chunks` > 0 | tous les jours, après le job de parsing |
    | `02_parse_errors.sql` | Qualibot - erreurs de parsing | `docs_in_error` > 0 | tous les jours, après le job de parsing |
    | `03_chat_failure_rate.sql` | Qualibot - échecs du chat | `alert_triggered` = 1 | toutes les heures |
    | `04_job_failures.sql` | Qualibot - jobs en échec | `failed_runs` > 0 | tous les jours |
    | `05_new_error_kinds.sql` | Qualibot - nouvelles erreurs | `new_error_kinds` > 0 | tous les jours, 03:00 (après l'export de 02:00) |
    | `06_chat_degraded_turns.sql` | Qualibot - tours dégradés | `alert_triggered` = 1 | tous les jours, 03:00 |

    Notification : ton e-mail ; *Notify again* au plus une fois par jour. 04 demande `USE SCHEMA`
    + `SELECT` sur `system.lakeflow` (admin du metastore). Les copies Delta lues par 03, 05 et 06
    sont rafraîchies par le job d'export à 02:00 et 14:00 : ces alertes ont jusqu'à 12 h de retard.

  - [ ] **D5.9. Supprimer l'ancien** (quelques jours plus tard, une fois l'app validée) :
    - les 3 KA UAT (`qualibot_ALL_v2` / `_AS_v2` / `_IS_v2`, UI **Agents** → ⋮ → Delete) — le KA de
      test `trace_test` aussi s'il existe encore ;
    - les index :

      ```powershell
      databricks vector-search-indexes delete-index uat_landingzone.qualibot.chunks_index_v1    --profile UAT
      databricks vector-search-indexes delete-index uat_landingzone.qualibot.chunks_as_index_v1 --profile UAT
      databricks vector-search-indexes delete-index uat_landingzone.qualibot.chunks_is_index_v1 --profile UAT
      ```

    - les tables `…_v1` de D5.2 (`DROP TABLE`, `UNDROP TABLE` possible 7 jours), dont `chunks_v1`,
      `src_chunks_as_v1`, `src_chunks_is_v1`, `chunks_archive_v1`, `chunks_archive_notices_v1`,
      `chunks_full_v1` et celles copiées en D5.3 ;
    - renommer la table de questions d'évaluation :

      ```sql
      ALTER TABLE uat_proj.qualibot.synthetic_retrieval_questions_v2
        RENAME TO uat_proj.qualibot.synthetic_retrieval_questions;
      ```

    - **avant** de supprimer les index : vérifier qu'aucun job de notation déployé n'interroge encore
      les index `_v1` (l'ancien `score_production_qa`, jobs DEV et UAT, dont le code n'est plus dans le
      dépôt) ; le cas échéant, le supprimer ou le passer sur `chunks_index` (+ filtre de division).

## Fait

_(rien de confirmé pour l'instant)_
