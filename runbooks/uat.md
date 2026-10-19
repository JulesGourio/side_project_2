# Opérations manuelles — UAT

Chaque étape à faire à la main (machine de déploiement ou UI Databricks) est listée ici, prête à copier-coller, dans l'ordre. Une étape faite est cochée et datée une fois confirmée ; ce qui n'est pas coché n'est pas confirmé.

PowerShell, depuis la racine du projet sur la machine de déploiement. Pour DEV : `runbooks/dev.md`. Pour PROD : `runbooks/prod.md`.

> **Branches.** Les blocs A, B et C sont sur la branche **`audit/doc-compare`**. L'étape 0 et le bloc D sont sur la branche **`feature/chat-vsi-merged-on-impact-search`**. Le zip à déployer est celui de la branche indiquée par le bloc.
>
> **Interdit avant le bloc D5** : cette branche ne se déploie sur `qualibot-uat` qu'au bloc D5, dans son ordre. Son code attend des tables et un index **sans suffixe** (`chunks`, `chunks_index`, `parsing_checkpoint`…) et n'a plus de Knowledge Assistant. Un `bundle deploy` de cette branche avant D5 ferait repartir le pipeline UAT de tables vides (re-parsing GPU de tout le corpus) et l'app chercherait un index qui n'existe pas encore.

