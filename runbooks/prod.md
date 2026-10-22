# Opérations manuelles — PROD (`prod_proj.qualibot`)

Cible : `prod` (profil CLI `PROD`). Rien ici ne déploie : le déploiement prod n'est jamais lancé sans accord explicite. Les commandes sont à taper à la main, dans l'ordre.

Le catalogue `prod_landingzone.intraqual` (source Intraqual) ne se touche pas.

## Ressources PROD

| Quoi | Lien / commande |
|---|---|
| App `qualibot` (utilisateurs) | https://qualibot-1210945332048776.aws.databricksapps.com |
| App — état | `databricks apps get qualibot --profile PROD` |

## 0. État de départ (lecture seule, profil `PROD`)

```powershell
databricks catalogs list --profile PROD -o json | ConvertFrom-Json | ForEach-Object { $_.name } | Select-String "prod_"
databricks schemas list prod_landingzone --profile PROD -o json | ConvertFrom-Json | ForEach-Object { $_.full_name }
databricks tables list prod_landingzone qualibot --profile PROD -o json | ConvertFrom-Json | ForEach-Object { $_.full_name }
databricks volumes list prod_landingzone qualibot --profile PROD -o json | ConvertFrom-Json | ForEach-Object { $_.full_name }
databricks vector-search-indexes list-indexes qualibot --profile PROD -o json | ConvertFrom-Json | ForEach-Object { $_.name }
```

Attendu : `prod_proj` existe ou non. Si l'ancien schéma `prod_landingzone.qualibot` contient des tables, des volumes ou un index, les étapes 2, 3 et 4 s'appliquent (copie) ; sinon elles sont sans objet.

## 0.1 Politique de cluster « S » de PROD — fait (2026-10-15)

Identifiant `001C199A8D85FE35` confirmé sur le workspace prod (`databricks cluster-policies get 001C199A8D85FE35 --profile PROD`) : politique S attendue (`activity-type` = `prod-job`, `cluster-policy` = `S`, `SPOT_WITH_FALLBACK` fixé avec `first_on_demand` minimum 1, `auto:latest-lts`, nœud `.xlarge`). Aucune action à faire.

## 0.2 Remise à zéro avant la bascule (décidée 2026-10-22, à faire avant l'étape 6)

Garde : l'app `qualibot`, le projet Lakebase `qualibot` (+ rôles, schéma), le endpoint Vector Search `qualibot`, les volumes `docling_models` et `doc_compare` (tarball LibreOffice), `prod_landingzone.intraqual`. Supprime : jobs, agents/endpoints liés, index, tables de `prod_proj.qualibot`, schéma `prod_landingzone.qualibot`, données d'usage Lakebase. Scripts : `runbooks/scripts/prod_cleanup/`.

1. Inventaire (2026-10-22, profil `PROD`) : jobs à supprimer = `qualibot-provision-knowledge-assistant-qualibot-prod` et `D_1_qualibot-parsing-pipeline-qualibot-prod` (les jobs `*intraqual*` ne sont pas à nous) ; endpoints `ka-3315943a-endpoint`, `ka-5d35f4d5-endpoint`, `ka-88a4da35-endpoint`, `ka-c9dbc4db-endpoint` ; l'endpoint Vector Search `qualibot` n'existe pas sur PROD ; `prod_proj.qualibot` n'existe pas ; les 4 volumes (`doc_compare`, `docling_models`, `images`, `test`) ne sont que dans `prod_landingzone.qualibot`.
2. Notebook `drop_prod_objects` lancé par le SP prod (`b82d3b5a-b633-4738-8dde-68bb2142cb02`) : phase `prepare` (crée `prod_proj.qualibot` + volumes, copie `docling_models` et `doc_compare`), puis phase `drop`, chacune en `dry_run=true` puis `false`.
2b. Créer l'endpoint Vector Search `qualibot` sur PROD (absent).
3. `reset_prod_lakebase.py` : comptage, puis `--apply`.
4. Valeurs à ajouter dans `utils/deploy/target_env.json` pour `prod` avant le déploiement : `SOFFICE_ARCHIVE_VOLUME_PATH`, puis `CHAT_MLFLOW_EXPERIMENT_ID` une fois `create_chat_experiment_prod` lancé.
5. Droits de l'étape 5 à passer par un `jobs submit` en SP prod, pas en SQL à la main.

