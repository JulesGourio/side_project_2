# Plan : améliorer le retrieval (acronymes, codes type ST-1027)

Un seul fichier. `- [ ]` = avancement. **Validation** = ce que tu me renvoies avant la suite.
Règles : tests et lectures en **DEV** ; notebooks à paramètres en dur (ou pipeline propre), je dis à chaque fois job runner ou tes droits (index → job runner) ; l'index de l'app ne change pas avant la bascule (index parallèle, go explicite).

**Liens** : [search.py](../../server/services/chat/engine/search.py) (HYBRID l.87) · [retrieval.py](../../server/services/chat/engine/retrieval.py) · [codes.py](../../server/services/chat/engine/codes.py) (regex de codes + `chunk_codes`, point de départ, à refaire) · [retrieval_eval.py](../../utils/evaluation/retrieval_eval.py) (éval existante) · [rebuild_chunks_index.py](../scripts/uat_index/rebuild_chunks_index.py) · diagnostic SF-1027 : [sf1027/README.md](../sf1027/README.md) (autre fil, ne pas refaire).

## Calendrier

| Quand | Étape | Fil |
|---|---|---|
| **Aujourd'hui (mar.)** | 1. Inventaire des acronymes/codes | F1 |
| Mer.-Jeu. | 2. Preprocess query · 3. Preprocess docs | F2 |
| Jeu.-Ven. | 4. Architecture d'index | F3 |
| Ven. | 5. Scoring MRR / Recall@10 (notebook puis MLflow) | F4 |
| Ven.-Lun. | 6. top_k optimal | F5 |
| Lun.-Mar. | 7. Vérification + PROD | F6 |
| TBD | Further steps | — |

