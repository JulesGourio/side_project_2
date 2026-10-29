# Déploiement PROD, pas à pas

Tout se lance depuis Bitbucket sur `main` (compte de service PROD `b82d3b5a-…`), sauf mention contraire. Rien ne supprime de données. **Jamais `--auto-approve` sur un deploy.** Cocher `[x]` avec la date.

## A. Recopier le code dans Bitbucket (`main`)

- [ ] [databricks.yml](../../databricks.yml)
- [ ] [bitbucket-pipelines.yml](../../bitbucket-pipelines.yml)
- [ ] [utils/deploy/prod_state_check.sh](../../utils/deploy/prod_state_check.sh) (nouveau)
- [ ] [utils/deploy/prod_state_fix.sh](../../utils/deploy/prod_state_fix.sh) (nouveau)

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

- [ ] Pipeline **`prod-state-fix`** (`main`). Il détache de l'état les 5 anciens objets de `prod_landingzone.qualibot` (rien n'est supprimé dans Databricks : schéma, tables et fichiers restent), lie ceux de `prod_proj.qualibot` qui existent déjà, et finit par le plan. Doit finir par `OK: no schema or volume is deleted or recreated.`
- [ ] Relancer **`prod-state-check`** : verdict `OK`.

## D. Déployer les jobs (app non touchée)

- [ ] Pipeline **`deploy-prod-jobs`** (`main`). Le plan ne doit montrer que des créations ou mises à jour (jobs, alertes, schéma/volumes `prod_proj` absents).

## E. Données PROD

- [ ] Modèles Docling : si `prod_proj/qualibot/docling_models` est vide (sortie B, section 3), pipeline **`sync-prod-data`** (`main`).
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

## F. Après le deploy

- [ ] Export Lakebase : Workflows > `D_1_Qualibot_Lakebase_Export_To_Volume` > Run now (les alertes du chat lisent ses copies).
- [ ] Jobs MLflow : Workflows > `Z_2_Qualibot_Create_Chat_Experiment` > Run now, puis `Z_3_Qualibot_Register_Chat_Prompts` > Run now.
- [ ] Alertes : SQL > Alerts, les 6 alertes `qualibot` ont pour seul abonné `jules.gourio.external@latecoere.aero`.
- [ ] Parsing, premier run : Workflows > `D_1_Qualibot_Parsing_Pipeline` > Run now (paramètres par défaut, `incremental`). S'il s'arrête sur `[BUDGET]`, l'état n'est pas reconnu : ne pas relever `max_parse_files`, envoyer le log.
- [ ] Planning du parsing : dans [databricks.yml](../../databricks.yml), target `prod`, `parsing_schedule_pause: "UNPAUSED"`, puis **`deploy-prod-jobs`**.

## G. Plus tard

- [ ] App PROD : pipeline **`deploy-prod`** (redéploie et redémarre l'app), sur décision.
- [ ] Ancien schéma `prod_landingzone.qualibot` : supprimé à la main seulement après validation complète de PROD (tables d'abord, volumes ensuite après contrôle de leur contenu).
