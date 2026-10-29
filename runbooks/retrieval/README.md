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

**Lancer** : [f2_familles_codes.py](f2_familles_codes.py) dans un notebook DEV (tes droits suffisent : lecture de `dev_proj`/`uat_proj`, écriture de `dev_proj.qualibot.f2_familles_codes` et `f3_cles_codes` seulement). Cellules dans l'ordre. En tête, des cellules markdown décrivent chaque regex (formats de REF `REF-01`…`REF-12`, autres codes, exclusions, ordre d'évaluation, colonnes). Puis : contrôles qualité (le notebook s'arrête si l'un échoue), codes par motif, formats de REF du catalogue, codes du chat laissés tels quels, 2 graphiques.

Refonte 2026-10-28 (proposition validée : [Code patterns — proposal](https://claude.ai/artifact/1XnWMx57tYcQ4ND8ny3Qka)) : tout en anglais, motifs nommés (`REF-01`, `STD-01`, `EXC-07`…), traitements `CANONICAL_TOKEN` / `KEEP_AS_IS` / `GLOSSARY_CANDIDATE`, une table à 14 colonnes (`code_key`, `spellings`, `pattern_id`, `pattern_regex`, `treatment`, `canonical_token`, `match_key`, `catalogue_ref`…). Le dictionnaire `f3_cles_codes` (`match_key`, `canonical_token`, `pattern_id`) est écrit par ce notebook. Sigles séparés (`E.A.S.A`, `P/N`) et codes courts (`B1`) reçoivent le jeton.

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

### 2. Recollage des codes (query et documents) — en cours (F2)
**Deux étages, pas de grosse regex de codes.** Hors ligne, les motifs de [f2_familles_codes.py](f2_familles_codes.py) décident quelles clés entrent dans le dictionnaire. À la volée, un seul mécanisme générique, [code_keys.py](../../server/services/chat/engine/code_keys.py) : découpe en morceaux (`[A-Za-z]+|[0-9]+`), recollage des morceaux voisins (rien, ou 0 à 3 espaces autour de 1-2 séparateurs, ou 1 à 3 espaces), garde de la suite la plus longue présente dans le dictionnaire, clé ajoutée après le mot (`SF-1027 [SF1027]`). Toutes les graphies d'une REF donnent la même clé, sans suffixe de langue (`IT-243` et `LdB IT-243` → `LDBIT243`).

**Lancer** : [f3_dictionnaire_codes.py](f3_dictionnaire_codes.py) dans un notebook DEV, après `f2_familles_codes.py`. Tes droits suffisent ; il n'écrit rien. Il applique le dictionnaire aux passages DEV et aux questions du chat UAT : passages modifiés par motif, exemples avant/après par motif, questions avant/après. Règles du recollage (`MTC-01`…`MTC-07`) dans sa première cellule.

- [x] Mécanisme + tests sur les graphies réelles ([test_code_keys.py](../../tests/test_code_keys.py) : `SF 1027`, `MI - 1365`, `IF2.03.08_CZ`, `LDB IT 243`, `PR/LAT/506`, `FD_AQL_2193`, `QIS-46A-352-a`, `IAQ 04 04 05`, `Q0697MI_FR`, `TDA8 - 00/027`, `MI-13991Table`… et rien sur `LEVEL 2`, `XSF-1027`, `SF-10273`). Testé en local avec Spark.
- [ ] Tu lances f2 puis f3 et me renvoies les cellules 3 à 6. *Validation : exemples avant/après relus, aucune annotation absurde dans les questions.*
- [x] **Sigles reliés par `/` (`NDT/NDI`), décidé 2026-10-28.** Le jeton s'ajoute au texte sans rien remplacer (`NDT/NDI [NDTNDI]`) : `NDT` et `NDI` restent indexés, donc rien n'est raté, mais `NDTNDI` n'apporte rien. Le jeton est donc réservé aux sigles écrits lettre par lettre (`P/N`, `N/A`, `E.A.S.A`, `B.P.`, `N.D.T`, motif `ACR-02` = `^[A-Z]([./][A-Z])+\.?$`). `NDT/NDI`, `BPL/SBR`, `FAI/RPA` restent deux mots (`ACR-01`).
  Idée de Jules : faire remplacer `/` par « et »/« and » par le LLM à l'étape de traduction de la question quand l'utilisateur voulait dire « et ». Pas nécessaire pour la recherche par mots-clés, qui coupe déjà sur `/` (la question `NDT/NDI` cherche `NDT` et `NDI`). Utile seulement si on mesure (étape 5) que les embeddings comprennent mieux « NDT et NDI » que `NDT/NDI`. À tester alors dans `translate_query.md` / `rewrite.md`, sur le jeu d'éval.
- [x] Tour 4 (2026-10-28, tes 4 tableaux) : code court dans une chaîne (`A01` dans `PTD-…-A-01`, `A30` dans `145.A.30`) : clé de 5 caractères minimum ; décimales (`T3 1,2`, `R 0,55`) ; mot long + espace (`STRUCTURE 1000`, `EMBRAER - 550`) ; sigles à 2 lettres (`A/B`, `Y/N`) et sigle seul dans un nom de fichier (`Regles_SAP_controle`) : plus de jeton. Classement : « mot + nombre » (`de 5`, `to 0`, `inspect 100`) et « code + nombre séparé » (`AMS5862 15`, `7475-T7351 2.500`) exclus ; numéros de section + lettre (`5.3.Po`) exclus ; `F04641A` n'est plus pris pour une REF hors catalogue.
- [x] Tour 5 (2026-10-28, audit Genie) : mot courant + nombre (`de 2`, `OF 9`, `Inspect 100`, `en 2021`, `a 905`, `part 1/4`) exclu avant toute famille de code (`EXC-11`) ; casse exigée pour STD (`en 2021`), ACF (liste fermée, pas de décimale), SHC (graphie collée ou tiret), IDN (deux majuscules ou séparateur) ; REG limité à Part 21/145/147/66/M, CS, AMC/GM (`PART 1` exclu) ; LdB jamais sans préfixe (plus de `P100`/`IT100` → LDB…, le matcher accepte `LdB` devant un espace) ; clé ambiguë (`Q0087QP`, `QIS45D008`, `FORM607`) → elle-même + `candidate_refs`/`alternatives` ; UNL : plus de sous-clé dans un code plus long, `catalogue_ref` vide, `family_ref` à part ; OTH/IDN > 20 caractères exclus ; mois en liste fermée (plus de `MARKING8`) ; `8D`/`5S`/`3D`/`2D` et `ATA52` gardés ; familles PN et SAP ; sigles : mots courants et ≥ 7 lettres exclus, pluriel → singulier (`KPIS` → `KPI`) ; dictionnaire : colonnes `source`, `envs`, `alternatives`. Non fait : norme collée à un numéro de titre (`4179NAS410`), le jeton ne pourrait pas être écrit (mot entier seulement).
- [x] Tour 6 (2026-10-28, tes 6 tableaux) : sigles qui sont des mots (SCOPE, OBJECT, AIRBUS, OBSAH) exclus quand 20 % de leurs occurrences dans les passages sont écrites en minuscules ; pluriel seulement si le singulier existe (plus de AIRBUS → AIRBU) ; sigle à points seulement (plus de `M/C/T/J/N`, `E.E.E`) ; mot courant collé à 1-2 chiffres (`ET5`, `LA1`, `OF25`) et libellés tout en minuscules (`x0000_t75`) exclus.
- [x] Intégration dans le code (2026-10-28, refaite sans catalogue) : `utils/parsing_pipeline/code_normalization.py` = une table `CODE_PATTERNS` de motifs nommés (REF-01 familles générées, REF-03 site, REF-04 LdB, REF-07 IF, REF-10 phases, REF-12 à REF-16, STD/ATA/ACF/REG/MAT/ACR-02/SAP/PN/TRM), chacun avec exemples et contre-exemples testés ; aucune liste de REF dans le code. Couverture du catalogue de juillet : 7 510 REF sur 7 600 lues par un motif.
- [x] Tour f3 1 (2026-10-28) : listes de codes (`PRLAT506/549`) plus lues comme un code, normes AIPI/AIPS/AITM/BMS/MIL-A/ASTM, spécifications client (DGQT, ADET, MEP, BPS, AC71xx, D6-), paragraphes EASA (`145.A.30`), CCAR, registre `R03-10-002`, pas de code site dans un numéro de plan (`D2D-170-69210`), pas de `L_XXXX_YYY`. Les codes courts (SHC : `M3`, `A1`, `of 2`) ne sont volontairement pas normalisés.
- [ ] Tu relances f3 en DEV et me renvoies les mêmes 5 tables. *Validation : plus aucun code voulu raté, aucun faux code.*
- [ ] Après validation : `run_mode=rechunk` + synchro de l'index en DEV.
- [ ] Routage : BM25 ← question annotée ; embeddings ← question brute. Dépend du test 1 de l'étape 4. Rien n'est branché dans l'app.

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

### 5. Scoring du retrieval — en cours (F4)
Jeu d'éval : `dev_proj.qualibot.eval_golden_retrieval` (vérité terrain au niveau **document**, variantes de langue fusionnées).
Comparaison : **baseline** (aucune normalisation des codes, ni dans les documents ni dans la requête) contre **code_norm** (l'app actuelle).

**Lancer, dans l'ordre, en DEV :**
1. [eval_baseline_index.py](eval_baseline_index.py) : crée `dev_proj.qualibot.chunks_baseline_index` sur `chunks`. Il embarque `chunk_text` et ne synchronise ni `chunk_preprocessed` ni `cited_codes` (la partie mots-clés cherche dans toutes les colonnes synchronisées, `chunks_text_index` voit donc les `[SF1027]`). Même endpoint et même modèle d'embedding que `chunks_index`. Ne touche ni `chunks` ni l'index de l'app. Tes droits si tu as `CREATE TABLE` sur `dev_proj.qualibot` et `CAN USE` sur l'endpoint (la cellule 2 s'arrête net sinon : il faut alors le job runner). La cellule 3 attend la fin de l'embedding. À relancer après chaque run de parsing DEV : il resynchronise.
2. [eval_retrieval_mlflow.py](eval_retrieval_mlflow.py) : Run all, tes droits suffisent, n'écrit rien dans Unity Catalog. Expérience MLflow `/Shared/qualibot/retrieval_eval_dev` : un run parent (comparaison) et un run enfant par (recherche, version).
   - Recherche `index` : 1 requête hybride + reranker, 50 passages → courbes recall@k et hit@k de k = 1 à 50.
   - Recherche `app` : le vrai `retrieve_for_turn` importé du code déployé (`/Workspace/.bundle/qualibot/dev/files`). Pour la baseline, chaque étape de normalisation des codes de l'app est coupée en mémoire (requête, recherche des citations, choix de la variante de langue).
   - Métriques : MRR, MRR@10, nDCG@10, recall@k, hit@k, all@k, rang médian, taux de ratés. Ventilées par langue, division, « question avec code » et nombre de documents attendus. Différences avec intervalle bootstrap à 95 %.
   - Sorties : tables par question, listes des questions améliorées et dégradées, et 5 graphiques (courbes @k, MRR par tranche, rangs baseline contre code_norm, questions qui bougent, score du 1er passage des `no_document`).
   - `RUN_PAIRWISE_JUDGE = True` (désactivé par défaut) : réponse de chaque version à partir de ses 8 premiers passages, jugée en pairwise dans les deux ordres par `databricks-gpt-5-6-luna`.
- [ ] Tu lances 1 puis 2, et tu me renvoies le lien du run parent et la sortie des cellules de résumé (tableau `SUMMARY`, `DELTAS`).
  *Validation : baseline vs code_norm, global et « questions avec code ».*
- [ ] `unknown_refs` : traitement à décider (aujourd'hui gardées dans les attendus, `DROP_UNKNOWN_REFS = True` les retire).

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
