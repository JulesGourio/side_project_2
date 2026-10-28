# SF-1027 — étude approfondie (DEV), avant toute modification de l'app

Notebook : [`etude_dev.py`](./etude_dev.py). Import dans un notebook DEV, « Run all », ~5 min. Lecture seule, **tes droits suffisent** (pas le SP). Rien n'est modifié, ni l'app ni les index. Colle-moi toute la sortie.

**Déjà établi par ta sortie de `investigate_dev.py`** :
1. Les deux index sont sains et donnent **exactement les mêmes rangs en FULL_TEXT**. Explication (doc Databricks) : le côté mots-clés couvre *toutes* les colonnes texte de l'index, donc `chunk_preprocessed` (avec `[SF1027]`) est cherché aussi par `chunks_text_index`. Le choix de la colonne n'agit que sur les embeddings.
2. Les embeddings (ANN) ne trouvent **jamais** le chunk, quelle que soit la requête : pour un code, tout repose sur les mots-clés.
3. La question française échoue **même en FULL_TEXT** (> 50), alors que `SF1027` seul est rang 1. Hypothèse : les mots de la question comptent comme des mots-clés et des chunks qui les contiennent passent devant.
4. Point à trancher : l'app DEV a `CHAT_TRANSLATE_ONLY=on`. Pour un premier message, elle cherche la question **et sa traduction anglaise** (16 passages chacune). Or `Which documents cite SF-1027?` sort en rang 1 avec le reranker. Donc soit la vraie traduction était différente, soit le chunk a été retrouvé mais pas utilisé par la réponse. La cellule A le dit à partir du journal `chat_turns`.

**Ce que mesure chaque cellule** :

