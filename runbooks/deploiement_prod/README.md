# Déploiement PROD, pas à pas

Tout se lance depuis Bitbucket sur `main` (compte de service PROD `b82d3b5a-…`), sauf mention contraire. Rien ne supprime de données. **Jamais `--auto-approve` sur un deploy.** Cocher `[x]` avec la date.

## A. Recopier le code dans Bitbucket (`main`)

- [x] [databricks.yml](../../databricks.yml) _(2026-10-29)_
- [x] [bitbucket-pipelines.yml](../../bitbucket-pipelines.yml)
- [x] [utils/deploy/prod_state_check.sh](../../utils/deploy/prod_state_check.sh) (nouveau)
- [x] [utils/deploy/prod_state_fix.sh](../../utils/deploy/prod_state_fix.sh) (nouveau)

Ce que change `databricks.yml` en PROD : alertes et échecs de jobs envoyés à `jules.gourio.external@latecoere.aero` seul ; `prevent_destroy` sur le schéma et les volumes ; parsing quotidien `PAUSED` tant que l'état de parsing n'est pas importé (étape E).

## B. État des lieux (lecture seule)

- [ ] Pipelines > Run pipeline > branche `main` > **`prod-state-check`**. Coller la sortie à Claude.

Ce qu'il affiche :
1. L'état du bundle : vers quoi pointe chaque schéma/volume. `prod_landingzone.qualibot…` = ancien objet (cause du blocage du pipeline #195).
2. Ce qui existe dans `prod_proj.qualibot` et `prod_landingzone.qualibot` (volumes, tables).
3. Le contenu des volumes `docling_models`.
4. L'état de l'app.
5. Le plan du deploy, puis le verdict `OK` ou `DESTRUCTIVE`.

Contrôle en plus avec ton profil (lecture seule, PowerShell) :

```powershell
databricks schemas get prod_proj.qualibot --profile PROD
databricks volumes list prod_proj qualibot --profile PROD -o json | ConvertFrom-Json | ForEach-Object { $_.full_name }
databricks volumes list prod_landingzone qualibot --profile PROD -o json | ConvertFrom-Json | ForEach-Object { $_.full_name }
databricks fs ls dbfs:/Volumes/prod_landingzone/qualibot/docling_models --profile PROD
databricks fs ls dbfs:/Volumes/prod_landingzone/qualibot/doc_compare --profile PROD
```

Contenu des volumes (ton profil, lecture seule). Antiword est livré avec le code (`third_party/antiword`), rien à garder pour lui dans un volume.

```powershell
databricks fs ls dbfs:/Volumes/prod_proj/qualibot/docling_models --profile PROD
databricks fs ls dbfs:/Volumes/prod_landingzone/qualibot/docling_models --profile PROD
databricks fs ls dbfs:/Volumes/uat_proj/qualibot/docling_models --profile UAT
databricks fs ls dbfs:/Volumes/prod_proj/qualibot/doc_compare/libreoffice --profile PROD
databricks fs ls dbfs:/Volumes/prod_landingzone/qualibot/doc_compare --profile PROD
databricks fs ls dbfs:/Volumes/prod_landingzone/qualibot/test --profile PROD
python utils/deploy/render_target_config_env.py prod
databricks bundle summary -t prod --profile PROD
databricks bundle plan -t prod --profile PROD
```

Attendu : `docling_models` de `prod_proj` rangé comme celui d'UAT (mêmes dossiers à la racine) ; `prod_proj/.../doc_compare/libreoffice/libreoffice-25.8.7-linux-x64-v3.tar.gz` présent (aperçu PDF exact de la comparaison). `summary` et `plan` ne changent rien ; s'ils sont refusés à ton compte, passer par `prod-state-check`.

## C. Réparer l'état du bundle (seulement si B dit `DESTRUCTIVE`)

- [x] _(2026-10-30, plan local : 0 delete, 0 recreate)_ Pipeline **`prod-state-fix`** (`main`). Il détache de l'état les 5 anciens objets de `prod_landingzone.qualibot` (rien n'est supprimé dans Databricks : schéma, tables et fichiers restent), lie ceux de `prod_proj.qualibot` qui existent déjà, et finit par le plan. Doit finir par `OK: no schema or volume is deleted or recreated.`
- [ ] Relancer **`prod-state-check`** : verdict `OK`.

## D. Déployer les jobs (app non touchée)

- [ ] Pipeline **`deploy-prod-jobs`** (`main`). Le plan ne doit montrer que des créations ou mises à jour (jobs, alertes, schéma/volumes `prod_proj` absents).

## E. Données PROD

- [x] _(2026-10-29 : modèles présents dans prod_proj, rien à copier)_ Modèles Docling : si `prod_proj/qualibot/docling_models` est vide (sortie B, section 3), pipeline **`sync-prod-data`** (`main`).
- [ ] État de parsing (évite de re-parser tout le corpus sur GPU) :
  1. UAT, ton compte : importer et lancer [1_uat_export_pour_prod.py](1_uat_export_pour_prod.py) (hors run de parsing UAT).
  2. Ta machine :
     ```powershell
     Remove-Item -Recurse -Force .\prod_staging -ErrorAction SilentlyContinue
     New-Item -ItemType Directory -Force prod_staging | Out-Null
     databricks fs cp -r dbfs:/Volumes/uat_proj/qualibot/staging/prod_copy .\prod_staging\prod_copy --profile UAT
     databricks fs cp -r .\prod_staging\prod_copy dbfs:/Volumes/prod_proj/qualibot/staging/prod_copy --overwrite --profile PROD
     databricks fs ls dbfs:/Volumes/prod_proj/qualibot/staging/prod_copy --profile PROD
     ```
     Attendu : `manifest.json` et `tables`.
  3. PROD, **compte de service PROD** : [2_prod_import_depuis_uat.py](2_prod_import_depuis_uat.py) (Workflows > Create job > tâche notebook = ce notebook > Run as = `b82d3b5a-b633-4738-8dde-68bb2142cb02` > Run now). Le log compare les lignes à l'export.

