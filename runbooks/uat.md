# Opérations manuelles — UAT et `qualibot-uat-test`

Chaque étape à faire à la main (machine de déploiement ou UI Databricks) est listée ici, prête à copier-coller, dans l'ordre. Une étape faite est cochée et datée une fois confirmée ; ce qui n'est pas coché n'est pas confirmé.

PowerShell, depuis la racine du projet sur la machine de déploiement. Pour DEV : `runbooks/dev.md`. Pour PROD : `runbooks/prod.md`.

> **Branches.** Les blocs A, B et C sont sur la branche **`audit/doc-compare`**. L'étape 0 et le bloc D sont sur la branche **`feature/chat-vsi-merged-on-impact-search`**. Le zip à déployer est celui de la branche indiquée par le bloc.
>
> **Interdit avant le bloc D5** : cette branche ne se déploie sur `qualibot-uat` / `qualibot-uat-test` qu'au bloc D5, dans son ordre. Son code attend des tables et un index **sans suffixe** (`chunks`, `chunks_index`, `parsing_checkpoint`…) et n'a plus de Knowledge Assistant. Un `bundle deploy` de cette branche avant D5 ferait repartir le pipeline UAT de tables vides (re-parsing GPU de tout le corpus) et l'app chercherait un index qui n'existe pas encore.
> Exception : `qualibot-uat-test` sert de répétition de la bascule (tables `uat_proj.qualibot.*_test`, index `chunks_index_test`, rien dans `uat_proj.qualibot`).

