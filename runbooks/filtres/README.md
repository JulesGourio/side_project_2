# Filtres de l'app : inventaire (UAT)

Resultat d'abord : **un seul filtre utilisateur existe aujourd'hui, la division (ALL / AS / IS). Il n'y a aucun filtre de date ni de categorie cote recherche.** Le reste est du perimetre applique au moment du parsing, pas a la requete.

## 1. Ce qui existe dans le code

| Filtre | Ou | Effet |
|---|---|---|
| **Division** ALL/AS/IS | UI [division.tsx](../../client/src/components/chat/division.tsx) puis [ws.py](../../server/routers/chat/ws.py) puis [`division_filter`](../../server/services/chat/engine/config.py) | Filtre Vector Search `division IN [AS|IS, "GROUP"]`; ALL = aucun filtre. Les documents `GROUP` sont communs. Valeur `DIRECTIVE` existe dans la source mais n'est ni dans l'UI ni dans le filtre (donc **absente** des recherches AS/IS, visible seulement en ALL). |
| Filtres techniques | [search.py](../../server/services/chat/engine/search.py) | `chunk_id IN (...)` et `REF IN (...)` pour retrouver les documents nommes ou cites. Pas des filtres utilisateur. |
| Compare / impact | [vector_search.py](../../server/services/infra/vector_search.py) | Aucun filtre. Index = `COMPARE_IMPACT_INDEX` (meme index que le chat en UAT). |
| Date | [config.py](../../utils/parsing_pipeline/config.py) `DOC_DATE_CUTOFF = 2016-01-01` | Pas un filtre de requete : les docs avant cette date sont ecartes de `chunks` (donc de l'index) a l'ecriture, et vont dans `chunks_archive` (index `chunks_full_index`, impact seulement). Date inconnue = recent. |
| Categories | [config.py](../../utils/parsing_pipeline/config.py) `SCOPE_ROOT_IDCATS`, `SCOPE_SUB_IDCATS`, `EXCLUDED_IDCATS` | Perimetre applique au parsing (racines 3870, 948, 3209, 12, 962 + sous-categories 3967, 4006). `EXCLUDED_IDCATS` est vide. Rien cote requete. |

Index UAT : `uat_proj.qualibot.chunks_index` ([target_env.json](../../utils/deploy/target_env.json)), source `uat_proj.qualibot.chunks`.

## 2. Metadonnees disponibles pour filtrer (colonnes de `chunks`, [build_steps.py](../../utils/parsing_pipeline/build_steps.py))

`IDDOC, REF, division, chunk_id, chunk_index, chunk_text, chunk_token_count, chunk_content_type, semantic_headers, url, doc_date, titre, type_document, indice, langue`.

Constat important : **les categories (`niveau_plus_1..6`) et la date de diffusion ne sont pas toutes des colonnes de l'index** (la categorie n'est que dans le prefixe texte `[Source: ... | Category: ... | Date de diffusion: ...]` du chunk). A confirmer avec la cellule 1 et 5 du notebook. Ajouter un filtre date ou categorie cote requete implique donc : ajouter la colonne a `chunks`, la synchroniser dans l'index (recreation/sync = decision a part, l'index de l'app ne doit pas bouger sans ton go), puis le filtre dans `division_filter` + l'UI.

## 3. Ce qu'il se passe reellement en UAT : a lancer

Notebook en lecture seule : [inventaire_filtres_uat.py](inventaire_filtres_uat.py). Tes droits suffisent (SELECT), pas besoin du job runner, rien n'est ecrit, l'index n'est pas touche. Colle ici les sorties des cellules 1 a 5.

Il donne : colonnes reelles de `chunks`, repartition par division, par annee, par categorie (niveau 1 et 2), type de document, langue, et colonnes exposees par l'index.

## 4. Questions pour la suite (apres tes resultats)

1. Quels filtres veux-tu **ajouter** (date de diffusion ? categorie ? type de document ? langue ?) et lesquels **supprimer** (division ?).
2. Le filtre date doit-il etre une plage libre, ou des raccourcis (2 ans, 5 ans) ?
3. `DIRECTIVE` : doit-il etre inclus dans AS et IS ?