## F. Mise en service (dans l'ordre)

- [ ] 1. Run de parsing `full` terminé (Repair run de `3c_chunks` et des tâches suivantes) : `5_sync_index` crée `prod_proj.qualibot.chunks_index`. Si `6_update_kb_metadata` échoue sur la base Lakebase `doccompare` absente : faire l'étape 6, puis Repair run de `6_update_kb_metadata`.
- [x] _(2026-10-30, 16:42 : export UAT de 16:09, comptes identiques sur les 13 tables)_ 1b. Conversations et historique d'UAT vers le Lakebase PROD, après le premier démarrage de l'app PROD (étape 6, qui crée les tables) et avant l'étape 2 : [copie_lakebase_uat_vers_prod.py](copie_lakebase_uat_vers_prod.py).
  ```powershell
  python runbooks/deploiement_prod/copie_lakebase_uat_vers_prod.py
  python runbooks/deploiement_prod/copie_lakebase_uat_vers_prod.py --apply
  databricks fs ls dbfs:/Volumes/uat_proj/qualibot/doc_compare --profile UAT
  Remove-Item -Recurse -Force .\doc_compare_uat -ErrorAction SilentlyContinue
  databricks fs cp -r dbfs:/Volumes/uat_proj/qualibot/doc_compare .\doc_compare_uat --profile UAT
  Get-ChildItem .\doc_compare_uat -Directory | Where-Object { $_.Name -ne 'libreoffice' } | ForEach-Object { databricks fs cp -r $_.FullName "dbfs:/Volumes/prod_proj/qualibot/doc_compare/$($_.Name)" --overwrite --profile PROD }
  Get-ChildItem .\doc_compare_uat -File | ForEach-Object { databricks fs cp $_.FullName "dbfs:/Volumes/prod_proj/qualibot/doc_compare/$($_.Name)" --overwrite --profile PROD }
  ```
- [ ] 2. Workflows > `D_1_Qualibot_Lakebase_Export_To_Volume` > Run now : copie Delta de toutes les tables Lakebase dans `prod_proj.qualibot` (sauvegarde, Lakebase non modifié).
- [ ] 3. Workflows > `Z_2_Qualibot_Create_Chat_Experiment` > Run now, puis `Z_3_Qualibot_Register_Chat_Prompts` > Run now. Envoyer à Claude l'identifiant d'expérience du log de `Z_2` : il va dans `utils/deploy/target_env.json` (`CHAT_MLFLOW_EXPERIMENT_ID` de `prod`).
- [ ] 4. Droits de l'app PROD, après l'étape 1 (index créé) : seul `SELECT` sur `prod_proj.qualibot.chunks_index` manque ; le volume `doc_compare` (avec `USE CATALOG` / `USE SCHEMA`) est donné par `deploy-prod`, les tables de traces MLflow par `Z_2`. [3_prod_grant_app.py](3_prod_grant_app.py), compte de service PROD (Workflows > Create job > tâche notebook = ce notebook > Run as = `b82d3b5a-b633-4738-8dde-68bb2142cb02` > Run now), ou à la main plus tard par un administrateur : `GRANT SELECT ON TABLE prod_proj.qualibot.chunks_index TO \`42ba83dd-3098-4d90-b2d2-a5df7d8beb1c\`;`
- [ ] 5. Administrateur du metastore (pas le compte de service), éditeur SQL de PROD :
  ```sql
  GRANT USE CATALOG ON CATALOG system TO `b82d3b5a-b633-4738-8dde-68bb2142cb02`;
  GRANT USE SCHEMA ON SCHEMA system.access TO `b82d3b5a-b633-4738-8dde-68bb2142cb02`;
  GRANT SELECT ON TABLE system.access.audit TO `b82d3b5a-b633-4738-8dde-68bb2142cb02`;
  GRANT USE SCHEMA ON SCHEMA system.lakeflow TO `b82d3b5a-b633-4738-8dde-68bb2142cb02`;
  GRANT SELECT ON SCHEMA system.lakeflow TO `b82d3b5a-b633-4738-8dde-68bb2142cb02`;
  ```
  Sans eux : pas de synchronisation des groupes (`D_2_Qualibot_User_Capabilities_Sync`) ni d'alerte `job_failures`.
- [ ] 6. App : recopier `utils/deploy/target_env.json` (avec l'identifiant de l'étape 3), puis pipeline **`deploy-prod`** (`main`) : redéploie les jobs (planning du parsing actif, 04:00) et redémarre l'app.
- [ ] 7. Contrôle : `https://qualibot-1210945332048776.aws.databricksapps.com/api/health` renvoie `"lakebase": "ready"` ; une question au chat avec sources, une comparaison docx avec l'aperçu PDF exact, une recherche d'impact.
- [ ] 8. Alertes : SQL > Alerts, les 6 alertes `Qualibot - …` ont pour seul abonné `jules.gourio.external@latecoere.aero`.
- [ ] 9. Bitbucket : Repository settings > Deployments > `Production` : déploiement limité à la branche `main`.

## G. Plus tard

- [ ] Ancien schéma `prod_landingzone.qualibot` : supprimé à la main seulement après validation complète de PROD (tables d'abord, volumes ensuite après contrôle de leur contenu).