## 0.3 Remise à zéro de PROD puis copie du corpus de DEV (décidée 2026-10-22, pas à pas)

Garde : l'app `qualibot` (son adresse ne change pas : le déploiement la met à jour sur place), le projet Lakebase `qualibot` (non touché : `reset_prod_lakebase.py` n'est lancé que sur décision explicite), `prod_landingzone.intraqual`, les volumes `docling_models` et `doc_compare`. Tout se lance par un compte de service PROD (`b82d3b5a-b633-4738-8dde-68bb2142cb02`), jamais en SQL personnel. Chaque script supprimant ou copiant démarre en simulation (`dry_run = true`) : lire ses logs avant de relancer en `false`.

**P0. Le profil `PROD` pointe sur PROD** (lecture seule). Doit afficher l'adresse `qualibot-1210945332048776` ; sinon corriger l'hôte du profil avant tout :

```powershell
databricks apps get qualibot --profile PROD -o json | ConvertFrom-Json | Select-Object name, url
```

**P1. Sonde d'accès** (lecture seule) : PROD peut-il lire `dev_proj` ? Le log donne `READ OK` ou `READ NO` pour `dev`, `uat` et `prod`, plus la liste des tables de `prod_proj.qualibot` et `prod_landingzone.qualibot`.

```powershell
databricks workspace mkdirs /Workspace/Shared/qualibot_prod_copy --profile PROD
databricks workspace import /Workspace/Shared/qualibot_prod_copy/probe_access --file runbooks/scripts/prod_copy/probe_access.py --format SOURCE --language PYTHON --overwrite --profile PROD
databricks jobs submit --json "@runbooks/scripts/prod_copy/probe_access_submit.json" --profile PROD
```

- `READ OK  dev_proj.qualibot.chunks` : continuer avec P2 à P6.
- `READ NO` : la copie passe par un export sur un volume lisible des deux côtés ; script à écrire, ne pas continuer sans lui.

**P2. Anciens jobs et endpoints** (jobs `qualibot-provision-knowledge-assistant-qualibot-prod`, `D_1_qualibot-parsing-pipeline-qualibot-prod`, endpoints `ka-3315943a-endpoint`, `ka-5d35f4d5-endpoint`, `ka-88a4da35-endpoint`, `ka-c9dbc4db-endpoint`). Simulation, puis suppression en remplaçant `"dry_run": "true"` par `"false"` dans `drop_prod_legacy_submit.json` :

```powershell
databricks workspace mkdirs /Workspace/Shared/qualibot_prod_cleanup --profile PROD
databricks workspace import /Workspace/Shared/qualibot_prod_cleanup/drop_prod_legacy --file runbooks/scripts/prod_cleanup/drop_prod_legacy.py --format SOURCE --language PYTHON --overwrite --profile PROD
databricks jobs submit --json "@runbooks/scripts/prod_cleanup/drop_prod_legacy_submit.json" --profile PROD
```

**P3. Tables, index et ancien schéma** (`prod_proj.qualibot` : toutes les tables sauf `…_otel_*`, index, schéma `prod_landingzone.qualibot`). Ordre : phase `prepare` (crée le schéma et les volumes, copie `docling_models` et `doc_compare`) en simulation puis réelle, puis phase `drop` en simulation puis réelle. Les phases et `dry_run` se changent dans `base_parameters` de `drop_prod_objects_submit.json` :

```powershell
databricks workspace import /Workspace/Shared/qualibot_prod_cleanup/drop_prod_objects --file runbooks/scripts/prod_cleanup/drop_prod_objects.py --format SOURCE --language PYTHON --overwrite --profile PROD
databricks jobs submit --json "@runbooks/scripts/prod_cleanup/drop_prod_objects_submit.json" --profile PROD
```

