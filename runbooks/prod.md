# Runbook PROD — tables, volumes et index dans `prod_proj.qualibot`

Cible : `qualibot-prod` (profil CLI `qualibot-prod`). Rien ici ne déploie : le déploiement prod n'est jamais lancé sans accord explicite (`CLAUDE.md`). Les commandes sont à taper à la main, dans l'ordre.

## 0. État de départ (lecture seule, profil `qualibot-prod`)

```powershell
databricks catalogs list --profile qualibot-prod -o json | ConvertFrom-Json | ForEach-Object { $_.name } | Select-String "prod_"
databricks schemas list prod_landingzone --profile qualibot-prod -o json | ConvertFrom-Json | ForEach-Object { $_.full_name }
databricks tables list prod_landingzone qualibot --profile qualibot-prod -o json | ConvertFrom-Json | ForEach-Object { $_.full_name }
databricks volumes list prod_landingzone qualibot --profile qualibot-prod -o json | ConvertFrom-Json | ForEach-Object { $_.full_name }
databricks vector-search-indexes list-indexes qualibot --profile qualibot-prod -o json | ConvertFrom-Json | ForEach-Object { $_.name }
```

Attendu : `prod_proj` existe ou non. Le catalogue `prod_landingzone.intraqual` (source Intraqual) ne se touche pas. Si le schéma `prod_landingzone.qualibot` contient des tables ou un index, les étapes 3 et 4 s'appliquent ; sinon elles sont sans objet.

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

Copier les données depuis `prod_landingzone` (ignorer une ligne si la source n'existe pas, voir étape 0) :

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

## 3. Tables (seulement si `prod_landingzone.qualibot` en contient, étape 0)

Cloner chaque table listée à l'étape 0 (éditeur SQL du workspace prod, un utilisateur qui lit `prod_landingzone` et crée dans `prod_proj.qualibot`) :

```sql
CREATE TABLE IF NOT EXISTS prod_proj.qualibot.<table> DEEP CLONE prod_landingzone.qualibot.<table>;
```

Contrôle : le nombre de lignes doit être identique entre source et copie (`SELECT count(*) FROM ...`).

## 4. Index Vector Search `prod_proj.qualibot.chunks_index` (seulement si l'index existe en prod, étape 0)

Même méthode que `runbooks/uat.md` étape 0.3 : reprendre la définition de l'index source puis le recréer sur `prod_proj.qualibot.chunks` :

```powershell
databricks vector-search-indexes get-index prod_landingzone.qualibot.chunks_index --profile qualibot-prod -o json > chunks_index_def.json
```

Éditer `chunks_index_def.json` (nom de l'index et table source vers `prod_proj`), puis :

```powershell
databricks vector-search-indexes create-index --json @chunks_index_def.json --profile qualibot-prod
databricks vector-search-indexes sync-index prod_proj.qualibot.chunks_index --profile qualibot-prod
```

Si `prod_landingzone.qualibot.chunks_index` n'existe pas, cette étape n'est pas nécessaire : l'index est créé par le job parsing au premier déploiement (`parsing_vector_search_indexes` pointe déjà sur `prod_proj`).

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

Après déploiement : vérifier le chat et la comparaison sur l'URL de l'app prod, puis les logs de l'app (absence de `live group check failed`).

## 7. Suppression des objets legacy (après validation, sur accord)

```powershell
databricks volumes delete prod_landingzone.qualibot.doc_compare --profile qualibot-prod
databricks volumes delete prod_landingzone.qualibot.test --profile qualibot-prod
databricks volumes delete prod_landingzone.qualibot.images --profile qualibot-prod
databricks volumes delete prod_landingzone.qualibot.docling_models --profile qualibot-prod
```

Tables et index legacy de `prod_landingzone.qualibot` (à lister à l'étape 0) : `DROP TABLE IF EXISTS prod_landingzone.qualibot.<table>;` et `databricks vector-search-indexes delete-index prod_landingzone.qualibot.chunks_index --profile qualibot-prod`, uniquement après contrôle.
