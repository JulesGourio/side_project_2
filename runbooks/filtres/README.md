# Filtres du parsing : inventaire

Resultat d'abord : le parsing applique **4 familles de filtres**, toutes en constantes de [config.py](../../utils/parsing_pipeline/config.py). Cote requete (app), il n'y a que la division AS/IS/ALL.

## 1. Filtres appliques au parsing

| Filtre | Constante | Valeur actuelle | Effet |
|---|---|---|---|
| **Statut du document** | `DOC_SCOPE_FILTER` | `revision_status_code=1` (courant), `lifecycle_state_code=7`, `is_hidden=false` | Hors de ces criteres : jamais parse |
| **Categories (perimetre)** | `SCOPE_ROOT_IDCATS` | 3870 (One QMS), 948 (AS Programs), 3209 (AS Tech. Instr.), 12 (AS Processes), 962 (Directive Groupe) | Seules ces racines sont parsees |
| | `SCOPE_SUB_IDCATS` | 3967, 4006 (To be classified) | Gardees ou qu'elles soient dans l'arbre |
| **Categories (exclusion)** | `EXCLUDED_IDCATS` | vide | Exclut une categorie principale (`SKIPPED_CATEGORY_EXCLUDED`) |
| **Date** | `DOC_DATE_CUTOFF` | 2016-01-01 | Avant : va dans `chunks_archive` (impact seulement), pas dans le chat. Date inconnue = recent |
| | `DATE_EXEMPT_ROOT_IDCATS` | vide | Racines exemptees de la coupure |
| | `ARCHIVE_MAX_DOCS` | 0 | Nb de docs d'avant la coupure reellement parses (0 = aucun, -1 = tous) |
| **Exclusions manuelles** | `MANUAL_REF_EXCLUSIONS` | vide | REF exclue (`SKIPPED_REF_MANUAL`) |
| | table `rag_exclusions` | (IDDOC, raison) | Exclusion manuelle en table |
| **Fichier** | `EMPTY_SMALL_FILE_SIZE_BYTES` | 50 Kio | Sans texte et plus petit : `SKIPPED_EMPTY_SMALL_FILE` |
| | `MAX_CHUNKS_SPREADSHEET` | 100 | Plafond de chunks par tableur |
| | `PARSE_TIMEOUT_SECONDS` | 200 | Au dela : `TIMEOUT` |
| **Images** | | | Decoratives ignorees (`SKIPPED_DECORATIVE`), `SKIPPED_EMPTY_IMAGES` |
| **Division** (metadonnee, pas un filtre) | [categories.py](../../utils/parsing_pipeline/categories.py) | AS, IS, DIRECTIVE, GROUP | Sert au filtre de l'app ; categories en `niveau_plus_1..6` |

Pas de filtre par type de document ni par langue.

Pour **ajouter/retirer une categorie** : modifier `SCOPE_ROOT_IDCATS` / `SCOPE_SUB_IDCATS` / `EXCLUDED_IDCATS`. Pour la **date** : `DOC_DATE_CUTOFF` et `ARCHIVE_MAX_DOCS`. Un retrait de perimetre supprime les chunks du document au run suivant (prune de `create_proj_parse_manifest`), a ne faire qu'avec ton go. Rien n'est modifie ici.

## 2. Etat reel en UAT : a lancer

[inventaire_filtres_uat.py](inventaire_filtres_uat.py), lecture seule, tes droits suffisent. Colle les sorties des cellules 1 a 5 : statuts du manifest, statuts de parsing, docs avant 2016 par annee, categories des docs indexes.

## 3. Requetes : docs dans le scope mais filtres

[requetes_hors_scope.py](requetes_hors_scope.py), lecture seule, tes droits suffisent (SELECT sur `prod_gold.quality_system` et `uat_proj.qualibot`). 9 cellules : vivants hors categories (par racine, par categorie, exemples), avant 2016 (par annee, par racine, exemples), date inconnue, filtre de statut, docs du scope absents de `parsed_documents`, exclusions manuelles.

## 4. Questions

1. Quelles categories ajouter / retirer (IDCAT ou nom) ?
2. Date : garder 2016-01-01, autre valeur, ou parser aussi une partie des archives (`ARCHIVE_MAX_DOCS`) ?
3. Autres filtres voulus (type de document, langue, taille) ?
