# Opérations manuelles — PROD (`prod_proj.qualibot`)

Cible : `qualibot-prod` (profil CLI `qualibot-prod`). Rien ici ne déploie : le déploiement prod n'est jamais lancé sans accord explicite. Les commandes sont à taper à la main, dans l'ordre.

Le catalogue `prod_landingzone.intraqual` (source Intraqual) ne se touche pas.

## 0. État de départ (lecture seule, profil `qualibot-prod`)

```powershell
databricks catalogs list --profile qualibot-prod -o json | ConvertFrom-Json | ForEach-Object { $_.name } | Select-String "prod_"
databricks schemas list prod_landingzone --profile qualibot-prod -o json | ConvertFrom-Json | ForEach-Object { $_.full_name }
databricks tables list prod_landingzone qualibot --profile qualibot-prod -o json | ConvertFrom-Json | ForEach-Object { $_.full_name }
databricks volumes list prod_landingzone qualibot --profile qualibot-prod -o json | ConvertFrom-Json | ForEach-Object { $_.full_name }
databricks vector-search-indexes list-indexes qualibot --profile qualibot-prod -o json | ConvertFrom-Json | ForEach-Object { $_.name }
```

Attendu : `prod_proj` existe ou non. Si l'ancien schéma `prod_landingzone.qualibot` contient des tables, des volumes ou un index, les étapes 2, 3 et 4 s'appliquent (copie) ; sinon elles sont sans objet.

## 0.1 Politique de cluster « S » de PROD (lecture seule, à faire une fois)

L'identifiant `001C199A8D85FE35` mis dans `databricks.yml` (cible `qualibot-prod`, variable `job_cluster_policy_id_S`) n'existe pas sur ce workspace : `Can't find a cluster policy with id`. Lister les politiques du workspace prod pour trouver la bonne, puis me donner son identifiant :

```powershell
databricks auth profiles
databricks cluster-policies list --profile qualibot-prod -o json | ConvertFrom-Json | ForEach-Object { "$($_.policy_id)  $($_.name)" }
```

`databricks auth profiles` doit afficher, pour `qualibot-prod`, l'hôte `https://dbc-77b87d5a-6bcb.cloud.databricks.com`. Si ce n'est pas le cas, le profil pointe sur un autre workspace : le corriger avant de lister.

Quand l'identifiant est connu, vérifier sa définition (même forme que la politique S de DEV : nœud unique, `first_on_demand`, `auto:latest-lts`, `activity-type` fixé à `prod-job`) :

```powershell
databricks cluster-policies get <policy_id> --profile qualibot-prod
```

Remplacer `<policy_id>` par l'identifiant trouvé. Aucune commande de déploiement ici.

## 1. Catalogue et schéma `prod_proj` (admin metastore, seulement si absent)

Si `prod_proj` n'existe pas, un admin le crée (SQL, éditeur du workspace prod) :

```sql
CREATE CATALOG IF NOT EXISTS prod_proj;
CREATE SCHEMA IF NOT EXISTS prod_proj.qualibot;
```

Le déploiement crée aussi le schéma `prod_proj.qualibot` ; ces deux lignes servent seulement si le catalogue manque.

## 2. Volumes et données (PowerShell, profil `qualibot-prod`)

Créer les volumes dans `prod_proj` :

```powershell
databricks volumes create prod_proj qualibot doc_compare MANAGED --profile qualibot-prod
databricks volumes create prod_proj qualibot test MANAGED --profile qualibot-prod
databricks volumes create prod_proj qualibot images MANAGED --profile qualibot-prod
databricks volumes create prod_proj qualibot docling_models MANAGED --profile qualibot-prod
```

Copier les données depuis l'ancien emplacement (ignorer une ligne si la source n'existe pas, voir étape 0) :

```powershell
databricks fs cp -r dbfs:/Volumes/prod_landingzone/qualibot/docling_models dbfs:/Volumes/prod_proj/qualibot/docling_models --profile qualibot-prod
databricks fs cp -r dbfs:/Volumes/prod_landingzone/qualibot/doc_compare dbfs:/Volumes/prod_proj/qualibot/doc_compare --profile qualibot-prod
databricks fs cp -r dbfs:/Volumes/prod_landingzone/qualibot/test dbfs:/Volumes/prod_proj/qualibot/test --profile qualibot-prod
databricks fs cp -r dbfs:/Volumes/prod_landingzone/qualibot/images dbfs:/Volumes/prod_proj/qualibot/images --profile qualibot-prod
```

Le volume `staging` n'est pas copié. Le bundle déclare `doc_compare`, `test`, `images`, `docling_models` pour prod : avant tout déploiement, lier les volumes créés pour éviter qu'il tente de les recréer :

```powershell
databricks bundle deployment bind qualibot_doc_compare prod_proj.qualibot.doc_compare -t qualibot-prod --profile qualibot-prod
databricks bundle deployment bind qualibot_test prod_proj.qualibot.test -t qualibot-prod --profile qualibot-prod
databricks bundle deployment bind qualibot_images prod_proj.qualibot.images -t qualibot-prod --profile qualibot-prod
databricks bundle deployment bind qualibot_docling_models prod_proj.qualibot.docling_models -t qualibot-prod --profile qualibot-prod
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
databricks vector-search-indexes get-index prod_landingzone.qualibot.chunks_index --profile qualibot-prod -o json > chunks_index_def.json
```

Éditer `chunks_index_def.json` (nom de l'index et table source vers `prod_proj`), puis :

```powershell
databricks vector-search-indexes create-index --json @chunks_index_def.json --profile qualibot-prod
databricks vector-search-indexes sync-index prod_proj.qualibot.chunks_index --profile qualibot-prod
```

Si l'index source n'existe pas, cette étape est sans objet : l'index est créé par le job parsing au premier déploiement (`parsing_vector_search_indexes` pointe déjà sur `prod_proj`).

## 5. Droits (SQL, éditeur prod)

Remplacer `<APP_SP>` par le client ID de l'app prod (`databricks apps get qualibot --profile qualibot-prod -o json`, champ `service_principal_client_id`) :

```sql
GRANT USE CATALOG ON CATALOG prod_proj TO `<APP_SP>`;
GRANT USE SCHEMA ON SCHEMA prod_proj.qualibot TO `<APP_SP>`;
GRANT SELECT ON TABLE prod_proj.qualibot.chunks_index TO `<APP_SP>`;
```

Les droits sur les volumes (`READ VOLUME`, `WRITE VOLUME`) sont à donner sur `prod_proj.qualibot.doc_compare` et `prod_proj.qualibot.images` si le bundle ne les pose pas. Aucun job de droits n'existe pour prod : ces lignes sont à lancer à la main.

## 6. Déploiement (sur accord explicite uniquement)

```powershell
databricks bundle deploy -t qualibot-prod --profile qualibot-prod
```

Après déploiement : jobs MLflow de la cible, puis vérification du chat et de la comparaison sur l'URL de l'app prod, et des logs de l'app (absence de `live group check failed`).

```powershell
databricks bundle run -t qualibot-prod create_chat_experiment_prod --profile qualibot-prod
databricks bundle run -t qualibot-prod register_chat_prompts_prod --profile qualibot-prod
```

Le scoring `score_chat_traces_prod` reste en pause ; son premier lancement se fait à la main, en `dry_run`.

## 7. Anciens objets

Les objets de l'ancien schéma `prod_landingzone.qualibot` ne sont supprimés par aucune étape de ce runbook. Leur suppression, si elle est décidée, se fait à la main, après validation complète de la prod.

## Fait

_(rien de confirmé pour l'instant)_