Nettoyage du code à rejouer : `runbooks/retest.md` (DEV d'abord).

## Vue d'ensemble

| Bloc | Quoi | Cible | Touche le chatbot ? |
|---|---|---|---|
| 0 | Migration des tables et volumes vers `uat_proj.qualibot` | `uat_proj`, `qualibot-uat` | non |
| A | Valider l'app (impact search v2 + audit comparaison) sur `qualibot-uat` | `qualibot-uat` | non |
| B | Pipeline : phase de test « documents d'avant 2018 » | `qualibot-uat` (job seul) | non |
| C | Mettre l'app en service sur `qualibot-uat` | `qualibot-uat` | non |
| D | Activations après validation (index complet, fiches, bascule du chatbot) | `qualibot-uat` | **oui** pour D3 à D5 |

A et B sont indépendants et peuvent se faire dans n'importe quel ordre. D ne se fait qu'après B, étape par étape, sur décision explicite.

## À faire

### 0. Migration vers `uat_proj.qualibot`

Les noms des jobs n'ont plus de suffixe de cible (`D_1_Qualibot_Parsing_Pipeline`, et non plus `..._qualibot-uat`). Le premier `bundle deploy` de `qualibot-uat` renomme les jobs existants en place (même identifiant, même planning) : pas de doublon.

L'app et le pipeline vivent dans `uat_proj.qualibot`. Les tables `*_v1` et les volumes de l'ancien emplacement ne servent qu'à la copie unique de cette étape. Rien ne touche à l'app tant que le déploiement n'a pas été accordé (0.7).

**0.1 — Inventaire (PowerShell, profil `UAT`)**

```powershell
databricks tables list uat_landingzone qualibot --profile UAT -o json | ConvertFrom-Json | ForEach-Object { $_.full_name }
databricks volumes list uat_landingzone qualibot --profile UAT -o json | ConvertFrom-Json | ForEach-Object { $_.full_name }
databricks tables list uat_proj qualibot --profile UAT -o json | ConvertFrom-Json | ForEach-Object { $_.full_name }
databricks volumes list uat_proj qualibot --profile UAT -o json | ConvertFrom-Json | ForEach-Object { $_.full_name }
databricks vector-search-indexes list-indexes qualibot --profile UAT -o json | ConvertFrom-Json | ForEach-Object { $_.name }
```

**0.2 — Clonage des tables (SQL, éditeur SQL du workspace UAT, un utilisateur qui lit l'ancien schéma UAT et crée dans `uat_proj.qualibot`)**

Pour chaque table listée en 0.1 (sauf les volumes), une ligne `DEEP CLONE`. Si la table sans suffixe n'existe pas, cloner la version `_v1` correspondante. Inventaire du 2026-10-16 : `chunks_archive_v1`, `chunks_archive_notices_v1` et `chunks_full_v1` n'existent pas dans `uat_landingzone` : rien à cloner, le pipeline les crée au premier run. `eval_feedback_failures` s'appelle `feedback_failure_cases` dans la source : cloner depuis ce nom, ligne ci-dessous. Référence :

```sql
CREATE SCHEMA IF NOT EXISTS uat_proj.qualibot;

CREATE TABLE IF NOT EXISTS uat_proj.qualibot.parsing_checkpoint   DEEP CLONE uat_landingzone.qualibot._pipeline_checkpoint_v1;
CREATE TABLE IF NOT EXISTS uat_proj.qualibot.parsed_documents        DEEP CLONE uat_landingzone.qualibot.processed_files_v1;
CREATE TABLE IF NOT EXISTS uat_proj.qualibot.image_metadata         DEEP CLONE uat_landingzone.qualibot.image_metadata_v1;
CREATE TABLE IF NOT EXISTS uat_proj.qualibot.parse_manifest         DEEP CLONE uat_landingzone.qualibot.parse_manifest_v1;
CREATE TABLE IF NOT EXISTS uat_proj.qualibot.category_reference     DEEP CLONE uat_landingzone.qualibot.category_reference_v1;
CREATE TABLE IF NOT EXISTS uat_proj.qualibot.parsing_run_health     DEEP CLONE uat_landingzone.qualibot.parsing_run_health_v1;
CREATE TABLE IF NOT EXISTS uat_proj.qualibot.document_change_log    DEEP CLONE uat_landingzone.qualibot.document_change_log_v1;
CREATE TABLE IF NOT EXISTS uat_proj.qualibot.eval_feedback_failures DEEP CLONE uat_landingzone.qualibot.feedback_failure_cases;
CREATE TABLE IF NOT EXISTS uat_proj.qualibot.chunks                 DEEP CLONE uat_landingzone.qualibot.chunks_v1;
```

Les tables chat (`chat_messages`, `chat_sessions`, `chat_feedbacks`, `chat_turns`, `chat_retrieved_chunks`, `app_errors`) ne se clonent pas : elles sont rechargées depuis Lakebase à l'étape 0.5.

Contrôle après clonage (doit donner les mêmes nombres que la source, à la date de copie près) :

```sql
SELECT 'chunks' AS t, (SELECT count(*) FROM uat_landingzone.qualibot.chunks_v1) AS source, (SELECT count(*) FROM uat_proj.qualibot.chunks) AS copie;
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
databricks bundle deployment bind qualibot_doc_compare uat_proj.qualibot.doc_compare -t qualibot-uat --profile UAT   # à lancer par le SP (même règle que DEV : pipeline Bitbucket), pas avec le profil personnel
databricks bundle deployment unbind qualibot_test -t qualibot-uat --profile UAT
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
databricks bundle run -t qualibot-uat lakebase_export --profile UAT
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

Ce déploiement fait partie du bloc D5 : il ne se lance pas avant D5 (voir l'avertissement en tête de fichier), et jamais sans accord explicite. Une fois accordé :

```powershell
databricks bundle deploy -t qualibot-uat --profile UAT
```

Puis, une fois le déploiement fait, les jobs MLflow de la cible (expérience des traces du chat, en tables Unity Catalog, et registre des prompts : `runbooks/uat_bascule.md` Q13) :

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

**0.9 — Suppression de la cible de test `qualibot-uat-test` (tout doit disparaître) — fait, confirmé le 2026-10-15**

Il n'y a plus de cible de test : la bascule se fait directement sur `qualibot-uat`. Le `bundle destroy` a besoin du code qui déclare la cible : il se lance donc depuis le commit `6384ff0`, le dernier qui la contient. Le dossier de travail ne la contient plus. Le zip `qualibot-destroy.zip` contient `databricks.yml` et `resources/` tels qu'ils étaient au commit `6384ff0` : c'est tout ce qu'il faut pour le destroy.

1. Code de la cible (PowerShell, à la racine du projet) :

```powershell
Expand-Archive -Path "C:\Users\L0041770\Downloads\qualibot-destroy.zip" -DestinationPath "C:\Users\L0041770\Desktop\qualibot-destroy" -Force
cd "C:\Users\L0041770\Desktop\qualibot-destroy"
databricks bundle validate -t qualibot-uat-test --profile UAT
databricks bundle destroy -t qualibot-uat-test --profile UAT --auto-approve
```

Cette commande supprime l'app `qualibot-uat-test`, son job de parsing, ses jobs de droits, d'arrêt de nuit et de notation.

2. Tables `_test` (les tables sans suffixe ne sont pas listées). Lister d'abord toutes les tables du schéma :

```powershell
databricks tables list uat_proj qualibot --profile UAT -o json | ConvertFrom-Json | ForEach-Object { $_.name }
```

Les tables de test appartiennent au service principal du job UAT (`job-runner-sa-uat`), donc un utilisateur ne peut pas les supprimer. Les étapes 2 et 3 sont faites par un job one-shot qui tourne sous ce service principal (`run_as`). Il ne déploie rien : c'est une soumission unique, depuis la racine du projet, avec le profil `UAT`.

Importer le notebook (son chemin doit correspondre exactement à `notebook_path` de `drop_uat_test_submit.json`) :

```powershell
databricks workspace import /Workspace/Shared/qualibot_uat_cleanup/drop_uat_test_objects --file runbooks/scripts/uat_cleanup/drop_uat_test_objects.py --format SOURCE --language PYTHON --overwrite --profile UAT
```

Lancer la suppression (tables et volumes listés dans le notebook, rien d'autre) :

```powershell
databricks jobs submit --json "@runbooks/scripts/uat_cleanup/drop_uat_test_submit.json" --profile UAT
```

La commande affiche l'identifiant du run : le suivre dans l'interface Jobs. Le run échoue si un objet n'a pas pu être supprimé, et le message indique lequel.

Les tables préfixées `trace_test_` ne finissent pas par `_test` : la boucle ci-dessus ne les touche pas. Les supprimer seulement après vérification de leur contenu :

```powershell
$traceTables = @("trace_test_otel_annotations", "trace_test_otel_logs", "trace_test_otel_metrics", "trace_test_otel_spans", "trace_test_trace_metadata", "trace_test_trace_unified")
foreach ($t in $traceTables) { databricks tables delete "uat_proj.qualibot.$t" --profile UAT }
```

3. Volumes de test (le volume `uat_proj.qualibot.test` n'est pas un volume de test, il reste). Vérifier d'abord lesquels existent :

```powershell
databricks volumes list uat_proj qualibot --profile UAT -o json | ConvertFrom-Json | ForEach-Object { $_.name }
```

Supprimer ceux qui sont listés (un volume absent renvoie « does not exist » : rien à faire) :

```powershell
databricks volumes delete uat_proj.qualibot.doc_compare_test --profile UAT
databricks volumes delete uat_proj.qualibot.test_test --profile UAT
databricks volumes delete uat_proj.qualibot.images_test --profile UAT
```

Si `images_test` renvoie « User does not have MANAGE », son propriétaire n'est pas l'utilisateur UAT courant. Voir le propriétaire, puis demander à ce propriétaire, ou à un administrateur UAT, de le supprimer :

```powershell
databricks volumes read uat_proj.qualibot.images_test --profile UAT -o json | ConvertFrom-Json | Select-Object owner, volume_type, full_name
```

4. Index de test :

```powershell
databricks vector-search-indexes delete-index uat_proj.qualibot.chunks_index_test --profile UAT
```

5. Experiment MLflow de test :

```powershell
$exp = databricks experiments get-by-name /Shared/qualibot/chat_turns_test --profile UAT -o json | ConvertFrom-Json
$exp.experiment.experiment_id
databricks experiments delete-experiment $exp.experiment.experiment_id --profile UAT
```

La première ligne affiche l'identifiant lu depuis le serveur ; la troisième le supprime.

6. Base Lakebase `doccompare_test` (éditeur SQL Lakebase, projet `qualibot`, branche `production`, connecté à la base `doccompare`) :

```sql
DROP DATABASE IF EXISTS doccompare_test;
```

Si la commande est refusée, supprimer la base depuis l'interface Lakebase.

7. Contrôle final :

```powershell
databricks apps list --profile UAT -o json | ConvertFrom-Json | Where-Object { $_.name -like '*uat-test*' } | ForEach-Object { $_.name }
databricks vector-search-indexes list-indexes qualibot --profile UAT -o json | ConvertFrom-Json | ForEach-Object { $_.name }
```

Attendu : aucune app dont le nom contient `uat-test`, et aucun index `chunks_index_test`.

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

### A. Valider l'app sur `qualibot-uat` (branche `audit/doc-compare`)

Pas de cible de test : les contrôles ci-dessous se font sur `qualibot-uat` juste après le déploiement de C1 (C1 remplace cette étape de déploiement).

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
    -- base Lakebase doccompare
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

- [ ] **D2. Créer l'index complet pour l'impact search** (après D5) : ajouter `uat_proj.qualibot.chunks_full_index` à `parsing_vector_search_indexes` ; `bundle deploy` + un run le créent. Une fois l'index `ONLINE`, basculer `COMPARE_IMPACT_INDEX` (uat) dans `utils/deploy/target_env.json`, puis :

  ```powershell
  .\utils\deploy\deploy_qualibot.ps1 -AppEnv uat
  .\utils\deploy\deploy_qualibot.ps1 -AppEnv uat
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

  Historique de comparaison (à faire avant le premier démarrage de la nouvelle app) : les tables `messages`, `feedbacks` et `errors` (Lakebase `doccompare`) contiennent l'historique des utilisateurs. Le démarrage de la nouvelle app copie leurs lignes dans `compare_messages`, `compare_feedbacks` et `app_errors`, **uniquement si ces tables sont vides** : vérifier d'abord, éditeur SQL Lakebase UAT (base `doccompare`) :

  ```sql
  SELECT (SELECT COUNT(*) FROM compare_messages) AS new_messages, (SELECT COUNT(*) FROM messages) AS legacy_messages;
  ```

  Attendu : `new_messages` = 0. Après le démarrage, dans les logs de l'app (`databricks apps logs qualibot --profile UAT`) : `Copied legacy rows messages -> compare_messages`. Les anciennes tables ne sont pas supprimées. La table `knowledge_base_metadata` n'est pas recopiée : la date du corpus se remet à jour avec la tâche `6_update_kb_metadata`.
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

    Le run écrit `chunks`, `chunks_archive`, `parsed_documents`, puis crée l'index `uat_proj.qualibot.chunks_index` et le synchronise (embedding complet, environ 1 h ; si la tâche s'arrête avant, la synchronisation continue côté serveur). Attendre `ONLINE`. Sa dernière tâche remplit aussi le catalogue des documents de l'app (table Lakebase `doc_catalog` de `doccompare` : REF, titre, lien).

  - [ ] **D5.5. Revenir au mode quotidien** (le planning repart, sur les nouvelles tables) :

    ```powershell
    databricks bundle deploy -t qualibot-uat --profile UAT
    ```

  - [ ] **D5.6. Droits du SP de l'app UAT** (`qualibot`) :
    - `SELECT` sur l'index :

      ```sql
      GRANT SELECT ON TABLE uat_proj.qualibot.chunks_index TO `<application id du SP de l'app qualibot>`;
      ```

    - **Can Query** sur `databricks-gpt-6-luna` et `databricks-gpt-5-6-luna` (UI UAT → Serving → l'endpoint → Permissions).

  - [ ] **D5.7. Déployer l'app** (onglet unique « Chat », index `chunks_index` pour le chat et l'impact search) :

    ```powershell
    .\utils\deploy\deploy_qualibot.ps1 -AppEnv uat
    .\utils\deploy\deploy_qualibot.ps1 -AppEnv uat
    ```

  - [ ] **D5.8. Tester** comme `runbooks/dev.md` 0.13 (ALL / AS / IS, langue, hors sujet, lien, impact search). Vérifier aussi le journal Lakebase des tours (`chat_turns`, `chat_retrieved_chunks`, colonnes ajoutées à `errors` : créées par l'app à son démarrage, rien à faire à la main) avec les requêtes de `runbooks/dev.md` 0.13, dans l'éditeur SQL Lakebase de l'UAT (base `doccompare`). Référence : `server/services/infra/lakebase/schema.md`. Retour arrière : redéployer le zip précédent ; les objets `_v1` restent intacts jusqu'à D5.9.

  - [ ] **D5.8b. Exporter le journal pour la notation** : lancer une fois le job d'export Lakebase (`D_1_Qualibot_Lakebase_Export_To_Volume`) ; il écrit toutes les tables, dont `chat_turns` et `chat_retrieved_chunks`, dans le volume de staging, et sa 2e tâche les charge aussi dans `uat_proj.qualibot` (avec `errors`) :

    ```powershell
    databricks bundle run lakebase_export -t qualibot-uat --profile UAT
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

  - [ ] **D5.8d. Alertes SQL** (déployées par le bundle avec `deploy-uat-jobs`, en pause : rien à créer à la main). Vérifier que six alertes `Qualibot - …` existent : `databricks bundle summary -t qualibot-uat --profile UAT`. Activer : `alerts_pause_status` à `UNPAUSED` pour `qualibot-uat` dans `databricks.yml`, puis `deploy-uat-jobs`, après décision écrite. `job_failures` demande SELECT sur `system.lakeflow` pour le SP UAT (admin du metastore).
  - [ ] **D5.9. Supprimer l'ancien** (quelques jours plus tard, une fois l'app validée) :
    - les 3 KA UAT (`qualibot_ALL_v2` / `_AS_v2` / `_IS_v2`, UI **Agents** → ⋮ → Delete) — le KA de test `trace_test` aussi s'il existe encore ;
    Les objets `_v1` (tables, index, volumes) ne sont pas supprimés par ce runbook (voir étape 0.11).