Nettoyage du code à rejouer : `runbooks/retest.md` (DEV d'abord).

## Vue d'ensemble

| Bloc | Quoi | Cible | Touche le chatbot ? |
|---|---|---|---|
| 0 | Migration des tables et volumes vers `uat_proj.qualibot` | `uat_proj`, `qualibot-uat-test` | non |
| A | Valider l'app (impact search v2 + audit comparaison) | `qualibot-uat-test` | non |
| B | Pipeline : phase de test « documents d'avant 2018 » | `qualibot-uat` (job seul) | non |
| C | Mettre l'app en service sur `qualibot-uat` | `qualibot-uat` | non |
| D | Activations après validation (index complet, fiches, bascule du chatbot) | `qualibot-uat` | **oui** pour D3 à D5 |

A et B sont indépendants et peuvent se faire dans n'importe quel ordre. D ne se fait qu'après B, étape par étape, sur décision explicite.

## À faire

### 0. Migration vers `uat_proj.qualibot`

L'app et le pipeline vivent dans `uat_proj.qualibot`. Les volumes de l'UAT ne portent pas de suffixe ; ceux de `qualibot-uat-test` ont le suffixe `_test`. Les tables `*_v1` et les volumes de l'ancien emplacement ne servent qu'à la copie unique de cette étape. Rien ne touche à l'app tant que le déploiement n'a pas été accordé (0.7).

**0.1 — Inventaire (PowerShell, profil `UAT`)**

```powershell
databricks tables list uat_landingzone qualibot --profile UAT -o json | ConvertFrom-Json | ForEach-Object { $_.full_name }
databricks volumes list uat_landingzone qualibot --profile UAT -o json | ConvertFrom-Json | ForEach-Object { $_.full_name }
databricks tables list uat_proj qualibot --profile UAT -o json | ConvertFrom-Json | ForEach-Object { $_.full_name }
databricks volumes list uat_proj qualibot --profile UAT -o json | ConvertFrom-Json | ForEach-Object { $_.full_name }
databricks vector-search-indexes list-indexes qualibot --profile UAT -o json | ConvertFrom-Json | ForEach-Object { $_.name }
```

**0.2 — Clonage des tables (SQL, éditeur SQL du workspace UAT, un utilisateur qui lit l'ancien schéma UAT et crée dans `uat_proj.qualibot`)**

Pour chaque table listée en 0.1 (sauf les volumes), une ligne `DEEP CLONE`. Si la table sans suffixe n'existe pas, cloner la version `_v1` correspondante. Référence :

```sql
CREATE SCHEMA IF NOT EXISTS uat_proj.qualibot;

CREATE TABLE IF NOT EXISTS uat_proj.qualibot.parsing_checkpoint   DEEP CLONE uat_landingzone.qualibot._pipeline_checkpoint_v1;
CREATE TABLE IF NOT EXISTS uat_proj.qualibot.parsed_documents        DEEP CLONE uat_landingzone.qualibot.processed_files_v1;
CREATE TABLE IF NOT EXISTS uat_proj.qualibot.image_metadata         DEEP CLONE uat_landingzone.qualibot.image_metadata_v1;
CREATE TABLE IF NOT EXISTS uat_proj.qualibot.parse_manifest         DEEP CLONE uat_landingzone.qualibot.parse_manifest_v1;
CREATE TABLE IF NOT EXISTS uat_proj.qualibot.category_reference     DEEP CLONE uat_landingzone.qualibot.category_reference_v1;
CREATE TABLE IF NOT EXISTS uat_proj.qualibot.parsing_run_health     DEEP CLONE uat_landingzone.qualibot.parsing_run_health_v1;
CREATE TABLE IF NOT EXISTS uat_proj.qualibot.document_change_log    DEEP CLONE uat_landingzone.qualibot.document_change_log_v1;
CREATE TABLE IF NOT EXISTS uat_proj.qualibot.chunks                 DEEP CLONE uat_landingzone.qualibot.chunks_v1;
CREATE TABLE IF NOT EXISTS uat_proj.qualibot.chunks_archive         DEEP CLONE uat_landingzone.qualibot.chunks_archive_v1;
CREATE TABLE IF NOT EXISTS uat_proj.qualibot.archive_notices DEEP CLONE uat_landingzone.qualibot.chunks_archive_notices_v1;
CREATE TABLE IF NOT EXISTS uat_proj.qualibot.chunks_full            DEEP CLONE uat_landingzone.qualibot.chunks_full_v1;
CREATE TABLE IF NOT EXISTS uat_proj.qualibot.eval_feedback_failures DEEP CLONE uat_landingzone.qualibot.eval_feedback_failures;
```

Les tables chat (`chat_messages`, `chat_sessions`, `chat_feedbacks`, `chat_turns`, `chat_retrieved_chunks`, `app_errors`) ne se clonent pas : elles sont rechargées depuis Lakebase à l'étape 0.5.

Contrôle après clonage (doit donner les mêmes nombres que la source, à la date de copie près) :

```sql
SELECT 'chunks' AS t, (SELECT count(*) FROM uat_landingzone.qualibot.chunks_v1) AS source, (SELECT count(*) FROM uat_proj.qualibot.chunks) AS copie;
```

**0.2a — Volumes du test `qualibot-uat-test` (suffixe `_test`, PowerShell, profil `UAT`)**

La cible de test partage le schéma `uat_proj.qualibot` : ses volumes sont `doc_compare_test`, `test_test`, `images_test`. Avant de les créer, libérer les noms `doc_compare` et `test` s'ils sont encore pris par le test :

```powershell
databricks volumes list uat_proj qualibot --profile UAT -o json | ConvertFrom-Json | ForEach-Object { $_.full_name }
```

Si `uat_proj.qualibot.doc_compare` ou `uat_proj.qualibot.test` existent, ce sont les volumes du test. Les supprimer seulement sur accord (données de test) :

```powershell
databricks volumes delete uat_proj.qualibot.doc_compare --profile UAT
databricks volumes delete uat_proj.qualibot.test --profile UAT
```

Créer les volumes du test et, si voulu, reprendre les documents de test de l'UAT :

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

**0.2b — Volumes de l'UAT (PowerShell, profil `UAT`)**

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

Même opération pour `chunks_full_index` si voulu (absent du déploiement aujourd'hui, voir `databricks.yml`).

**0.4 — Droits (SQL, éditeur SQL du workspace UAT)**

Remplacer `<APP_SP>` par le client ID de l'app : `databricks apps get qualibot --profile UAT -o json` (champ `service_principal_client_id`). Remplacer `<PIPELINE_SP>` par `3e5cd4e5-f765-4760-b974-ee7715258b39` (job-runner-sa-uat).

```sql
GRANT USE CATALOG ON CATALOG uat_proj TO `<APP_SP>`;
GRANT USE SCHEMA ON SCHEMA uat_proj.qualibot TO `<APP_SP>`;
GRANT SELECT ON TABLE uat_proj.qualibot.chunks_index TO `<APP_SP>`;

GRANT USE CATALOG ON CATALOG uat_proj TO `<PIPELINE_SP>`;
GRANT USE SCHEMA, CREATE TABLE, SELECT, MODIFY ON SCHEMA uat_proj.qualibot TO `<PIPELINE_SP>`;
```

**0.5 — Tables chat depuis Lakebase (run ponctuel)**

```powershell
databricks bundle run -t qualibot-uat lakebase_export_uat_to_volume --profile UAT
```

Ce job exporte Lakebase vers le volume, puis recharge `chat_*` et `app_errors` dans `uat_proj.qualibot`.

**0.6 — Contrôle avant déploiement**

```powershell
databricks tables list uat_proj qualibot --profile UAT -o json | ConvertFrom-Json | ForEach-Object { $_.full_name }
databricks volumes list uat_proj qualibot --profile UAT -o json | ConvertFrom-Json | ForEach-Object { $_.full_name }
databricks vector-search-indexes get-index uat_proj.qualibot.chunks_index --profile UAT -o json | ConvertFrom-Json | Select-Object name, status
```

Attendu : toutes les tables de 0.2 et les tables chat présentes, `status.ready` à `true` pour l'index.

**0.7 — Déploiement (seulement au bloc D5, après accord explicite)**

Ce déploiement fait partie du bloc D5 : il ne se lance pas avant D5 (voir l'avertissement en tête de fichier), et jamais sans accord explicite (règle de `CLAUDE.md`). Une fois accordé :

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

**0.8 — Suppression des jobs de synchronisation des droits (user capabilities)**

Le code ne déclare plus de job de synchronisation des droits. Un job de ce type peut encore exister dans le workspace UAT : le lister, puis le supprimer.

```powershell
databricks jobs list --profile UAT -o json | ConvertFrom-Json | Where-Object { $_.settings.name -match "Sync_User_Capabilities|sync-user-capabilities" } | ForEach-Object { "$($_.job_id) $($_.settings.name)" }
```

Pour chaque `job_id` affiché : `databricks jobs delete <JOB_ID> --profile UAT` (supprime le job, pas de données).

Ce DROP ne se lance qu'après le contrôle de 0.7 : chat et comparaison d'impact validés, logs de l'app sans `live group check failed`. Puis, dans l'éditeur SQL de la base Lakebase `doccompare` (projet `qualibot`, branche `production`), une fois que `qualibot-uat` n'utilise plus les colonnes :

```sql
ALTER TABLE users DROP COLUMN IF EXISTS can_chat, DROP COLUMN IF EXISTS can_compare, DROP COLUMN IF EXISTS groups;
```

Les droits sont vérifiés à chaque requête par l'app (`is_account_group_member()` avec le token du visiteur). Après le déploiement, vérifier dans les logs de l'app l'absence de `live group check failed`.

**0.9 — Déploiement du test (libre, cible jetable)**

```powershell
databricks bundle deploy -t qualibot-uat-test --profile UAT
```

**0.10 — Nettoyage de `dev_lab`**

Rien ne doit vivre dans `dev_lab`. Vérifier, puis supprimer seulement sur accord :

```sql
SHOW SCHEMAS IN dev_lab;
SHOW TABLES IN dev_lab.lab_jules;
```

```sql
DROP SCHEMA IF EXISTS dev_lab.lab_jules CASCADE;
```

(Les deux `DROP` détruisent les données : ne les lancer qu'après relecture de `SHOW TABLES`.)

**0.11 — Anciens objets de l'UAT**

Les tables `*_v1`, les index `*_v1` et les volumes de l'ancien emplacement ne sont supprimés par aucune étape de ce runbook : ils restent la source de la copie de l'étape 0. Leur suppression, si elle est décidée, se fait à la main, après validation complète du chat, de la comparaison d'impact, du parsing et du tableau de bord d'usage.

### A. Valider l'app sur `qualibot-uat-test` (branche `audit/doc-compare`)

App seule, pas de `-Infra` : aucune table, aucun index, aucun droit à créer. `deploy_qualibot.ps1` ne démarre jamais cette app : la démarrer d'abord.

- [ ] **A1. Déployer**

  ```powershell
  Remove-Item Env:DATABRICKS_TOKEN -ErrorAction SilentlyContinue
  databricks apps start qualibot-uat-test --profile UAT
  .\utils\deploy\deploy_qualibot.ps1 -AppEnv uat-test
  ```

- [ ] **A2. Impact search v2** : comparer deux fichiers, puis « Judge Impacted Docs » — les documents arrivent un par un, « Show passages » montre la phrase surlignée, la vue « By change » et « Export Excel » fonctionnent ; « Cancel » pendant la recherche l'arrête.

- [ ] **A3. Audit de la comparaison** (détail : `archive/docs/compare_audit_2026-10.md`) :
  - deux révisions connues : la Change Table contient au moins autant de lignes qu'avant (les changements d'un seul mot remontent) ; regarder si du bruit est apparu ;
  - deux révisions **PDF** avec tableaux : les lignes de tableau modifiées nomment leur colonne, pas de faux changements en bas / haut de page ;
  - deux révisions **DOCX**, dont une en suivi des modifications : les insertions suivies apparaissent ;
  - un PDF et un DOCX : message « must be of the same type » ;
  - impact search depuis le Change Summary, générer la Change Table, relancer : le résultat est recalculé (pas « retrieved from cache ») ;
  - charger une entrée de l'historique : pas de résultats d'une autre comparaison à l'écran ;
  - exports Excel et PDF toujours téléchargeables.

- [ ] **A4. Ajouts impact search** :
  - la Change Table a une colonne `#` (C1, C2…) ; dans « Show passages », cliquer sur un numéro fait défiler la Change Table jusqu'à cette ligne ;
  - 👍 / 👎 à droite de chaque document, et « Was this impact search helpful? » en bas de la carte : après un vote, une ligne apparaît (la table est créée au démarrage de l'app) :

    ```sql
    -- base Lakebase doccompare_test (uat-test) ou doccompare (uat)
    SELECT created_at, ref, verdict_shown, vote, comment, impact_request_id
    FROM impact_feedbacks ORDER BY created_at DESC LIMIT 10;
    ```

  - lancer une impact search, recharger la comparaison depuis « History » : le résultat d'impact réapparaît sans relancer la recherche.

- [ ] **A5. Décider de la fusion de `audit/doc-compare` dans `main`.**

### B. Pipeline de parsing — phase de test « documents d'avant 2018 »

Comportement du code, par défaut (aucun index modifié, rien de nouveau dans les tables du chatbot) :

- **Plafond** `parsing_archive_max_docs` = nombre TOTAL de documents d'avant 2018 autorisés à être parsés, les plus récents d'abord. `0` (défaut) = aucun ; `100` = les 100 plus récents ; `-1` = tous (~1 500, gros run GPU). Cumulatif : repasser à `0` ne supprime rien et ne reparse rien.
- **`chunks_archive`** : chunks des vieux documents parsés. Lue par aucun index.
- **`archive_notices`** : une fiche par vieux document (référence, titre, indice, type, date, « contenu non indexé »), parsé ou non. Réécrite à chaque run, sans GPU. Lue par aucun index.
- **`chunks_full`** : `chunks` + `chunks_archive`. Construite, mais l'index `chunks_full_index` n'est **pas** créé.

Le job n'existe que sur `qualibot-uat`. `bundle deploy` seul met à jour le job et ses notebooks ; il ne redéploie pas le code de l'app (pas de `apps deploy`).

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

- [ ] **B3. Vérifier le run n°1** (éditeur SQL, workspace UAT)

  ```sql
  -- manifeste : vieux documents connus, aucun autorisé au parsing
  SELECT doc_date < '2018-01-01' AS avant_2018, parse_content, count(*)
  FROM uat_proj.qualibot.parse_manifest GROUP BY 1, 2;

  -- fiches : une par vieux document, texte à relire
  SELECT count(*) FROM uat_proj.qualibot.archive_notices;
  SELECT REF, doc_date, url, chunk_text
  FROM uat_proj.qualibot.archive_notices ORDER BY doc_date DESC LIMIT 20;

  -- aucune fiche dans la table du chatbot (doit renvoyer 0)
  SELECT 'chunks' AS t, count(*) FROM uat_proj.qualibot.chunks WHERE chunk_content_type = 'archive_notice';

  -- chunks_full = chunks (l'archive est encore vide)
  SELECT (SELECT count(*) FROM uat_proj.qualibot.chunks_full) AS full,
         (SELECT count(*) FROM uat_proj.qualibot.chunks) AS chunks;
  ```

  Dans l'UI Vector Search (endpoint `qualibot`) : pas d'index `chunks_full_index`.

- [ ] **B4. Run n°2 : les 100 vieux documents les plus récents**

  ```powershell
  $env:BUNDLE_VAR_parsing_archive_max_docs = '100'
  databricks bundle deploy --target qualibot-uat --profile UAT
  Remove-Item Env:BUNDLE_VAR_parsing_archive_max_docs
  databricks bundle run parsing_pipeline --target qualibot-uat --profile UAT
  ```

  Le plafond reste à 100 dans le job jusqu'au prochain `bundle deploy` : les runs de nuit ne parsent donc rien de plus.

- [ ] **B5. Vérifier le run n°2**

  ```sql
  -- 100 documents autorisés (les plus récents d'avant 2018)
  SELECT count(*), min(doc_date), max(doc_date)
  FROM uat_proj.qualibot.parse_manifest
  WHERE doc_date < '2018-01-01' AND parse_content;

  -- résultat du parsing de ces 100 documents
  SELECT parse_status, count(*) FROM uat_proj.qualibot.parsed_documents
  WHERE doc_date < '2018-01-01' GROUP BY parse_status;

  -- chunks d'archive produits
  SELECT count(DISTINCT IDDOC) AS docs, count(*) AS chunks
  FROM uat_proj.qualibot.chunks_archive;

  -- chunks_full = chunks + archive
  SELECT (SELECT count(*) FROM uat_proj.qualibot.chunks_full) AS full,
         (SELECT count(*) FROM uat_proj.qualibot.chunks)
       + (SELECT count(*) FROM uat_proj.qualibot.chunks_archive) AS attendu;

  -- aucun vieux document dans la table du chatbot (doit renvoyer 0)
  SELECT count(*) FROM uat_proj.qualibot.chunks WHERE doc_date < '2018-01-01';
  ```

- [ ] **B6. Transmettre les résultats** (durée du run, statuts en erreur, qualité des fiches). Décision ensuite sur le bloc D.

### C. Mettre l'app en service sur `qualibot-uat`

Après A5 (fusion décidée). App seule : pas de `-Infra`.

- [ ] **C1. Déployer**

  ```powershell
  Remove-Item Env:DATABRICKS_TOKEN -ErrorAction SilentlyContinue
  .\utils\deploy\deploy_qualibot.ps1 -AppEnv uat
  ```

- [ ] **C2. Refaire une comparaison connue + « Judge Impacted Docs »** : les documents apparaissent au fil de l'eau, « Show passages » surligne le texte.

### D. Activations après validation — ne rien faire avant décision

Chaque point est indépendant et demande une modification de la configuration : la demander, la valeur est changée, puis le déploiement est refait.

- [ ] **D1. Parser le reste des vieux documents** : monter le plafond (`parsing_archive_max_docs` à `500`, puis `-1`), même procédure que B4.

- [ ] **D2. Créer l'index complet pour l'impact search** (après D5) : ajouter `uat_proj.qualibot.chunks_full_index` à `parsing_vector_search_indexes` ; `bundle deploy` + un run le créent. Une fois l'index `ONLINE`, basculer `COMPARE_IMPACT_INDEX` (uat + uat-test) dans `utils/deploy/target_env.json`, puis :

  ```powershell
  .\utils\deploy\deploy_qualibot.ps1 -AppEnv uat
  .\utils\deploy\deploy_qualibot.ps1 -AppEnv uat-test
  ```

  En cas d'erreur 403 sur l'index :

  ```sql
  GRANT SELECT ON TABLE uat_proj.qualibot.chunks_full_index TO `<application id du SP de l'app qualibot>`;
  ```

- [ ] **D3. Règle « documents archivés » dans le chatbot** (avant D4) : la section « Archived documents » de `server/prompts/chat/system/base.md` doit être relue et validée. Toute modification est faite dans le code, puis l'app est redéployée.

- [ ] **D4. Mettre les fiches dans l'index du chatbot** (après D3 et D5) : passer `parsing_archive_notices_in_rag` à `true` pour `qualibot-uat` ; `bundle deploy` + un run les ajoutent à `chunks` et l'index se synchronise. Tester ensuite dans le chatbot :
  - demander un vieux document par sa référence : il le signale, dit que le contenu n'est pas disponible, renvoie à Intraqual (lien sous la réponse) ;
  - poser une question sans rapport : aucune fiche citée.

  Retour arrière : repasser à `false`, `bundle deploy` + un run retirent les fiches.

- [ ] **D5. Passer l'UAT sur le chatbot retenu** : un seul chatbot (plus de Knowledge Assistant), un seul index `chunks_index` avec filtre de division, le découpage retenu (150 / 300 / 450 tokens), plus aucun nom versionné. Mesures et configuration retenue : `archive/docs/chat_tests.md` (partie 1) ; tester d'abord en DEV (`runbooks/dev.md`, bloc 0). **Avec accord seulement** : c'est la vraie app. Les objets `_v1` restent en place jusqu'à D5.9 : l'app actuelle continue de tourner pendant D5.1 à D5.5, et le retour arrière reste possible.

  - [ ] **D5.1. Mettre en pause le planning du pipeline UAT** (UI UAT → Jobs → le job de parsing `qualibot` → *Pause*), pour qu'aucun run ne parte pendant les copies.

  - [ ] **D5.2. Inventaire** (éditeur SQL UAT) :

    ```sql
    SHOW TABLES IN uat_proj.qualibot;
    ```

  - [ ] **D5.3. Tables d'état du pipeline : rétention de 60 jours.** Elles sont créées par l'étape 0.2 (copies de `_v1`) : vérifier avec D5.2 qu'elles existent. Une table absente de D5.2 : sauter sa ligne. Puis, pour chacune :

    ```sql
    ALTER TABLE uat_proj.qualibot.parsing_checkpoint SET TBLPROPERTIES (
      'delta.deletedFileRetentionDuration' = 'interval 60 days', 'delta.logRetentionDuration' = 'interval 60 days');
    ALTER TABLE uat_proj.qualibot.parsed_documents SET TBLPROPERTIES (
      'delta.deletedFileRetentionDuration' = 'interval 60 days', 'delta.logRetentionDuration' = 'interval 60 days');
    ALTER TABLE uat_proj.qualibot.image_metadata SET TBLPROPERTIES (
      'delta.deletedFileRetentionDuration' = 'interval 60 days', 'delta.logRetentionDuration' = 'interval 60 days');
    ALTER TABLE uat_proj.qualibot.parse_manifest SET TBLPROPERTIES (
      'delta.deletedFileRetentionDuration' = 'interval 60 days', 'delta.logRetentionDuration' = 'interval 60 days');
    ALTER TABLE uat_proj.qualibot.category_reference SET TBLPROPERTIES (
      'delta.deletedFileRetentionDuration' = 'interval 60 days', 'delta.logRetentionDuration' = 'interval 60 days');
    ALTER TABLE uat_proj.qualibot.parsing_run_health SET TBLPROPERTIES (
      'delta.deletedFileRetentionDuration' = 'interval 60 days', 'delta.logRetentionDuration' = 'interval 60 days');
    ALTER TABLE uat_proj.qualibot.document_change_log SET TBLPROPERTIES (
      'delta.deletedFileRetentionDuration' = 'interval 60 days', 'delta.logRetentionDuration' = 'interval 60 days');
    ```

  - [ ] **D5.4. Re-découper le corpus dans `chunks`** (aucun re-parsing GPU : les fichiers déjà parsés sont relus depuis `parsing_checkpoint` ; les passages d'image sont reconstruits depuis les descriptions existantes, sans appel LLM). Copier le zip de cette branche, puis :

    ```powershell
    databricks bundle deploy -t qualibot-uat --profile UAT --var="parsing_run_mode=full"
    databricks bundle run parsing_pipeline -t qualibot-uat --profile UAT
    ```

    Le run écrit `chunks`, `chunks_archive`, `parsed_documents`, puis crée l'index `uat_proj.qualibot.chunks_index` et le synchronise (embedding complet, environ 1 h ; si la tâche s'arrête avant, la synchronisation continue côté serveur). Attendre `ONLINE`. Sa dernière tâche remplit aussi le catalogue des documents de l'app (table Lakebase `doc_catalog` de `doccompare` : REF, titre, lien). La cible `qualibot-uat-test` (base `doccompare_test`) n'a pas de catalogue tant que cette base n'est pas ajoutée à `parsing_lakebase_database` : décision à prendre.

  - [ ] **D5.5. Revenir au mode quotidien** (le planning repart, sur les nouvelles tables) :

    ```powershell
    databricks bundle deploy -t qualibot-uat --profile UAT
    ```

  - [ ] **D5.6. Droits du SP de l'app UAT** (`qualibot`, et celui de `qualibot-uat-test`) :
    - `SELECT` sur l'index :

      ```sql
      GRANT SELECT ON TABLE uat_proj.qualibot.chunks_index TO `<application id du SP de l'app qualibot>`;
      ```

    - **Can Query** sur `databricks-gpt-6-luna` et `databricks-gpt-5-6-luna` (UI UAT → Serving → l'endpoint → Permissions).

  - [ ] **D5.7. Déployer l'app** (onglet unique « Chat », index `chunks_index` pour le chat et l'impact search) :

    ```powershell
    .\utils\deploy\deploy_qualibot.ps1 -AppEnv uat
    .\utils\deploy\deploy_qualibot.ps1 -AppEnv uat-test
    ```

  - [ ] **D5.8. Tester** comme `runbooks/dev.md` 0.13 (ALL / AS / IS, langue, hors sujet, lien, impact search). Vérifier aussi le journal Lakebase des tours (`chat_turns`, `chat_retrieved_chunks`, colonnes ajoutées à `errors` : créées par l'app à son démarrage, rien à faire à la main) avec les requêtes de `runbooks/dev.md` 0.13, dans l'éditeur SQL Lakebase de l'UAT (base `doccompare` ; `doccompare_test` pour `qualibot-uat-test`). Référence : `docs/lakebase_schema.md`. Retour arrière : redéployer le zip précédent ; les objets `_v1` restent intacts jusqu'à D5.9.

  - [ ] **D5.8b. Exporter le journal pour la notation** : lancer une fois le job d'export Lakebase (`D_1_Qualibot_Lakebase_Export_To_Volume_qualibot-uat`) ; il écrit toutes les tables, dont `chat_turns` et `chat_retrieved_chunks`, dans le volume de staging, et sa 2e tâche les charge aussi dans `uat_proj.qualibot` (avec `errors`) :

    ```powershell
    databricks bundle run lakebase_export_uat_to_volume -t qualibot-uat --profile UAT
    ```

    ```sql
    SELECT COUNT(*) FROM uat_proj.qualibot.chat_retrieved_chunks;
    ```

    La notation courante est `utils/evaluation/score_chat_traces.py` (job `score_chat_traces_uat`, en pause). Le job de notation de l'ancien assistant n'est plus dans le bundle : le prochain `bundle deploy -t qualibot-uat` (sur accord) le supprime.

  - [ ] **D5.8c. Révisions des documents dans le catalogue** (après D5.7 : c'est l'app qui ajoute les colonnes `doc_catalog.revision` / `doc_date`, la tâche de D5.4 a donc écrit le catalogue sans elles) :

    ```powershell
    databricks bundle run parsing_pipeline -t qualibot-uat --profile UAT --only 6_update_kb_metadata
    ```

    Puis redémarrer l'app UAT (Apps → `qualibot` → Stop / Start), ou attendre 30 min. Vérifier comme `runbooks/dev.md` 0.11 et 0.13.

  - [ ] **D5.8d. Alertes SQL** (workspace UAT, après D5.8b : les tables Delta `chat_turns` et `app_errors` doivent exister). Fichiers `utils/alerts/01` à `06`, mode d'emploi dans `utils/alerts/README.md`. Pour chacun : SQL Editor → coller la requête en remplaçant `dev_proj` par `uat_proj` → **Save** sous `Qualibot - <but>` (warehouse serverless) → **Create alert** :

    | Fichier | Nom | Condition | Planning (Europe/Paris) |
    |---|---|---|---|
    | `01_parsed_without_chunks.sql` | Qualibot - documents sans passages | `docs_without_chunks` > 0 | tous les jours, après le job de parsing |
    | `02_parse_errors.sql` | Qualibot - erreurs de parsing | `docs_in_error` > 0 | tous les jours, après le job de parsing |
    | `03_chat_failure_rate.sql` | Qualibot - échecs du chat | `alert_triggered` = 1 | toutes les heures |
    | `04_job_failures.sql` | Qualibot - jobs en échec | `failed_runs` > 0 | tous les jours |
    | `05_new_error_kinds.sql` | Qualibot - nouvelles erreurs | `new_error_kinds` > 0 | tous les jours, 03:00 (après l'export de 02:00) |
    | `06_chat_degraded_turns.sql` | Qualibot - tours dégradés | `alert_triggered` = 1 | tous les jours, 03:00 |

    Notification : votre e-mail ; *Notify again* au plus une fois par jour. 04 demande `USE SCHEMA` + `SELECT` sur `system.lakeflow` (admin du metastore). Les copies Delta lues par 03, 05 et 06 sont rafraîchies par le job d'export à 02:00 et 14:00 : ces alertes ont jusqu'à 12 h de retard.

  - [ ] **D5.9. Supprimer l'ancien** (quelques jours plus tard, une fois l'app validée) :
    - les 3 KA UAT (`qualibot_ALL_v2` / `_AS_v2` / `_IS_v2`, UI **Agents** → ⋮ → Delete) — le KA de test `trace_test` aussi s'il existe encore ;
    Les objets `_v1` (tables, index, volumes) ne sont pas supprimés par ce runbook (voir étape 0.11).

### R0. Renommer les tables au moment de la bascule (ne rien faire avant la décision de bascule)

L'UAT actuelle lit encore les anciens noms : ces commandes ne se lancent qu'avec la bascule, dans cet ordre, comme sur DEV (`runbooks/dev.md`, bloc R0).

1. Éditeur SQL UAT :

```sql
ALTER TABLE uat_proj.qualibot.processed_files RENAME TO parsed_documents;
ALTER TABLE uat_proj.qualibot.chunks_archive_notices RENAME TO archive_notices;
ALTER TABLE uat_proj.qualibot._pipeline_checkpoint RENAME TO parsing_checkpoint;
ALTER TABLE uat_proj.qualibot._rag_exclusions RENAME TO rag_exclusions;
ALTER TABLE uat_proj.qualibot.qualibot_eval_golden RENAME TO eval_golden_questions;
ALTER TABLE uat_proj.qualibot.synthetic_retrieval_questions RENAME TO eval_synthetic_questions;
ALTER TABLE uat_proj.qualibot.feedback_failure_cases RENAME TO eval_feedback_failures;
ALTER TABLE uat_proj.qualibot.eval_pairwise_runs_cache RENAME TO eval_pairwise_cache;
ALTER TABLE uat_proj.qualibot.eval_load_runs RENAME TO eval_load_chat_runs;
ALTER TABLE uat_proj.qualibot.eval_vs_load_runs RENAME TO eval_load_vector_search_runs;
SHOW TABLES IN uat_proj.qualibot;
```

2. Lakebase UAT, PowerShell à la racine du dépôt :

```powershell
python runbooks/scripts/lakebase/migrate_lakebase.py --env UAT
```

3. Relancer le job de droits UAT (`grant_app_access_uat_test` pour le test, puis le job de droits UAT) : sa liste de tables porte les nouveaux noms.

Les tables de la rehearsal `qualibot-uat-test` (suffixe `_test`) créées avant ce changement gardent les anciens noms : relancer `runbooks/scripts/uat/setup_proj_schema.py` avec `overwrite = true` pour les recréer sous les nouveaux noms.

## Fait

_(rien de confirmé pour l'instant)_