**P4. Copie du corpus et de l'état de parsing de DEV** (`category_reference`, `parse_manifest`, `parsing_checkpoint`, `image_metadata`, `parsed_documents`, `chunks_archive`, `chunks`). Les chemins de volume stockés (`dev_proj`) sont réécrits en `prod_proj`, le nombre de lignes est contrôlé table par table. Simulation d'abord (paramètres par défaut), puis `"dry_run": "false"` dans `copy_tables_to_prod_submit.json` :

```powershell
databricks workspace import /Workspace/Shared/qualibot_prod_copy/copy_tables_to_prod --file runbooks/scripts/prod_copy/copy_tables_to_prod.py --format SOURCE --language PYTHON --overwrite --profile PROD
databricks jobs submit --json "@runbooks/scripts/prod_copy/copy_tables_to_prod_submit.json" --profile PROD
```

Limite connue : les fichiers d'images restent dans le volume `images` de DEV ; le volume `images` de PROD démarre vide. Seule la reprise d'une image en échec les relit.

**P5. Endpoint Vector Search** (absent sur PROD), puis modèles Docling depuis l'UAT (pipeline Bitbucket `sync-prod-data`, deux étapes, depuis `main`) :

```powershell
databricks vector-search-endpoints create-endpoint qualibot STANDARD --profile PROD
```

**P6. Suite** : sections 4 à 6 et 9 ci-dessous (droits par le SP, valeurs de `target_env.json`, déploiement **sur accord explicite**). Après le déploiement, `databricks bundle run parsing_pipeline -t prod --profile PROD --params run_mode=rechunk` reconstruit `chunks` depuis le checkpoint copié, crée `prod_proj.qualibot.chunks_index`, le synchronise et remplit le catalogue de documents, sans re-parsing.

- [ ] P0 · [ ] P1 · [ ] P2 · [ ] P3 · [ ] P4 · [ ] P5 · [ ] P6

## 1. Catalogue et schéma `prod_proj` (admin metastore, seulement si absent)

Si `prod_proj` n'existe pas, un admin le crée (SQL, éditeur du workspace prod) :

```sql
CREATE CATALOG IF NOT EXISTS prod_proj;
CREATE SCHEMA IF NOT EXISTS prod_proj.qualibot;
```

Le déploiement crée aussi le schéma `prod_proj.qualibot` ; ces deux lignes servent seulement si le catalogue manque.

## 2. Volumes et données (PowerShell, profil `PROD`)

Créer les volumes dans `prod_proj` :

```powershell
databricks volumes create prod_proj qualibot doc_compare MANAGED --profile PROD
databricks volumes create prod_proj qualibot test MANAGED --profile PROD
databricks volumes create prod_proj qualibot images MANAGED --profile PROD
databricks volumes create prod_proj qualibot docling_models MANAGED --profile PROD
```

Copier les données depuis l'ancien emplacement (ignorer une ligne si la source n'existe pas, voir étape 0) :

```powershell
databricks fs cp -r dbfs:/Volumes/prod_landingzone/qualibot/docling_models dbfs:/Volumes/prod_proj/qualibot/docling_models --profile PROD
databricks fs cp -r dbfs:/Volumes/prod_landingzone/qualibot/doc_compare dbfs:/Volumes/prod_proj/qualibot/doc_compare --profile PROD
databricks fs cp -r dbfs:/Volumes/prod_landingzone/qualibot/test dbfs:/Volumes/prod_proj/qualibot/test --profile PROD
databricks fs cp -r dbfs:/Volumes/prod_landingzone/qualibot/images dbfs:/Volumes/prod_proj/qualibot/images --profile PROD
```

Le volume `staging` n'est pas copié. Le bundle déclare `doc_compare`, `test`, `images`, `docling_models` pour prod : avant tout déploiement, lier les volumes créés pour éviter qu'il tente de les recréer :

```powershell
databricks bundle deployment bind qualibot_doc_compare prod_proj.qualibot.doc_compare -t prod --profile PROD
databricks bundle deployment unbind qualibot_test -t prod --profile PROD
databricks bundle deployment bind qualibot_images prod_proj.qualibot.images -t prod --profile PROD
databricks bundle deployment bind qualibot_docling_models prod_proj.qualibot.docling_models -t prod --profile PROD
```

