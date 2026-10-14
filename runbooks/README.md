# Runbooks

Les commandes qu'un humain tape à la main, par environnement. Tout ce qui crée une ressource à partir du dépôt (bundle `databricks.yml`, `resources/*.yml`, notebooks, scripts `utils/deploy/`) reste dans le code ; ici, uniquement ce qui se tape.

| Fichier | Contenu |
|---|---|
| `dev.md` | Workspace DEV : copie de l'UAT, tables/volumes/index dans `dev_proj.qualibot`, opérations ponctuelles DEV |
| `uat.md` | Workspace UAT et cible `qualibot-uat-test` : migration vers `uat_proj.qualibot`, validation, passage en service |
| `prod.md` | Workspace PROD : `prod_proj.qualibot`, droits, déploiement sur accord |

## Règles

- Chaque étape donne toutes ses commandes, dans l'ordre (PowerShell, `databricks bundle run`, SQL). Une étape marquée « sur accord » ne se lance pas sans accord explicite.
- Les profils CLI : `DEV` (DEV), `UAT` (UAT et `qualibot-uat-test`), `qualibot-prod` (PROD).
- Une étape est datée une fois confirmée faite.
- Aucun objet qualibot ne vit dans `*_landingzone` : tables, volumes et index sont dans `<env>_proj.qualibot`. Les `*_v1` sont la source legacy de la copie unique et disparaissent à la fin de l'étape de nettoyage.

## Où trouver une opération ponctuelle

- Copie du corpus UAT vers DEV : `dev.md` section 00.3 et 00.4.
- Export Lakebase vers volume, chargement des tables chat : `uat.md` étape 0.5 (UAT) et `dev.md` 00.6 (DEV).
- Validation de l'UAT sur `qualibot-uat-test` : `uat.md` section A.
- Parsing, index et métadonnées : `uat.md` sections B et D.
- Suppression de la sync des droits et du job associé : `uat.md` étape 0.8 ; aucun job de ce type en DEV ni en PROD.