### E. Sonnet 5.5 pour l'analyse de comparaison (à faire avec le déploiement de `qualibot-uat`, pas avant)

Le modèle d'analyse des changements passe de `databricks-claude-sonnet-4-6` à `databricks-claude-sonnet-5-5` (`app.yaml`). Il n'y a rien à
accorder (voir `utils/grants/README.md`) : le changement arrive avec le prochain déploiement de `qualibot-uat`, donc après la décision D5.
Avant ce déploiement, vérifier que l'endpoint existe sur UAT (Attendu : `"name": "databricks-claude-sonnet-5-5"`) :

- [ ] **E1. Endpoint présent** :

  ```powershell
  databricks serving-endpoints get databricks-claude-sonnet-5-5 --profile UAT
  ```

  Une erreur `not found` : ne pas déployer, me le dire.

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

3. Relancer le job de droits UAT : sa liste de tables porte les nouveaux noms.

### 0.12 — Précautions et nettoyage (à lire avant D5)

Rien dans cette étape ne supprime de données. Les suppressions viennent après validation complète, sur accord écrit.

**Ce que fait le démarrage de la nouvelle version** (`server/services/infra/lakebase/schema.py`) : la copie de l'historique (`messages` → `compare_messages`, `feedbacks` → `compare_feedbacks`, `errors` → `app_errors`) n'a lieu que si la table cible est vide. Le même code supprime aussi des lignes de `users` (doublons) et des colonnes à chaque démarrage. Cette partie doit être tranchée avant D5.

