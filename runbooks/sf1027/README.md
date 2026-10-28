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
