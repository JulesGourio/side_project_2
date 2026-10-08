# Opérations manuelles (Jules)

Claude n'a pas accès à Databricks : chaque étape à faire à la main (côté
machine de déploiement ou UI Databricks) est listée ici, prête à
copier-coller. Cocher et dater une fois faite.

PowerShell, depuis la racine du projet sur la machine de déploiement.
Tout le code est sur la branche **`audit/doc-compare`** (= `main` + audit de la
comparaison + phase de test archive). Le zip à déployer est celui de cette
branche tant qu'elle n'est pas fusionnée.

Mis à jour le 2026-10-05. Rien de ce qui suit n'a encore été confirmé comme fait.

La mise en place de l'environnement **DEV** a son propre fichier : `operations_dev.md`.

> **Branche `feature/chat-vsi-merged-on-impact-search` (2026-10-08) : ne la déployer sur
> `qualibot-uat` / `qualibot-uat-test` qu'au bloc D5, dans son ordre.** Son code attend des tables
> et un index **sans suffixe** (`chunks`, `chunks_index`, `_pipeline_checkpoint`…) et n'a plus de
> Knowledge Assistant. Un `bundle deploy` de cette branche avant D5 ferait repartir le pipeline
> UAT de tables vides (re-parsing GPU de tout le corpus) et l'app chercherait un index qui
> n'existe pas encore. Les blocs A à C ci-dessous restent sur `audit/doc-compare`.

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

- [ ] **A5. Mesurer le bruit sur le corpus d'évaluation** (machine où se trouve
  `utils/compare_eval`), avec et sans le contrôle mot à mot :

  ```powershell
  python utils\compare_preview.py <ancien> <nouveau> --score
  $env:COMPARE_WORD_LEVEL_CHECK = 'false'; python utils\compare_preview.py <ancien> <nouveau> --score
  Remove-Item Env:COMPARE_WORD_LEVEL_CHECK
  ```

  Même comparaison pour l'extraction PDF (`COMPARE_PDF_MERGE_PAGE_SPLITS`,
  `COMPARE_PDF_TABLE_LABELS` à `false`), surtout sur MOP_AX et les WDT.

- [ ] **A6. Me dire si on fusionne `audit/doc-compare` dans `main`.**

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