## 3. Tables (seulement si l'ancien schéma `prod_landingzone.qualibot` en contient, étape 0)

Cloner chaque table listée à l'étape 0 (éditeur SQL du workspace prod, un utilisateur qui lit l'ancien schéma et crée dans `prod_proj.qualibot`) :

```sql
CREATE TABLE IF NOT EXISTS prod_proj.qualibot.<table> DEEP CLONE prod_landingzone.qualibot.<table>;
```

Contrôle : le nombre de lignes doit être identique entre source et copie (`SELECT count(*) FROM ...`).

## 4. Index Vector Search `prod_proj.qualibot.chunks_index` (seulement si l'index existe dans l'ancien schéma, étape 0)

Reprendre la définition de l'index source puis le recréer sur `prod_proj.qualibot.chunks` (même méthode que `runbooks/uat.md` étape 0.3) :

```powershell
databricks vector-search-indexes get-index prod_landingzone.qualibot.chunks_index --profile PROD -o json > chunks_index_def.json
```

Éditer `chunks_index_def.json` (nom de l'index et table source vers `prod_proj`), puis :

```powershell
databricks vector-search-indexes create-index --json @chunks_index_def.json --profile PROD
databricks vector-search-indexes sync-index prod_proj.qualibot.chunks_index --profile PROD
```

Si l'index source n'existe pas, cette étape est sans objet : l'index est créé par le job parsing au premier déploiement (`parsing_vector_search_indexes` pointe déjà sur `prod_proj`).

## 5. Droits (SQL, éditeur prod)

Remplacer `<APP_SP>` par le client ID de l'app prod (`databricks apps get qualibot --profile PROD -o json`, champ `service_principal_client_id`) :

```sql
GRANT USE CATALOG ON CATALOG prod_proj TO `<APP_SP>`;
GRANT USE SCHEMA ON SCHEMA prod_proj.qualibot TO `<APP_SP>`;
GRANT SELECT ON TABLE prod_proj.qualibot.chunks_index TO `<APP_SP>`;
```

Les droits sur les volumes (`READ VOLUME`, `WRITE VOLUME`) sont à donner sur `prod_proj.qualibot.doc_compare` et `prod_proj.qualibot.images` si le bundle ne les pose pas. Aucun job de droits n'existe pour prod : ces lignes sont à lancer à la main.

## 6. Déploiement (sur accord explicite uniquement)

```powershell
databricks bundle deploy -t prod --profile PROD
```

Le job `D_1_Qualibot_Lakebase_Export_To_Volume` (clé `lakebase_export`) est créé sur PROD avec son planning en `UNPAUSED` (tel que `databricks.yml` le fixe) : il exporte Lakebase vers `prod_proj.qualibot.staging` puis charge les tables en Delta dans `prod_proj.qualibot`. Un premier lancement à la main (`databricks bundle run lakebase_export -t prod --profile PROD`) et un accord écrit avant de reprendre le planning.

Après déploiement : jobs MLflow de la cible, puis vérification du chat et de la comparaison sur l'URL de l'app prod, et des logs de l'app (lignes `Capabilities …` ; le SP prod doit avoir SELECT sur `system.access.audit` pour la tâche `sync_user_capabilities` du job Lakebase).

```powershell
databricks bundle run -t prod create_chat_experiment_prod --profile PROD
databricks bundle run -t prod register_chat_prompts_prod --profile PROD
```

Le scoring `score_chat_traces_prod` reste en pause ; son premier lancement se fait à la main, en `dry_run`.

## 7. Anciens objets

Les objets de l'ancien schéma `prod_landingzone.qualibot` ne sont supprimés par aucune étape de ce runbook. Leur suppression, si elle est décidée, se fait à la main, après validation complète de la prod.

## 8. Une fois tout validé : restreindre l'environnement Bitbucket Production à `main`

À faire dans Bitbucket, après la validation complète de DEV et UAT : Repository settings → Deployments → environnement `Production` → autoriser le déploiement depuis la branche `main` seulement. Le pipeline refuse déjà un déploiement PROD hors de `main` ; ce réglage ajoute le même verrou côté Bitbucket.

## 9. Export Lakebase de PROD (dé-pausé dans `databricks.yml`)

Le job `lakebase_export` de PROD tourne maintenant sur son horaire (02:00 et 14:00, Europe/Bruxelles) et charge les tables Lakebase de PROD dans `prod_proj.qualibot` : les alertes du chat lisent ces copies. Rien ne tourne avant le déploiement de PROD (approbation requise). Avant de déployer : le principal de service de PROD doit pouvoir lire le Lakebase de PROD, écrire dans le volume de transit et dans `prod_proj.qualibot` (grants par le pipeline Bitbucket de PROD, pas depuis le SQL du compte personnel).

## 10. Rôle Lakebase de l'application PROD

`prevent_destroy` est posé sur le rôle `app-service-principal` de PROD. Si l'application PROD est recréée avec un nouveau principal de service, le déploiement refuse de remplacer le rôle : mettre alors le nouvel identifiant en dur dans `postgres_role` comme pour UAT.

## Checklist de mise en production (à cocher dans l'ordre, rien n'est déployé tant que tout n'est pas validé)

Établie le 2026-10-22. Les commandes de chaque ligne sont dans les sections 0 à 10 ci-dessus ; les actions ci-dessous sont celles qu'on ne doit pas oublier.

Avant tout
- [ ] DEV puis UAT validés avec la checklist de test complète (chat, comparaison, aperçus, base de données, parsing).
- [ ] Le profil CLI `PROD` de Jules répond aujourd'hui avec l'application DEV (même identifiant d'app, chemin `/Workspace/.bundle/qualibot/dev/files`) : corriger son hôte, puis `databricks apps get qualibot --profile PROD` doit montrer l'application PROD.
- [ ] Pipeline Bitbucket `tests` vert sur `main`.

Configuration à compléter une fois l'application PROD créée
- [ ] Adresse de l'application PROD dans `utils/deploy/target_env.json` (`CHAT_ALLOWED_ORIGINS`, adresse exacte `https://qualibot-<id>.aws.databricksapps.com`).
- [ ] Identifiant du principal de service de l'application PROD en dur dans `postgres_role` du rôle Lakebase `app-service-principal` de PROD (comme UAT), `prevent_destroy` reste posé.
- [ ] `CHAT_MLFLOW_EXPERIMENT_ID` de PROD dans `target_env.json` après la création de l'expérience (section 6).
- [ ] Droits du principal de service de PROD : volumes `doc_compare`, `images`, `staging`, schéma `prod_proj.qualibot`, index, lecture du Lakebase de PROD, SELECT sur `system.access.audit` (par le pipeline Bitbucket de PROD, jamais en SQL personnel).
- [ ] Droits de l'application PROD : groupes seulement, aucun nom individuel.

Déploiement (accord explicite de Jules)
- [ ] Environnement Bitbucket `Production` limité à la branche `main` (section 8) ; jeton `DATABRICKS_TOKEN` de l'environnement en place.
- [ ] Pipeline `deploy-prod` (tests d'abord), puis les jobs MLflow de la section 6.
- [ ] Premier démarrage : `/api/health` renvoie `"lakebase": "ready"` ; le schéma Lakebase est créé.

Après le déploiement
- [ ] Export Lakebase de PROD : un premier lancement à la main, puis les copies Delta de `prod_proj.qualibot` ; alertes du chat : les dé-pauser (`alerts_pause_status`) quand les copies existent.
- [ ] Parsing de PROD : planning toujours `PAUSED` ; quelques runs manuels en incrémental validés, puis `parsing_schedule_pause: "UNPAUSED"` pour `prod` dans `databricks.yml` (même cadence qu'UAT, 4 h) et un redéploiement des jobs.
- [ ] Aucune donnée de chat ne vient d'UAT : le pipeline `sync-prod-data` ne copie que les modèles docling.
- [ ] Parcours de test complet sur l'application PROD (chat, comparaison, aperçus, partage, historique).
- [ ] OIDC à la place du jeton longue durée (plus tard, avec un administrateur de compte Databricks).

## Fait

_(rien de confirmé pour l'instant)_