**Ordre critique** (validé : jeu d'éval en parallèle, avec de vraies questions d'utilisateurs ; priorité aujourd'hui = inventaire) : le scoring (5) mesure tout le reste, mais il est planifié vendredi. Je propose de **préparer le jeu d'éval dès F1** (c'est lui qui prouve que 2-3-4 aident) et de le coder en parallèle ; sinon on découvre vendredi que le preprocess n'a rien amélioré. À valider.

## Étapes

### 1. Inventaire des acronymes et codes — aujourd'hui (F1)
Objectif : savoir quoi normaliser avant d'écrire une regex.

**Lancer** : [f1_inventaire_codes.py](f1_inventaire_codes.py) dans un notebook DEV. Le découper aux lignes `# COMMAND ----------` (une cellule par bloc), lancer dans l'ordre. Il lit DEV et UAT (`ENVS = ["dev", "uat"]` ; `["uat"]` ou `["dev"]` pour une seule passe) et écrit `dev_proj.qualibot.f1_inventaire_codes` (colonne `env`, sections `A_reference`, `B_corpus`, `C_questions`). Cellule 4 = contrôle du nombre de lignes par env, section et catégorie. Cellule 5 = graphiques (vue d'ensemble, REF à graphie variable, top codes par motif, codes du chat absents du corpus), en DEV et UAT côte à côte.

**Extraction** (cellule 1, sans seuil de longueur) : un token = un bloc de lettres et chiffres relié au suivant par `-`, `_`, `/` ou `.` (`ST-1027`, `MI-1413_FR`, `INAQ/560`, `4.2.1`) ; une séquence = un token suivi de nombres séparés par des espaces (`IAQ 20 00 04`, `BAC 5423`). Catégories (triées après extraction) : `code_lettres_chiffres`, `sequence_espace`, `sigle` (majuscules, 2 lettres ou plus), `casse_mixte` (`eFNC`, `PIRs`), `nombre_compose` (`4.2.1`). Écartés : nombres seuls, mots en minuscules, mots capitalisés. La colonne `structures` donne la forme (`A-9` pour `ST-1027`) pour trier les graphies.\n\n- [x] Notebook, lecture seule : sur le corpus parsé (`parsing_checkpoint`, tous les fichiers, sans scope), extraire tous les tokens « code » (lettres+chiffres, avec tiret/espace/underscore) et les acronymes MAJUSCULES, avec fréquence, nb de docs, graphies variantes. → [f1_inventaire_codes.py](f1_inventaire_codes.py) (cellules 4 à 6)
- [x] **A. Références des docs Intraqual** : la liste officielle des `REF` (`doc_catalog` / `parse_manifest`) donne les préfixes (ST, SF, QR, …) et leur forme exacte ; mesurer combien de citations dans les chunks sont écrites autrement que la REF.
- [x] **B. Les autres, ratisser très large** : sigles (DOA, AIDS…), normes et standards aéronautiques (ISO, EN, AS, NADCAP, DO-…), références clients (Airbus), références internes à espaces ou points (`IAQ 20 00 04`, `1.2.3`), codes pièce/plan, noms propres en majuscules. Classer : code à chiffres / référence à segments / sigle sans chiffres / mot courant en majuscules (faux positifs). Les motifs ne sont pas supposés : on part des tokens réellement présents (n-grammes MAJUSCULES+chiffres, segments séparés par espace/tiret/point/slash) et on regarde ce qui ressort.
- [x] **C. Vraies questions d'utilisateurs** : même extraction sur les questions réelles du chat UAT (`chat_turns`) : quels codes/sigles les gens tapent, avec quelle graphie. Écart entre la graphie de la question et celle du chunk = le cas à corriger. Ces questions nourrissent aussi le jeu d'éval (étape 5, priorité plus basse qu'ici).
- [x] Sortie : tableau top 200 par catégorie, 30 faux positifs, graphies question vs chunk, décision par catégorie (regex, liste/glossaire, rien).
  *Validation : tu relis le tableau ; on fige les catégories traitées par regex (étape 2-3) et celles renvoyées au glossaire (Further).*

**Résultats F1 (run du 2026-10-27, analyse Genie sur `dev_proj.qualibot.f1_inventaire_codes` ; 6 049 documents DEV, 6 218 UAT)**

| Constat | Chiffres | Décision |
|---|---|---|
| REF du catalogue citées sous la forme exacte | UAT 2 863 / 5 708 ; DEV 2 288 / 4 589 | rien à faire |
| REF citées autrement (espaces, casse, points) ou avec suffixe collé (`MI-1413_FR`) | UAT 381 et 369 ; DEV 344 et 291 | **regex** (étape 2-3) |
| REF jamais citées dans `document_text` | UAT 2 672 ; 2 084 ont pourtant leur document dans le checkpoint | rien : chaque chunk commence par `[Source: <REF> | Title: …]` (`worker.source_prefixed_text`, `EMBED_SOURCE_PREFIX`), la REF y est déjà |
| Variantes réelles trouvées dans le texte | `MI -1058`, `MI - 1365`, `QTP 01-174`, `IF2.03.08_CZ`, `LDB IT 243`, `LAT 215.300.00/55` | **regex** : séparateur précédé ou suivi d'espaces (0 à 3), `.` `/` `-` `_` équivalents, casse ignorée, préfixes pris dans le catalogue |
| Suffixe de langue dans les questions (`Q0697MI_FR`, `NF-10149_Fr`, `MM-1059_BG`) | 6 des 65 codes absents du corpus | **regex** : suffixe de langue facultatif côté query |
| Fautes de frappe (`Q0126P`, `ALUMUNIUM`) | 3 sur 65 | rien |
| Code voisin d'un autre à un chiffre près (`NS-1717`, `MI-13663`, `QTR-01-247`) | ~25 sur 65 | rien : répondre « inconnu », ne pas rediriger |
| `sigle` (DOA, EUR, NA…) | ~50 % de bruit (mots courants, en-têtes) | **glossaire** (Further), classer par nombre de documents et non par occurrences |
| `casse_mixte` | 85 % d'occurrences dans un seul document (`Contrat-cadrePoste`) | rien / glossaire (`eFNC`, `BoM`) |
| `nombre_compose` | dates `AAAAMMJJ` 65 %, `1.54` et `15.4` fusionnés si on retire les points | rien ; ne jamais retirer les séparateurs d'un nombre |
| `code_lettres_chiffres` : bruit `COL1…COL28`, `OA21`, `OC20` | 26 % des occurrences | exclure ces préfixes de la normalisation |
| Familles non captées | `§ 6` (1 556 docs), `Rev A`/`Issue C` (360), `P/N` (75), `B.P.` (790), `LdB P-103` (116), `EC-7210 B` (65) | `LdB` : ajouter au préfixes ; `B.P.` : glossaire ; le reste : Further |
| Clés de plusieurs catégories | `A0` et `A.Arsicaud` ; `IF20308` = `IF 2.03.08` = `IF 20308` | fusionner seulement les préfixes connus du catalogue |

### 1b. Familles de codes : couvrir tout l'inventaire (F1, en cours)
Méthode : chaque clé de l'inventaire reçoit une famille (code voulu, sigle, bruit) ; on relit ce qui reste (`residu`), on ajoute ou corrige une règle, on relance, jusqu'à ce que le résidu ne soit plus que du bruit. Aucune regex de normalisation avant.

**Lancer** : [f2_familles_codes.py](f2_familles_codes.py) dans un notebook DEV (tes droits suffisent : lecture de `dev_proj`/`uat_proj`, écriture de `dev_proj.qualibot.f2_familles_codes` seulement). Cellules 1 à 6 dans l'ordre. Cellule 4 = couverture par famille, cellule 5 = résidu par préfixe puis clé par clé, cellule 6 = familles des clés tapées dans le chat.

Les REF ne sont plus décrites par des formes : la famille vient du **catalogue lui-même** (clé de la REF, REF sans langue, sans premier mot, début de REF, REF + suffixe, même signature qu'une REF). `QIS-46A-352`, `LdB IT-243`, `IAQ 04 04 05`, `PR/LAT/506`, `FD_AQL_2193`, `IF2.03.10_GB` sont couverts par construction (testé en local).

- [x] Notebook des familles, testé en local sur un mini-inventaire.
- [x] Tour 1 (2026-10-28) : résidu 151 k clés / 22 % des documents. Règles ajoutées : mot + petit nombre (`LEVEL 2`, `F 1`), codes à segments (`BAC 5578`, `NE 40-012`, `STM-0002396`), nombre + code (`2024-T3`), puces (`3.P`), URL et fichiers, REF au milieu d'une graphie (`PRO_Q0014NF_EN`, `MI-13991Table`), codes collés (`IS0024879M`, déjà un seul mot). Corrigé : `ref_sans_premier_mot` et la REF sans indice ne donnent plus `VALIDATION`, `EN`, `43`.
  `SF-1027` est cité dans 1 seul document parsé UAT (ce n'est pas une REF du catalogue) : c'est ce document que la question « dans quel document cite-t-on SF-1027 » doit remonter. Cas type à réussir, quel que soit le nombre de documents.
  Règle de sécurité : une famille « bruit » veut dire seulement « pas de clé ajoutée » ; le texte reste indexé comme aujourd'hui, donc un code classé bruit à tort n'est jamais moins bien trouvé qu'aujourd'hui, il n'est juste pas amélioré.
- [x] Tour 2 (2026-10-28) : résidu 40 k clés / 2,9 % des documents (matériaux `TA6V`, outillage `tool_…`, libellés `C3-Fill`, dates, `Partie-145.A.90`). Décision : le résidu est **annoté par défaut** (rien ne peut être oublié, au pire une clé inutile). Ajouts : `code_court` (`B1`, `R30`, `D1` : tapés dans le chat, annotés), `bruit_date`, puce sur la clé (`3P`) ; le bruit « mot + petit nombre » exige un mot de 3 lettres ou plus qui n'est pas un préfixe de REF.
- [x] Tour 3 (2026-10-28) : cellule 6 propre (les clés du chat en bruit sont des morceaux d'URL, des unités `3D`/`8A` déjà collées, des numéros `01-02-003` dont le code entier `AIPI 01-02-003` est annoté). Corrigé : `15-5PH` repassait en puce. Notebook remis au propre : description et tableau des familles en cellule markdown, une cellule markdown avant chaque tableau.
  *Validation : familles figées, on passe à l'étape 2.*
- [ ] Trou connu de l'inventaire, réglé à l'étape 2 : un code écrit avec des espaces autour du séparateur (`MI - 1365`, `SF - 1027`) n'est pas une clé de l'inventaire ; le recollage de l'étape 2 tolère ces espaces et sera testé sur ces cas.
- [ ] Tours suivants jusqu'à résidu = bruit pur.
  *Validation : résidu relu, plus aucun code voulu dedans, aucune clé du chat en bruit.*

### 2. Preprocess de la query (BM25) — après 1b (F2)
- [ ] Normalisation par dictionnaire : les clés des familles `annoter` (catalogue + codes voulus). Dans un texte, les blocs de lettres et chiffres voisins (séparés par `- _ / .` ou des espaces) sont recollés et gardés quand la clé obtenue est dans le dictionnaire (la plus longue gagne) ; la clé est **ajoutée** à côté (`SF-1027 [SF1027]`), rien n'est remplacé. Aucune forme de code n'est devinée. (Le premier essai par préfixes appris, qui laissait `QIS-46A-352` de côté, est retiré.)
- [ ] Tests sur tous les exemples réels de l'inventaire (une ligne par famille), faux positifs sur 200 vraies questions (`chat_turns`).
- [ ] Routage : BM25 ← question annotée ; embeddings ← question brute. Dépend du test 1 de l'étape 4.

### 3. Preprocess des documents — Mer.-Jeu. (F2)
- [ ] Même fonction sur les chunks : `ST-1027` → `ST-1027 [ST1027]`. Résultat dans une **nouvelle colonne** (`chunk_text_pp`), `chunk_text` brut intact (c'est lui qui part en génération).
- [ ] Où : dans le pipeline (`3c_chunks`), version de la regex stockée (`pp_version`) pour recalculer seulement ce qui change.
  *Validation : nb de chunks modifiés, 10 exemples avant/après.*

### 4. Architecture d'index — Jeu.-Ven. (F3)
Deux colonnes : embeddings sur `chunk_text` brut, BM25 sur `chunk_text_pp`.
- [ ] **Test 1 (bloquant)** : en DEV, un index Delta Sync à embeddings auto-gérés (`embedding_vector_column`, vecteurs calculés sur `chunk_text`) accepte-t-il une requête HYBRID dont la partie mot-clé porte sur `chunk_text_pp` ? Sinon : retrieval sur une seule colonne (`chunk_text_pp`) ou **2 index fusionnés par RRF** (embeddings brut + BM25 pp) — le RRF est de toute façon prévu plus tard.
- [ ] Calcul des embeddings : endpoint d'embedding actuel en batch dans le pipeline (coût et durée à mesurer sur DEV, 85k chunks).
- [ ] Nouvel index parallèle en DEV (job runner), `search.py` paramétré par nom d'index et colonnes ; l'index de l'app reste le même.
  *Validation : résultat du test 1, index ONLINE, requête test `ST1027` OK.*

### 5. Scoring du retrieval — Ven. (F4, jeu d'éval préparé dès F1)
- [ ] Jeu d'éval à refaire : ressortir les notebooks qui génèrent `eval_golden_questions` / `eval_synthetic_questions`, les améliorer, y ajouter des questions « codes » ; vérité terrain = **chunk exact**.
- [ ] Notebook : **MRR** = moyenne de 1/rang du 1er chunk pertinent ; **Recall@10** = pertinents dans le top 10 / pertinents. Niveau chunk. Base : [retrieval_eval.py](../../utils/evaluation/retrieval_eval.py).
- [ ] Puis MLflow : un run par config (index, regex `pp_version`, top_k), métriques + échecs en artefact.
  *Validation : baseline (index actuel) vs nouvel index, global et « questions avec code ».*

### 6. top_k optimal — Ven.-Lun. (F5)
- [ ] Balayage k ∈ {5, 8, 10, 15, 20, 30} (et rerank/raw, cf. `CHAT_RERANK_TOP_K` / `CHAT_RAW_TOP_K`) : Recall@k, tokens de contexte, latence. k = plus petit k où Recall@k plafonne.
  *Validation : tableau Recall@k vs k.*

### 7. Vérification + PROD — Lun.-Mar. (F6)
- [ ] DEV → UAT (hors heures, UAT utilisé en semaine) → PROD en dernier ; bascule de l'index sur **ton go**, ancien index gardé pour retour arrière.

## Further steps (notés)
- Lemmatisation / stemming des chunks et de la query. — TBD
- Réécriture de la query par LLM (décomposition + contexte) + RRF. — TBD
- Meilleure gestion des acronymes : glossaire + définitions (alimenté par l'inventaire de l'étape 1). — TBD

## Réponses à tes questions

**Framework / LangChain ?** Non, je recommande de ne pas en ajouter. Le chat appelle déjà Vector Search en REST, le rerank, le RRF (une quinzaine de lignes) et le preprocess sont du code simple et testé, et LangChain imposerait de réécrire le moteur sans rien apporter sur ce qu'on mesure. MLflow suffit pour l'éval et le suivi. À revoir seulement pour la réécriture LLM (Further).

**Ta réponse 7 (sigles sans chiffres, ex. DOA, AIDS)** : pas de regex de fusion, car `DOA` n'a pas de variante tiret à réparer. Ils vont dans le glossaire (Further) : sigle → définition, ajoutée à la query et/ou aux chunks. L'étape 1 chiffre combien il y en a.

**Ta réponse 5 (une ou plusieurs regex)** : une regex de **détection** générique (comme `CODE_PATTERN` de [codes.py](../../server/services/chat/engine/codes.py)) + une petite liste de **préfixes connus** issue de l'inventaire pour éviter les faux positifs (dates, normes). Un seul module, une constante `PP_VERSION`.

**Ta réponse 3 (`chunk_codes`)** : il extrait les codes des chunks (Lakebase) et ajoute les passages qui les citent. Jamais testé par toi : je le garde hors du chemin critique, on le mesure en baseline à l'étape 5, puis on décide : garder, étendre ou retirer.

## Restent ouvertes
1. Étape 4 (à instruire quand on y sera, sans impact sur l'étape 1) : si l'index à 2 colonnes est refusé, 1 colonne (`chunk_text_pp`) ou 2 index + RRF ? Je ferai les recherches à ce moment-là.