**U1 — Comptage des anciennes tables (éditeur SQL Lakebase, base `doccompare`, projet `qualibot`, branche `production`). Avant D5, seules les anciennes tables existent.** :

```sql
SELECT 'messages' AS t, COUNT(*) AS n FROM messages
UNION ALL SELECT 'feedbacks', COUNT(*) FROM feedbacks
UNION ALL SELECT 'errors', COUNT(*) FROM errors
UNION ALL SELECT 'users', COUNT(*) FROM users;
```

**U2 — Inventaire Delta (PowerShell, profil `UAT`)** :

```powershell
databricks tables list uat_landingzone qualibot --profile UAT -o json | ConvertFrom-Json | ForEach-Object { $_.full_name }
databricks volumes list uat_landingzone qualibot --profile UAT -o json | ConvertFrom-Json | ForEach-Object { $_.full_name }
databricks tables list uat_proj qualibot --profile UAT -o json | ConvertFrom-Json | ForEach-Object { $_.full_name }
databricks vector-search-indexes list-indexes qualibot --profile UAT -o json | ConvertFrom-Json | ForEach-Object { $_.name }
```

**U3 — Après le premier démarrage de la nouvelle version (D5 seulement, accord écrit)** : relancer U1. Attendu : `compare_messages` = `messages`, `compare_feedbacks` = `feedbacks`, `app_errors` >= `errors`. Ensuite, vérifier dans l'app que l'historique DocCompare des utilisateurs est visible.