| Cellule | Question | Lecture |
| --- | --- | --- |
| A | Qu'a fait l'app pour tes questions SF-1027 ? | requêtes envoyées, et si le chunk a été retrouvé, envoyé au modèle, cité. Retrouvé mais pas cité : c'est la réponse, pas la recherche |
| B | Où est le code dans le chunk ? | le reranker ne lit que les 2000 premiers caractères |
| C | Comment les mots-clés sont découpés | `the` ≈ 0 et `les` élevé : analyseur anglais, les mots français comptent ; `SF-1027` ≈ `SF` + `1027` : le tiret coupe le code |
| D | Rang exact du chunk (jusqu'à 1000) | et l'écart de score avec le 1er et le 16e |
| E | Quel mot de la question enfonce le code | rang de `SF1027` + chaque mot, puis question moins chaque mot |
| F | Qui prend la place du chunk | les 10 premiers résultats et les mots de la question qu'ils contiennent |
| G | Le reranker voit-il plus que `num_results` ? | rangs à k = 12, 16, 50 |
| H | **Options chiffrées** sur 25 codes rares × 3 formes de question | voir ci-dessous |
| I | Effet de bord sur des questions sans code | 1.00 = mêmes passages qu'aujourd'hui |

**Options testées en H** (simulées dans le notebook, l'app n'est pas touchée) :

| | Option | Change le comportement pour |
| --- | --- | --- |
| A | l'app DEV aujourd'hui (référence) | — |
| B | retirer les mots grammaticaux FR/EN des requêtes | **toutes** les questions (mesuré en I) |
| C | + 1 requête « codes seuls » avec reranker, fusionnée par rang (pas forcée en tête) | questions avec code seulement |
| D | idem C en FULL_TEXT (sans embeddings) | questions avec code seulement |
| E | code canonique répété 3 fois dans la requête | questions avec code seulement |
| F | embedding de la question entière + mots-clés = codes seuls | questions avec code seulement (si l'API l'accepte) |

**Résultats du 28/10 (1er passage)** :
- Les deux tours SF-1027 du journal datent du 26/10, avant la reconstruction des index du 28/10. Aujourd'hui, ces mêmes requêtes sortent en **rang 1** (question FR et traduction EN). La copie Delta du journal s'arrête au 26/10 : ton essai du soir n'y est pas.
- Côté mots-clés, `SF1027` ne pèse que 5 points, autant que deux mots courants. `SF-1027 [SF1027]` pèse plus, parce que `SF` et `1027` comptent en plus. Une question qui écrit le code collé (`SF1027`) n'a pas ce renfort : c'était ton essai du soir.
- Sur 25 codes × 3 formes de question : A (app actuelle) 70/75, B 73/75 mais change 11 à 54 % des passages des questions sans code (rejetée), C 70/75, **D, E, F 75/75**. E (code répété) ne coûte aucun appel.
- Cellule J ajoutée : même mesure avec le code écrit collé, en minuscules ou avec un espace.

**Cellule J (28/10)** : avec le code écrit collé dans la question (`SF1027`), l'app actuelle (A) trouve 48/75 (SF-1027 raté 3/3) ; minuscules 73/75 ; espace 61/75. D, E et F : 75/75 pour chaque graphie. Cause : l'app n'ajoute `[CODE]` qu'à un code écrit autrement que sa forme collée, donc un code collé garde un seul mot-clé. Recommandation : E (forme collée répétée dans la requête), à mesurer d'abord sur les vraies questions (1, 2 ou 3 répétitions).

**Cellule K** : autres familles de méthodes sur les 300 questions. Recherche à un seul moteur, mots-clés seuls (N1) ou sens seul (N2). Recherche par correspondance exacte du code : filtre sur `cited_codes` (G1), filtre `LIKE` (G2), passages lus dans la table (G3), documents citant le code (G4). Dans les quatre cas, le code choisit les passages et la question les ordonne.

Le tableau final donne pour chaque option : cas trouvés, cas en top 5, position médiane, passages envoyés au modèle (coût en tokens), appels Vector Search. On choisira ensemble à partir de ces chiffres.

---

# SF-1027 introuvable dans le chat (UAT) — diagnostic en lecture seule

Rien n'est modifié : ni `chunks`, ni `chunks_index`, ni l'app. Tes droits suffisent (pas le runner SP).
Code existant à ne pas casser : recherche exacte des codes via `chunk_codes` ([`codes.py`](../../server/services/chat/engine/codes.py), commit 99864ff).

**Ce que fait le chat aujourd'hui** ([`search.py`](../../server/services/chat/engine/search.py) l.87) : `query_type: HYBRID` (+ reranker `databricks_reranker`), colonne vectorisée `chunk_text`. Le côté mots-clés est celui de Databricks, non paramétrable : on ne peut pas changer son analyseur, on peut seulement tester ce qui remonte.

## 1. SQL UAT (éditeur SQL, `uat_proj`)

**1a. Les chunks qui citent le code, toutes graphies** (tiret normal, insécable, demi-cadratin, espace, rien) :
```sql
SELECT chunk_id, IDDOC, REF, division,
       regexp_extract(chunk_text, '(?i)(.{0,60}SF[\\s\\-\\u2010\\u2011\\u2012\\u2013\\u2014_]?1027.{0,60})', 1) AS extrait
FROM uat_proj.qualibot.chunks
WHERE chunk_text RLIKE '(?i)SF[\\s\\-\\u2010\\u2011\\u2012\\u2013\\u2014_]?1027'
ORDER BY REF, chunk_id;
```
Attendu : au moins `Q0102QP_GB` (IDDOC 25074). Si l'extrait montre une autre graphie que `SF-1027`, c'est la piste (parsing/OCR), pas BM25.

**1b. Compte par graphie exacte** :
```sql
SELECT regexp_extract(chunk_text, '(?i)(SF[\\s\\-\\u2010\\u2011\\u2012\\u2013\\u2014_]?1027)', 1) AS graphie,
       COUNT(*) AS nb_chunks, COUNT(DISTINCT IDDOC) AS nb_docs
FROM uat_proj.qualibot.chunks
WHERE chunk_text RLIKE '(?i)SF[\\s\\-\\u2010\\u2011\\u2012\\u2013\\u2014_]?1027'
GROUP BY 1 ORDER BY 2 DESC;
```

**1c. Ces chunks sont-ils dans l'index ?** (l'index Delta Sync a le même `chunk_id` ; on compare à `chunks`) :
```sql
SELECT COUNT(*) AS chunks_table FROM uat_proj.qualibot.chunks;   -- doit = indexed_row_count de l'index (137187 le 21/10, plus depuis)
```
Puis dans le notebook ci-dessous (cellule 3) : chaque `chunk_id` cible est relu **dans l'index** avec un filtre `chunk_id`. S'il remonte, il est indexé.

