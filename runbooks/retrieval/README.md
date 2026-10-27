# Plan : améliorer le retrieval (codes type ST-1027)

Un seul fichier. Cases à cocher = avancement. **Validation** = ce que tu me renvoies avant de passer à la suite.
Règles : tu exécutes tout toi-même (notebook, paramètres en dur, dis si le runner SP est requis) ; **l'index utilisé par l'app ne change pas** (on construit un index parallèle, bascule à part et sur go explicite) ; UAT utilisé en semaine → écritures UAT en dehors des heures ou sur tables `_test`.

**Liens utiles**
[search.py](../../server/services/chat/engine/search.py) (requête HYBRID l.87, colonne `chunk_text`) ·
[retrieval.py](../../server/services/chat/engine/retrieval.py) ·
[codes.py](../../server/services/chat/engine/codes.py) (recherche exacte `chunk_codes`, existe déjà) ·
[retrieval_eval.py](../../utils/evaluation/retrieval_eval.py) (éval retrieval existante, à réutiliser) ·
[rebuild_chunks_index.py](../scripts/uat_index/rebuild_chunks_index.py) (création d'index) ·
Diagnostic SF-1027 (autre fil) : [sf1027/README.md](../sf1027/README.md) → **ne pas refaire**, on part de son verdict.

## Découpage en fils

| Fil | Étapes | Dépend de |
|---|---|---|
| F0 | Verdict SF-1027 + figer le jeu d'éval | (fil sf1027) |
| F1 | Éval retrieval MRR / Recall@10 (notebook) | F0 |
| F2 | Preprocess query + docs (regex) | F0 |
| F3 | Nouvel index parallèle (preprocessed / brut) | F2 |
| F4 | Éval A/B + top_k optimal | F1, F3 |
| F5 | MLflow | F1, F4 |
| F6 | Bascule index (go explicite) | F4 |
| Plus tard | Lemmatisation, réécriture LLM + RRF | F6 |

F1 d'abord : sans mesure on ne sait pas si F2/F3 améliorent.

## Étapes de base

### 0. Prérequis
- [ ] Verdict du fil SF-1027 (BM25 coupe-t-il au tiret ? FULL_TEXT `SF1027` OK vs `SF-1027` KO ?)
  *Validation : tableau final du notebook collé ici.*
- [ ] Jeu d'éval figé : tables `eval_golden_questions` / `eval_synthetic_questions` + 20-30 questions « codes » ajoutées à la main (ST-1027, SF-1027, etc.) avec chunks/docs attendus.
  *Validation : nb de questions par source, et par type (code / texte libre).*

### 1. Scoring du retrieval (en premier)
- [ ] Notebook : pour chaque question, rang du 1er chunk pertinent → **MRR** = moyenne de 1/rang ; **Recall@10** = pertinents dans le top 10 / total pertinents.
  Base : [retrieval_eval.py](../../utils/evaluation/retrieval_eval.py) (mesure déjà « doc attendu dans les passages »). À ajouter : MRR et Recall@10 au niveau chunk, par source et par type de question.
- [ ] Baseline mesurée sur l'index actuel (UAT, lecture seule).
  *Validation : MRR et Recall@10 baseline, global + « questions avec code ».*

### 2. Preprocess de la query (BM25)
- [ ] Fonction pure `normalize_codes(text)` : regex `\b([A-Za-z]{1,5})[-‐‑‒–—\s]?(\d{2,6})\b` → `ST_1027`. Une seule source de vérité, importée par l'app et par le pipeline.
- [ ] Tests unitaires (tirets unicode, `ST 1027`, `st-1027`, faux positifs : dates, « page 12 », normes ISO).
- [ ] Qui reçoit quoi : BM25 ← `C'est quoi ST_1027` ; embeddings ← `C'est quoi ST-1027` (inchangé).
  *Validation : liste des faux positifs trouvés sur un échantillon de 200 vraies questions.*

### 3. Preprocess des documents
- [ ] Même regex côté chunks : `ST-1027` → `ST-1027 [ST_1027]` (graphie d'origine conservée pour les embeddings).
- [ ] Nouvelle colonne `chunk_text_pp` dans une table **`chunks_pp`** (copie de `chunks`, pas de modif de `chunks`), calculée dans le pipeline de parsing.
- [ ] Test sur table `_test` UAT, hors heures ouvrées.
  *Validation : nb de chunks modifiés, 10 exemples avant/après, volumétrie.*

### 4. Nouvelle architecture d'index
- [ ] Nouvel index Delta Sync **parallèle** sur `chunks_pp` (ex. `chunks_pp_index`), colonne vectorisée = partie preprocessed, colonne renvoyée = `chunk_text` brut pour la génération.
- [ ] Adapter `_COLUMNS`/`_RERANK_COLUMNS` dans [search.py](../../server/services/chat/engine/search.py) derrière un paramètre d'index (l'app garde l'ancien tant qu'on ne bascule pas).
- [ ] Vérifier droits SP de l'app sur le nouvel index (grants).
  *Validation : index ONLINE, `indexed_row_count` = lignes de `chunks_pp`, requête test OK.*

### 5. Éval A/B + top_k optimal
- [ ] Notebook : baseline vs index preprocessed, mêmes questions.
- [ ] Balayage top_k ∈ {5, 8, 10, 15, 20, 30} (et rerank/raw : voir `rerank_top_k` / `raw_top_k` dans [config.py](../../server/services/chat/engine/config.py)) : MRR, Recall@k, taille du contexte (tokens), latence.
- [ ] Choix du top_k = plus petit k où Recall@k plafonne.
  *Validation : courbe/tableau Recall@k vs k, k retenu.*

### 6. MLflow
- [ ] Même fonction de scoring, un run MLflow par config (index, top_k, regex version) : params + MRR/Recall@10 + artefact des échecs.
- [ ] Décision d'avant-bascule prise sur MLflow.
  *Validation : lien de l'experiment, comparaison de 2 runs.*

### 7. Bascule (sur go explicite uniquement)
- [ ] DEV → test → UAT (hors heures) → PROD en dernier.
- [ ] Index de l'app inchangé jusqu'à ce go ; plan de retour arrière (garder l'ancien index).

## Plus tard (noté, pas détaillé)
- Lemmatisation / stemming des chunks et de la query.
- Réécriture de la query par LLM (décomposition, ajout de contexte) + RRF pour fusionner les résultats.

## Questions à clarifier

**Architecture**
1. **Un seul texte pour BM25 ET embeddings** : en Delta Sync managé, la colonne vectorisée sert aux deux (HYBRID Databricks, analyseur non paramétrable). `ST-1027 [ST_1027]` sera donc aussi embeddé. Acceptable ? Sinon : embeddings auto-gérés (vecteurs précalculés sur le texte brut + texte preprocessed pour BM25) = refonte plus lourde. Ton schéma « embeddings gardent ST-1027 » implique cette 2e option : tu veux vraiment ça ?
2. Autre piste moins lourde : ne **pas** toucher aux chunks, juste réécrire la query (`ST-1027 ST_1027` ajoutés) → à tester d'abord dans F0, peut suffire.
3. [codes.py](../../server/services/chat/engine/codes.py) / `chunk_codes` (recherche exacte) fait déjà une partie du travail : on le garde en complément ou il est remplacé ?
4. Pipeline : où mettre le preprocess des docs (parsing, chunking, ou vue SQL entre `chunks` et l'index) ? Impact sur les runs incrémentaux (re-calcul de tout `chunks_pp` ?).
5. Faut-il versionner la regex (colonne `pp_version`) pour savoir quels chunks recalculer quand on la change ?

**Périmètre des codes**
6. Quels formats exacts : `ST-1027`, `SF-1027`, autres préfixes ? Codes avec lettres après (`ST-1027A`), avec points (`1.2.3`) ? Liste exhaustive des préfixes ?
7. Que faire des acronymes sans chiffres (`DOA`, `MOE`) : même traitement ?

**Évaluation**
8. Qui fournit les questions de vérité terrain (golden) ? Combien au minimum ? « Chunk pertinent » = chunk exact ou n'importe quel chunk du bon document ?
9. MRR/Recall@10 au niveau chunk ou document ? Avec ou sans reranker ?
10. Éval sur DEV ou UAT ? (UAT = bon corpus mais utilisé en semaine ; lecture seule OK ?)

**Exécution / contraintes**
11. Runner SP nécessaire pour créer l'index dans le catalog UAT, ou tes droits suffisent ?
12. Fenêtre de création/sync d'index UAT acceptable (hors heures, week-end) ?
13. MLflow : experiment dans quel workspace/chemin ? Le notebook doit tourner sur quel cluster (ML runtime) ?
14. Rework : OK pour toucher `search.py` et le pipeline de parsing (merge dans sharp-mayer, pas de PR) ?