Après A6 (fusion décidée). App seule : pas de `-Infra`.

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
  `uat_landingzone.qualibot.chunks_full_index` à
  `parsing_vector_search_indexes` ; `bundle deploy` + un run le créent. Une fois
  l'index `ONLINE`, je bascule `COMPARE_IMPACT_INDEX` (uat + uat-test) dans
  `utils/deploy/target_env.json`, puis :

  ```powershell
  .\utils\deploy\deploy_qualibot.ps1 -AppEnv uat
  .\utils\deploy\deploy_qualibot.ps1 -AppEnv uat-test
  ```

  En cas d'erreur 403 sur l'index :

  ```sql
  GRANT SELECT ON TABLE uat_landingzone.qualibot.chunks_full_index TO `<application id du SP de l'app qualibot>`;
  ```

- [ ] **D3. Règle « documents archivés » dans le chatbot** (avant D4) : la règle écrite pour le
  KA (`archive/knowledge_assistant/provisioning/ka_profiles.py`) doit passer dans
  `server/config/chat_vsi/answer_rules.md`. Me prévenir : je l'ajoute, vous redéployez l'app.

- [ ] **D4. Mettre les fiches dans l'index du chatbot** (après D3 et D5) : je passe
  `parsing_archive_notices_in_rag` à `true` pour `qualibot-uat` ; `bundle deploy` + un run les
  ajoutent à `chunks` et l'index se synchronise. Tester ensuite dans le chatbot :
  - demander un vieux document par sa référence : il le signale, dit que le contenu n'est pas
    disponible, renvoie à Intraqual (lien sous la réponse) ;
  - poser une question sans rapport : aucune fiche citée.

  Retour arrière : repasser à `false`, `bundle deploy` + un run retirent les fiches.

- [ ] **D5. Passer l'UAT sur le chatbot retenu** : un seul chatbot (plus de KA), un seul index
  `chunks_index` avec filtre de division, le découpage retenu (150 / 300 / 450 tokens), plus aucun
  nom versionné. Tout est mesuré en DEV (`docs/chat_vsi_tests.md`, partie 1) et doit d'abord être
  passé en DEV (`operations_dev.md`, bloc S). **Avec ton accord seulement** : c'est la vraie app.
  Les tables et index `_v1` restent en place jusqu'à D5.9 : l'app actuelle continue de tourner
  pendant D5.1 à D5.5, et le retour arrière reste possible.

  - [ ] **D5.1. Mettre en pause le planning du pipeline UAT** (UI UAT → Jobs → le job de parsing
    `qualibot` → *Pause*), pour qu'aucun run ne parte pendant les copies.

  - [ ] **D5.2. Inventaire** (éditeur SQL UAT) :

    ```sql
    SHOW TABLES IN uat_landingzone.qualibot;
    ```

  - [ ] **D5.3. Tables d'état du pipeline sans suffixe** : copies (pas de renommage : l'app et le
    job actuels gardent leurs tables jusqu'à D5.9), avec 60 jours d'historique :

    ```sql
    CREATE TABLE uat_landingzone.qualibot._pipeline_checkpoint DEEP CLONE uat_landingzone.qualibot._pipeline_checkpoint_v1;
    CREATE TABLE uat_landingzone.qualibot.processed_files      DEEP CLONE uat_landingzone.qualibot.processed_files_v1;
    CREATE TABLE uat_landingzone.qualibot.image_metadata       DEEP CLONE uat_landingzone.qualibot.image_metadata_v1;
    CREATE TABLE uat_landingzone.qualibot.parse_manifest       DEEP CLONE uat_landingzone.qualibot.parse_manifest_v1;
    CREATE TABLE uat_landingzone.qualibot.category_reference   DEEP CLONE uat_landingzone.qualibot.category_reference_v1;
    CREATE TABLE uat_landingzone.qualibot.parsing_run_health   DEEP CLONE uat_landingzone.qualibot.parsing_run_health_v1;
    CREATE TABLE uat_landingzone.qualibot.document_change_log  DEEP CLONE uat_landingzone.qualibot.document_change_log_v1;
    CREATE TABLE uat_landingzone.qualibot.audit_files_unified  DEEP CLONE uat_landingzone.qualibot.audit_files_unified_v1;
    ```

    (Une table absente de D5.2 : sauter sa ligne.) Puis, pour chacune :

    ```sql
    ALTER TABLE uat_landingzone.qualibot._pipeline_checkpoint SET TBLPROPERTIES (
      'delta.deletedFileRetentionDuration' = 'interval 60 days', 'delta.logRetentionDuration' = 'interval 60 days');
    ```

  - [ ] **D5.4. Re-découper le corpus dans `chunks`** (aucun re-parsing GPU : les fichiers déjà
    parsés sont relus depuis `_pipeline_checkpoint` ; les passages d'image sont reconstruits depuis
    les descriptions existantes, sans appel LLM). Copier le zip de cette branche, puis :

    ```powershell
    databricks bundle deploy -t qualibot-uat --profile UAT --var="parsing_run_mode=full"
    databricks bundle run parsing_pipeline -t qualibot-uat --profile UAT
    ```

    Le run écrit `chunks`, `chunks_archive`, `processed_files`, puis sa dernière tâche crée l'index
    `uat_landingzone.qualibot.chunks_index` et le synchronise (embedding complet, environ 1 h ; si
    la tâche s'arrête avant, la synchronisation continue côté serveur). Attendre `ONLINE`.

  - [ ] **D5.5. Revenir au mode quotidien** (le planning repart, sur les nouvelles tables) :

    ```powershell
    databricks bundle deploy -t qualibot-uat --profile UAT
    ```

  - [ ] **D5.6. Droits du SP de l'app UAT** (`qualibot`, et celui de `qualibot-uat-test`) :
    - `SELECT` sur l'index :

      ```sql
      GRANT SELECT ON TABLE uat_landingzone.qualibot.chunks_index TO `<application id du SP de l'app qualibot>`;
      ```

    - **Can Query** sur `databricks-gpt-6-luna` et `databricks-gpt-5-6-luna` (UI UAT → Serving →
      l'endpoint → Permissions).

  - [ ] **D5.7. Déployer l'app** (onglet unique « Chat », index `chunks_index` pour le chat et
    l'impact search) :

    ```powershell
    .\utils\deploy\deploy_qualibot.ps1 -AppEnv uat
    .\utils\deploy\deploy_qualibot.ps1 -AppEnv uat-test
    ```

  - [ ] **D5.8. Tester** comme `operations_dev.md` S9 (ALL / AS / IS, langue, hors sujet, lien,
    impact search). Retour arrière : redéployer l'ancien zip, puis `bundle deploy` de l'ancien zip
    (tables et index `_v1` intacts).

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
      ALTER TABLE uat_landingzone.qualibot.synthetic_retrieval_questions_v2
        RENAME TO uat_landingzone.qualibot.synthetic_retrieval_questions;
      ```

    - **avant** de supprimer les index : me prévenir, je passe sur `chunks_index` (+ filtre de
      division) les deux notebooks de notation qui interrogent encore les index `_v1`
      (`utils/databricks_ops/evaluation/score_production_qa.py`, job DEV ;
      `utils/quality_monitoring/Score_Production_QA.py`, job UAT) ;
    - les jobs UAT qui lisent les traces du KA (`resources/traces_migration.yml`,
      `sync_mlflow_scorer_assessments_uat`) : à revoir ensemble, ils ne sont pas modifiés par cette
      branche.

## Fait

_(rien de confirmé pour l'instant)_