**1d. Les autres codes `1027` qui le noient** :
```sql
SELECT REF, COUNT(*) AS nb FROM uat_proj.qualibot.chunks
WHERE chunk_text RLIKE '1027' AND NOT chunk_text RLIKE '(?i)SF[\\s\\-\\u2010\\u2011\\u2012\\u2013\\u2014_]?1027'
GROUP BY REF ORDER BY nb DESC LIMIT 30;
```

## 2. Notebook de test — [`test_vector_search_codes.py`](./test_vector_search_codes.py)

Import dans un notebook UAT, « Run all ». Paramètres en dur en haut. Lecture seule, tes droits (SELECT sur `uat_proj.qualibot.chunks` + droit de requête sur `chunks_index`).

Il teste, pour chaque variante (`SF-1027`, `SF 1027`, `SF1027`, `sf-1027`, `c'est quoi SF-1027 ?`, `Dans quel document se trouve SF-1027 ?`) × chaque méthode : **ANN**, **HYBRID**, **HYBRID + reranker** (= le chat), **FULL_TEXT** (BM25 seul, ignoré s'il est refusé) — le **rang** des chunks cibles dans les 50 premiers, et ce qui remonte à leur place. Fin : tableau récapitulatif + verdict.

Lecture du verdict :
- FULL_TEXT retrouve la cible pour `SF1027` mais pas `SF-1027` → tokenisation du tiret confirmée.
- ANN seul la trouve, HYBRID non → le mot-clé perturbe.
- Aucune méthode ne la trouve mais le filtre `chunk_id` oui → c'est un problème de classement, pas d'indexation.
- Le filtre `chunk_id` ne la trouve pas → chunk absent de l'index (index en retard, sync à voir).

Colle-moi le tableau final. Pistes déjà identifiées si BM25 est confirmé : requête réécrite avec la graphie sans tiret (`SF1027`) en plus de l'originale ; `chunk_codes` couvre déjà le cas exact une fois la tâche 6 relancée (runbook UAT, K1 à K4).

## 3. Index : classique = `chunk_preprocessed`, `chunks_text_index` à côté (texte brut)

État constaté dans le code : l'app normalise bien les requêtes (`SF-1027` devient `SF-1027 [SF1027]`, `search.py` l.87), mais le pipeline avait été remis sur `chunk_text` : l'index UAT n'embarque donc pas la forme canonique, la requête normalisée n'a rien à rattraper. Un index ne peut pas changer de colonne : il faut le supprimer puis le recréer.

Corrigé dans le code (`update_proj_vector_indexes.py`, `databricks.yml` DEV et UAT) : `chunks_index` et `chunks_full_index` embarquent `chunk_preprocessed`, `chunks_text_index` (même table) embarque `chunk_text`. Le test `create_test_index.py` est supprimé.

**Sans relancer tout le pipeline** (une seule tâche, une seule attente de cluster) :
1. Déployer les jobs UAT (Bitbucket `deploy-uat-jobs`) pour que la tâche connaisse la nouvelle liste d'index.
2. Notebook [`rebuild_indexes.py`](./rebuild_indexes.py) : cellules 1-2 vérifient que `chunks` porte `chunk_preprocessed` partout (sinon rechunk d'abord) ; **la cellule 3 supprime `chunks_index`** → le chat n'a plus de recherche jusqu'à l'étape 3 (≈ 1 h). À faire hors heures d'usage (UAT utilisé en semaine).
3. Tâche seule, qui recrée les deux index en SP puis les synchronise :
```powershell
'{"job_id":762813135325407,"only":["5_sync_index"]}' | Out-File -Encoding ascii "$env:TEMP\run5.json"
databricks jobs run-now --json "@$env:TEMP\run5.json" --profile UAT
```
4. Tâche `6_update_kb_metadata` seule (même commande avec `"only":["6_update_kb_metadata"]`) pour `doc_references` (questions « quels documents citent X »).
5. Relancer [`test_vector_search_codes.py`](./test_vector_search_codes.py) : il compare `chunks_index` et `chunks_text_index`.

Pour éviter la coupure : ne fais pas la cellule 3 ; dis-le moi et je mets le classique sous un nouveau nom avec bascule de `CHAT_INDEX` (redémarrage de l'app).