**U4 — Suppressions (après validation complète et accord écrit, à ne pas lancer avant)** : les anciennes tables Lakebase, puis les tables `_v1` et l'ancien schéma, objet par objet, à partir de la liste de U2.

```sql
DROP TABLE IF EXISTS messages;
DROP TABLE IF EXISTS feedbacks;
DROP TABLE IF EXISTS errors;
DROP TABLE IF EXISTS knowledge_base_metadata;
```

**E1 — Enquête REF QM-1041 : document encore remonté alors que absent d'Intraqual (SQL, à lancer sur `uat_proj`, compte utilisateur, lecture seule ; à lancer en premier : la requête 1 de Lakebase, une fois le trace id connu)**

Requête 1 (Lakebase de l'environnement, éditeur SQL) : passages renvoyés pour QM-1041 (remplacer `<trace_id>` ou garder le filtre par REF) :

```sql
SELECT t.created_at, t.trace_id, r.source, r.kept, r.drop_reason, r.iddoc, r.chunk_id, r.best_score
FROM chat_retrieved_chunks r
JOIN chat_turns t ON t.id = r.turn_id
WHERE r.ref = 'QM-1041'
ORDER BY t.created_at DESC
LIMIT 50;
```

Requête 2 (Databricks SQL, `uat_proj`) : l'IDDOC et l'état du document dans le pipeline :

```sql
SELECT 'chunks' AS src, IDDOC, REF, count(*) AS n FROM uat_proj.qualibot.chunks WHERE REF = 'QM-1041' GROUP BY IDDOC, REF
UNION ALL
SELECT 'chunks_archive', IDDOC, REF, count(*) FROM uat_proj.qualibot.chunks_archive WHERE REF = 'QM-1041' GROUP BY IDDOC, REF;

SELECT * FROM uat_proj.qualibot.parsed_documents WHERE IDDOC IN (
  SELECT DISTINCT IDDOC FROM uat_proj.qualibot.chunks WHERE REF = 'QM-1041'
);
```

Requête 3 (Databricks SQL, `prod_bronze`) : le document est-il encore dans la source, sous quel IDDOC, courant ou non :

```sql
SELECT IDDOC, REF, indice, courant, etat, nonvisible, difftotale
FROM prod_bronze.intraqual.gd_doc_latest
WHERE REF = 'QM-1041'
ORDER BY indice DESC;
```

Requête 4 (Databricks SQL, `uat_proj`) : le dernier passage du pipeline a-t-il bien supprimé les anciens IDDOC, et quand l'index a-t-il été synchronisé pour la dernière fois :

```sql
DESCRIBE HISTORY uat_proj.qualibot.chunks LIMIT 15;
DESCRIBE HISTORY uat_proj.qualibot.parsed_documents LIMIT 15;
```

Requête 5 (Databricks SQL, `uat_proj`) : ce que l'index renvoie réellement pour QM-1041 (fonction `vector_search`, à comparer avec la table `chunks`) :

```sql
SELECT chunk_id, REF, IDDOC, left(chunk_text, 120) AS extrait, score
FROM vector_search(
  index => 'uat_proj.qualibot.chunks_index',
  query => 'Design Organisation Manual DOM QM-1041',
  num_results => 50
)
WHERE REF = 'QM-1041';
```

Si la requête 5 renvoie des lignes et la requête 2 aucune ligne, l'index n'a pas été resynchronisé après la suppression.

Requête 6 (Lakebase) : le titre et la date affichés pour cette référence (la source de la fiche, pas de la recherche) :

```sql
SELECT ref, title, revision, doc_date FROM doc_catalog WHERE ref = 'QM-1041';
```

**Lecture des résultats** : (a) requête 3 = aucune ligne → le document a bien quitté la source ; (b) requête 2 contient des lignes en `SUCCESS`/`ERROR`/`EMPTY_TEXT`/`IMAGES_PENDING` → la purge ne l'a pas touché, à corriger ; (c) requête 2 contient des lignes avec un autre `parse_status` (SKIPPED_…) → la purge ne couvre pas ce statut ; (d) requête 5 renvoie QM-1041 et requête 2 n'en a pas → index non resynchronisé (ou run échoué après la purge, la synchronisation n'est pas arrivée) ; (e) requête 1 contient `source = ref_lookup` → la référence est nommée dans la question ou dans un message précédent, et la recherche par référence la reprend directement de l'index, sans contrôle de périmètre.

**Constat E1 (2026-10-19)** : QM-1041 (IDDOC 20605) est `nonvisible = 1` dans `gd_doc_latest`, donc hors périmètre, et absent de `chunks`. L'index le sert encore : sa synchronisation échoue depuis le 2026-10-19 14:20 UTC avec `DELTA_CHANGE_DATA_FEED_INCOMPATIBLE_SCHEMA_CHANGE` (versions 11 à 19 de `chunks`). Cause : `3c_chunks` écrase `chunks` avec `overwriteSchema` puis remet la clé en NOT NULL (`CREATE OR REPLACE` v14 + `CHANGE COLUMN` v15), `4_describe_images` ajoute `anchor_chunk_index` (v17) ; le change data feed ne traverse pas ce changement. `5_sync_index` restait en SUCCESS parce que l'attente ne regardait que `detailed_state`.

**E2 — Corriger le code puis reconstruire `chunks_index` (UAT)** : dans cet ordre. Le code corrigé (`overwrite_rows` dans `tables.py`, écrasement sans changement de schéma ; attente de `update_proj_vector_indexes.py` qui échoue sur `update failed`) doit être déployé AVANT toute nouvelle exécution du pipeline, sinon le prochain écrasement casse le nouvel index.

1. Copier la nouvelle version du dépôt dans `latec-compare`, puis commit et push (PowerShell) :

```powershell
cd C:\Users\L0041770\Desktop\GenAI\Qualibot\latec-compare
git status
git add -A
git commit -m "Chunks: the overwrite keeps the schema (Delta Sync index); the index task fails on a failed update"
git push
```

2. Bitbucket → Pipelines → Run pipeline → branche poussée → pipeline personnalisé `deploy-uat-jobs` (« Deploy jobs and notebooks to UAT, app untouched »). Attendre qu'il soit vert.

3. Importer le notebook de reconstruction (PowerShell, profil `UAT`) :

```powershell
databricks workspace mkdirs /Workspace/Shared/qualibot_uat_index --profile UAT
databricks workspace import /Workspace/Shared/qualibot_uat_index/rebuild_chunks_index --file runbooks/scripts/uat_index/rebuild_chunks_index.py --format SOURCE --language PYTHON --overwrite --profile UAT
```

4. Lancer la reconstruction, exécutée par le compte de service UAT (la recherche du chat est coupée jusqu'à la fin) :

```powershell
databricks jobs submit --json "@runbooks/scripts/uat_index/rebuild_chunks_index_submit.json" --no-wait --profile UAT
```

Noter le `run_id` affiché. Suivre l'état (remplacer `<RUN_ID>`), jusqu'à `TERMINATED` :

```powershell
databricks jobs get-run <RUN_ID> --profile UAT -o json | ConvertFrom-Json | ForEach-Object { $_.state }
```

Si `result_state` n'est pas `SUCCESS`, lire l'erreur :

```powershell
$run = databricks jobs get-run <RUN_ID> --profile UAT -o json | ConvertFrom-Json
databricks jobs get-run-output $run.tasks[0].run_id --profile UAT -o json | ConvertFrom-Json | Select-Object error, error_trace | Format-List
```

5. Vérifier l'index (PowerShell, profil `UAT`), attendu : `indexed_row_count` égal au nombre de lignes de `chunks`, et aucun message d'échec :

```powershell
databricks vector-search-indexes get-index uat_proj.qualibot.chunks_index --profile UAT -o json
```

6. Vérifier dans Databricks SQL (`uat_proj`), attendu : 0 ligne pour les deux requêtes de QM-1041 ; le nombre de la première = `indexed_row_count` du point 5 :

```sql
SELECT count(*) AS chunks_rows FROM uat_proj.qualibot.chunks;

SELECT chunk_id, REF, IDDOC
FROM vector_search(
  index => 'uat_proj.qualibot.chunks_index',
  query => 'Design Organisation Manual DOM QM-1041',
  num_results => 50
)
WHERE IDDOC = 20605;
```

7. Poser dans le chat de l'application UAT la question qui avait fait remonter QM-1041 : la source QM-1041 ne doit plus apparaître.

8. Test du correctif de code : lancer le pipeline complet (le mode `rechunk` du job écrase `chunks`, c'est le cas qui avait cassé la synchronisation), environ 2 h :

```powershell
databricks jobs run-now 762813135325407 --profile UAT
```

Attendu : `5_sync_index` en SUCCESS et, dans Databricks SQL, une ligne `WRITE` avec `operationParameters` `Overwrite` (pas `CREATE OR REPLACE TABLE AS SELECT`) pour `chunks` à la date du run :

```sql
DESCRIBE HISTORY uat_proj.qualibot.chunks LIMIT 10;
```

À marquer fait (daté) dans « Fait » une fois confirmé.

## Fait

_(rien de confirmé pour l'instant)_
