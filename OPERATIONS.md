# Opérations manuelles (Jules)

Claude n'a pas accès à Databricks : chaque étape à faire à la main (côté
machine de déploiement ou UI Databricks) est listée ici, prête à
copier-coller. Cocher et dater une fois faite.

PowerShell, depuis la racine du projet sur la machine de déploiement.

## À faire

### Impact search v2 + index complet avec archive < 2018 (2026-10-02)

Le code change deux choses :

- **L'app** : nouvelle impact search (une requête par changement, un jugement
  par document, affichage par document ou par changement, export Excel). Elle
  fonctionne tout de suite sur l'index actuel `chunks_index_v1`.
- **Le pipeline de parsing** : les documents d'avant 2018 sont maintenant parsés
  (~1 500 documents, **gros run GPU la première fois**). Leurs chunks vont dans
  `chunks_archive_v1`, pas dans les tables du chatbot. L'étape `5_sync_index`
  construit `chunks_full_v1` (récents + archive) et crée l'index
  `chunks_full_index_v1` sur l'endpoint `qualibot`.

- [ ] **0. Tester d'abord sur `qualibot-uat-test`** (app seule, pas de `-Infra` :
  le pipeline de parsing n'existe pas sur cette cible, et l'app y utilise déjà
  l'index partagé `chunks_index_v1`). Le script ne démarre jamais cette app :
  la démarrer d'abord si elle est arrêtée.

  ```powershell
  Remove-Item Env:DATABRICKS_TOKEN -ErrorAction SilentlyContinue
  databricks apps start qualibot-uat-test --profile UAT
  .\utils\deploy\deploy_qualibot.ps1 -AppEnv uat-test
  ```

  À vérifier : comparaison de deux fichiers, puis « Judge Impacted Docs » — les
  documents arrivent un par un, « Show passages » montre la phrase surlignée,
  la vue « By change » et « Export Excel » fonctionnent. Les documents d'avant
  2018 n'apparaîtront pas encore (étapes 3 à 5).

- [ ] **1. Déployer sur `qualibot-uat` avec `-Infra`** (la liste d'index du job a
  changé, donc la définition du job doit être redéployée) :

  ```powershell
  Remove-Item Env:DATABRICKS_TOKEN -ErrorAction SilentlyContinue
  .\utils\deploy\deploy_qualibot.ps1 -AppEnv uat -Infra
  ```

- [ ] **2. Tester la nouvelle impact search sur `qualibot-uat`** : refaire une
  comparaison déjà connue, cliquer « Judge Impacted Docs ». Les documents doivent
  apparaître au fil de l'eau ; « Show passages » doit montrer le texte surligné.

- [ ] **3. Lancer le pipeline de parsing** (sinon il tourne cette nuit à 4 h, le
  planning est actif sur `qualibot-uat`) :

  ```powershell
  databricks bundle run parsing_pipeline --target qualibot-uat --profile UAT
  ```

- [ ] **4. Vérifier le résultat** (SQL editor, workspace UAT) :

  ```sql
  -- documents d'archive parsés et chunks produits
  SELECT count(DISTINCT IDDOC) AS docs, count(*) AS chunks
  FROM uat_landingzone.qualibot.chunks_archive_v1;

  -- chunks_full = chunks_v1 + chunks_archive_v1
  SELECT (SELECT count(*) FROM uat_landingzone.qualibot.chunks_full_v1) AS full,
         (SELECT count(*) FROM uat_landingzone.qualibot.chunks_v1)
       + (SELECT count(*) FROM uat_landingzone.qualibot.chunks_archive_v1) AS expected;

  -- documents d'avant 2018 encore non parsés (doit tendre vers 0)
  SELECT parse_status, count(*) FROM uat_landingzone.qualibot.processed_files_v1
  WHERE filtered_by_date GROUP BY parse_status;
  ```

  Puis, dans l'UI Vector Search (endpoint `qualibot`), vérifier que
  `uat_landingzone.qualibot.chunks_full_index_v1` est `ONLINE`.

- [ ] **5. Me prévenir** : je bascule `COMPARE_IMPACT_INDEX` sur
  `chunks_full_index_v1` dans `utils/deploy/target_env.json` (uat et uat-test).
  Tu redéploies ensuite l'app seule (sans `-Infra`) :

  ```powershell
  .\utils\deploy\deploy_qualibot.ps1 -AppEnv uat
  .\utils\deploy\deploy_qualibot.ps1 -AppEnv uat-test
  ```

  Si l'impact search renvoie une erreur 403 sur l'index, donner l'accès au
  service principal de l'app :

  ```sql
  GRANT SELECT ON TABLE uat_landingzone.qualibot.chunks_full_index_v1 TO `<application id du SP de l'app qualibot>`;
  ```

## Fait
