# Chatbot Qualibot — configuration retenue et journal des tests (2026-10-06 → 2026-10-08)

Un seul document pour tout ce qui a été mesuré sur le chatbot : la configuration gardée (= le code
actuel), le sens des noms d'essai, les résultats qui ont décidé, ce qui a été écarté, ce qui reste à
tester. La partie 2 est le journal complet, tel qu'il a été tenu pendant les essais.

Code : `server/services/chat_vsi.py` (+ `chat_vsi_llm.py`, `chat_vsi_titles.py`,
`translation_bridge.py`). Notebooks de mesure gardés : `utils/evaluation/retrieval_eval.py`
(recherche seule) et `pairwise_answers.py` (réponses côte à côte). Tout le reste est dans `archive/`
(`archive/README.md`). Robustesse (secours, relances) : `docs/chat_vsi_robustesse_2026-10.md`.

# Partie 1 — Ce qu'on garde

## A. Configuration retenue

| Étape | Ce qui se passe | Mesuré dans |
|---|---|---|
| Langue | fastText détecte la langue. Une question ni française ni anglaise est traduite en anglais (GPT-5.6 Luna, secours GPT-6 Luna) ; la réponse est retraduite | Juillet (`translation_bridge.py`) |
| Réécriture | GPT-6 Luna (secours GPT-5.6 Luna) écrit une requête en français **et** une en anglais, acronymes développés | `u-bi`, `u-all-luna6` (journal § 2.3, § 5.6) |
| Recherche | 3 requêtes (question, français, anglais), chacune HYBRID : 12 passages reclassés par le reranker de Vector Search (colonnes `REF`, `semantic_headers`, `chunk_text`) **plus** 10 passages bruts, fusionnés par rang | `union-ctx` (journal § 2.1, § 2.2) |
| Documents cités par REF | Jusqu'à 6 REF nommées dans la conversation : 8 passages de ces documents, placés en tête | `union-ctx-ref` |
| Documents cités par titre | Titres du catalogue proches de la question : 3 documents, 6 passages, ajoutés à la fin. Le catalogue est la table Lakebase `doc_catalog`, réécrite après chaque run du pipeline depuis `parse_manifest` (seuls les documents présents dans l'index sont proposés) ; mesuré le 2026-10-07 avec l'ancien instantané `doc_catalog.json` | `u-title` |
| Une langue par document | Q0102QP_GB et Q0102QP_BG ne prennent qu'une place | `u-1lang` |
| Division (AS / IS) | **Un seul index** `chunks_index` ; le choix AS / IS est un filtre Vector Search sur la colonne `division`, appliqué dans la requête elle-même (pas après) : déterministe, sans coût mesurable | Déjà utilisé pour les REF (filtre `REF`) |
| Prompt | Instructions de la division (`instructions_<div>.md`), puis les règles de réponse (`answer_rules.md` : seulement les documents, refus hors sujet, glossaire des préfixes de REF, règle de citation avec exception pour une URL écrite dans un passage), puis les documents numérotés, la question, et une ligne qui **nomme** la langue de réponse | `luna6-prompt` (`u-all+v3+lang`) |
| Réponse | GPT-6 Luna, secours GPT-5.6 Luna, 8 000 tokens maximum (réflexion comprise) | `luna6-versions`, `luna6-prompt` |
| Index | Découpage 150 / 300 / 450 tokens, 1 600 caractères maximum, 12 % de chevauchement, préfixe `[Source: …]`, sommaires et cartouches **marqués** (`chunk_content_type`) mais **gardés** | `idx-v2b-all` (journal § 5.6) |

**Aucun modèle Claude dans le chatbot** (décision du 2026-10-08) : réécriture, réponse, traduction et
secours sont des modèles Luna.

Le filtre de division : la question « on peut filtrer de manière déterministe au moment de la requête
sans perdre de temps ? » — oui. `filters_json={"division": ["AS"]}` restreint la recherche dans
l'index avant le classement : un passage IS ne peut pas sortir pour une question AS, et la requête
coûte le même temps. Les trois index par division (`chunks_as_index`, `chunks_is_index`) et leurs
tables `src_chunks_as` / `src_chunks_is` n'existaient que pour le Knowledge Assistant, qui ne savait
pas filtrer : ils disparaissent.

## B. Les noms du journal

Le journal garde les noms utilisés pendant les essais. Ils ne figurent plus dans le code.

| Nom dans le journal | Ce que c'était | Aujourd'hui |
|---|---|---|
| KA, `ka`, Chat KA | Knowledge Assistant (Agent Bricks), le premier chatbot | Retiré le 2026-10-08 (`archive/knowledge_assistant/`) |
| VSI, Chat VSI | Le chatbot sans KA : Vector Search + un LLM | Le chatbot, onglet « Chat » |
| `baseline` | Première version du Chat VSI : une requête, 10 passages, Sonnet 4.6 | `archive/chat_vsi_base/` |
| `rerank`, `union`, `union-ctx`, `u-*`, `u-all` | Réglages de la recherche dans `chat_vsi_rerank.py` | `u-all` = la recherche actuelle, sans réglage |
| Instructions `ka` | Les instructions du KA, recopiées | `server/config/chat_vsi/instructions_<div>.md` |
| Instructions `v2` | Instructions réécrites pour le VSI | Écartées (`archive/chat_vsi_lab/config/rewritten_instructions/`) |
| Instructions `v3`, « addendum » | Instructions KA + règles d'ancrage, refus hors sujet, glossaire | `answer_rules.md` (premier jet : `archive/chat_vsi_lab/config/answer_rules_first_draft.md`) |
| `lang`, `CHAT_VSI_LANGUAGE_REMINDER` | Ligne de langue après la question | Toujours active |
| `CHAT_VSI_SKIP_NOISE`, `-clean` | Écarter sommaires et cartouches de la recherche | Écarté (−11 points) |
| Index `v1` (`chunks_index_v1`) | Découpage 250 / 500 / 1 000 tokens, l'index UAT copié en DEV | Remplacé |
| Index `v2a` | Découpage corrigé, même taille que `v1` | Écarté |
| Index `v2b` (`chunks_v2b`) | Découpage corrigé, 150 / 300 / 450 tokens | **Le découpage actuel** : table `chunks`, index `chunks_index` |
| Index `v2c` | `v2b` sans préfixe `[Source: …]` | Préparé, non mesuré |
| `idx-<v>`, `idx-<v>-all` | Une recherche (`union-ctx` ou `u-all`) sur l'index de test `<v>` | — |
| `luna6-versions`, `luna6-prompt`, `luna6-v2b` | Runs de `pairwise_answers` (`eval_id`) | — |

## C. Les résultats qui ont décidé

| Question | Résultat | Décision |
|---|---|---|
| Le reranker aide-t-il ? | Seul : 48 % de réponses correctes (contre 67 %). **Ajouté** aux résultats bruts : 71 à 81 % | Union reclassés + bruts |
| Nos prompts réécrits ? | `v2` : jamais mieux que le prompt KA | Prompt KA + règles de réponse |
| Recherche complète (`u-all`) | 80 % des documents attendus contre 72 % (`union-ctx`) | `u-all` |
| GPT-6 Luna contre Sonnet 5.5 | `u-all+v3+lang` : 19 gagnés / 16 perdus / 5 égalités, inventions 0,63 contre 1,59, 0,0036 € contre 0,089 € par question | GPT-6 Luna |
| Réécriture par GPT-6 Luna plutôt que Sonnet 4.6 | 80,0 % contre 80 % | GPT-6 Luna, plus de Claude |
| Découpage `v2b` contre `v1` | Recherche 83,6 % contre 78,5 % (second run), contexte 11k tokens contre 21k ; réponses 10 / 10 / 20, exactitude 2,81 contre 2,61, inventions 0,40 contre 0,53, 0,0022 € contre 0,0036 €, premier mot 5,9 s contre 8,1 s | `v2b` |
| 20 passages reclassés au lieu de 12 | 80,5 % contre 83,6 % | 12 |
| Écarter sommaires et cartouches | −11 points | Marqués, gardés |
| KA (réponses stockées) contre VSI | 1 gagné / 36 perdus / 3 égalités | KA retiré |

## D. Testé et écarté

- Le reranker seul (`rerank`), sans les résultats bruts.
- Les instructions réécrites `v2`, seules ou avec union.
- Le budget de contexte (25 000 ou 35 000 caractères) : moins de documents trouvés.
- 20 ou 25 passages reclassés par requête.
- 3 passages au plus par document (`u-1lang-k20-cap3`).
- Écarter les passages marqués sommaire / cartouche / texte répété (`CHAT_VSI_SKIP_NOISE`).
- Le découpage `v2a` (même taille corrigée) : battu par `v2b`.
- Sonnet 4.6, Sonnet 5.5 pour répondre ; Sonnet 4.6 et GPT-5.6 Luna pour réécrire.
- Le Knowledge Assistant.

Le code de chacune de ces options est dans `archive/chat_vsi_lab/`.

## E. À tester plus tard

| Piste | Ce que c'est | Où en est le code |
|---|---|---|
| E1 — fiche par document | Un passage de synthèse par document, généré par LLM (objet, domaine, sujets, rôles, documents cités). ≈ 15 € | `archive/evaluation/rechunk_experiment.py`, widget `doc_cards` (désactivé) |
| E2 — contexte par passage | Une ou deux phrases en tête de chaque passage pour le situer (méthode « Contextual Retrieval »). ≈ 50 € | Même notebook, widget `chunk_context` (désactivé) |
| `v2c` — sans préfixe `[Source: …]` | Mesure l'effet du préfixe sur la recherche | Même notebook |
| **Nombre de passages** (recherche mesurée le 2026-10-08, journal § 5.7 ; reste la comparaison des réponses) | Jusqu'à 3 × (12 reclassés + 10 bruts) = 66 passages, sans plafond. Les passages bruts ont été gardés parce que le reranker seul perdait des réponses (48 % contre 76 %), mais c'était avec les passages de 4 000 caractères dont le reranker ne lit que les 2 000 premiers. Avec le découpage actuel (≤ 1 600), le reranker seul n'a jamais été remesuré | Réglages `CHAT_VSI_RERANK_TOP_K`, `CHAT_VSI_RAW_TOP_K`, `CHAT_VSI_MAX_SEARCH_PASSAGES` (défauts inchangés) ; `operations_dev.md`, bloc T |
| Pistes côté recherche R1 à R12 | Passages voisins, ordre du document, filtre par type, seuil « je ne sais pas »… | Journal § 4, rien de codé |
| Documents d'avant 2018 | Leur fiche dans le chatbot (P11) | Décision métier ; pipeline prêt (`parsing_archive_notices_in_rag`) |
| Numéros de page et de slide | P13, re-parsing GPU | Rien de codé |

Pour mesurer une piste : construire un index de test en DEV, puis `retrieval_eval` avec
`indexes = chat,essai=dev_landingzone.qualibot.<index de test>` et, si la recherche gagne,
`pairwise_answers` avec le même index en concurrent.

---

# Partie 2 — Journal des tests

Tenu pendant les essais, inchangé depuis : les noms sont ceux du § B. Les fichiers cités
(`chat_vsi_rerank.py`, `golden_eval_ka_vs_vsi.py`, `rechunk_experiment.py`…) sont dans `archive/`.

## 0. Lexique et fonctionnement

### 0.1 Les mots et acronymes

| Terme | Ce que c'est |
|---|---|
| **KA** | Knowledge Assistant : l'agent « clé en main » de Databricks (Agent Bricks) qui sert aujourd'hui le chatbot. Il cherche dans notre index et rédige la réponse, mais on ne voit ni ne règle ce qu'il fait à l'intérieur. Databricks l'arrête : il faut le remplacer |
| **VSI** | Le remplaçant écrit par Mehdi (`chat_vsi.py`) : l'app interroge elle-même l'index Vector Search (*Vector Search Index*), puis demande la réponse à un LLM. Tout est réglable |
| **RAG** | *Retrieval-Augmented Generation* : chercher des passages de documents, puis les donner au LLM pour qu'il réponde à partir d'eux |
| **LLM** | Le modèle de langage qui rédige (Sonnet 4.6, Sonnet 5.5, GPT-5.6 Luna…) |
| **Passage** (*chunk*) | Un morceau d'un document (250 à 1 000 tokens) : c'est l'unité indexée et renvoyée par la recherche |
| **Token** | Unité de texte des modèles, environ 4 caractères en français. Les coûts et les limites se comptent en tokens |
| **REF** | La référence Intraqual d'un document (QP-1518, MI-14183…). Une même REF existe souvent en plusieurs langues (QP-1518, QP-1518_GB, Q0102QP_BG…) |
| **IDDOC** | L'identifiant Intraqual d'une révision de document. Chaque révision a un nouvel IDDOC |
| **Embedding** | Le vecteur qui représente le sens d'un texte. Calculé par `databricks-qwen3-embedding-0-6b` pour chaque passage et pour chaque question |
| **Recherche hybride** (*HYBRID*) | Vector Search combine deux recherches : par sens (embedding) et par mots-clés (comme un moteur classique, type BM25). Elle renvoie les N meilleurs passages |
| **Reranker** | Un second modèle (`databricks_reranker`, un *cross-encoder*) qui relit la question et chaque passage candidat ensemble et les reclasse. Plus précis que l'embedding, mais il ne voit que les 50 premiers candidats et que les 2 000 premiers caractères de chaque passage |
| **Réécriture** (*rewrite*) | Avant de chercher, un LLM reformule la question en une requête autonome en français (« et pour IS ? » → « exigences de qualification CND pour la division IS »). Elle passe avant la recherche, donc retarde le premier mot |
| **Golden** | Le jeu de 21 questions de référence avec réponses et documents attendus (`qualibot_eval_golden`) |
| **Juge** | Un LLM qui note une réponse (juste / fausse) par rapport à l'attendu. Ici les juges MLflow `Correctness` et `ExpectationsGuidelines` |
| **Recall** (rappel, « docs trouvés ») | Part des documents attendus qui sont dans les passages envoyés au LLM. 100 % = tous les documents attendus sont dans le contexte |
| **`at_least_one_pct`** | Part des questions où au moins un document attendu est trouvé |
| **`recall_top5_pct`** | Rappel limité aux 5 premiers documents du contexte : compare des configurations qui n'envoient pas le même nombre de documents |
| **Contexte** | Tout ce qu'on envoie au LLM : instructions + passages + question. Sa taille fait le coût |
| **Premier mot** (*TTFT*) | Temps avant que la réponse commence à s'afficher |
| **AS / IS** | Les deux divisions Latécoère (Aérostructures, Interconnection Systems). Il y a un index par division et un index `ALL` |
| **DBU** | L'unité de facturation Databricks (0,078 € dans le contrat) |
| **Checkpoint** | `_pipeline_checkpoint` : la table où le pipeline garde le texte parsé de chaque document. Re-découper part de là, sans re-parser |
| **TOC** | *Table of contents* : table des matières |
| **OCR** | Lecture du texte d'une image ou d'un scan (ici faite par GPT-5.6 Luna) |

### 0.2 Ce qui se passe à chaque question

**Baseline (le code de Mehdi, `chat_vsi.py`)**
1. Si la question n'est ni en français ni en anglais, elle est traduite en anglais.
2. Le LLM de réponse réécrit la question en une requête française.
3. **Deux recherches hybrides** : la question telle quelle, et la requête française. **10
   passages chacune** (`CHAT_VSI_NUM_RESULTS`).
4. Les deux listes sont fusionnées par meilleur rang, sans doublon : 10 à 20 passages.
5. Les passages sont regroupés par document (REF) et numérotés `[1]`, `[2]`…
6. Le LLM reçoit les instructions du KA, la règle de citation, les documents et la question,
   et rédige la réponse.

**Variante `rerank` (`chat_vsi_rerank.py`)** : mêmes étapes, seule l'étape 3 change selon les
réglages ci-dessous. Tous sont désactivés par défaut, et la baseline n'a jamais été modifiée.

### 0.3 Ce que fait vraiment chaque réglage

Les noms d'essais sont faits de ces morceaux : `union-ctx-s55-ref` = union + ctx + Sonnet 5.5 +
recherche par REF.

| Morceau du nom | Réglage | Ce qui se passe réellement |
|---|---|---|
| `rerank` | variante `rerank` | Pour chacune des 2 requêtes, Vector Search prend ses 50 meilleurs candidats hybrides, les fait **reclasser par le reranker**, et n'en rend que **12** (`CHAT_VSI_RERANK_TOP_K`). Ces 12 **remplacent** les 10 de la baseline. Le reranker lit le texte du passage seul |
| `union` | `CHAT_VSI_RERANK_MERGE=union` | On fait **les deux** : la recherche reclassée (12 par requête) **et** la recherche brute de la baseline (10 par requête), et on fusionne. Le reranker peut **ajouter** des passages que la recherche brute classait mal, sans **retirer** ceux qu'elle trouvait. Environ 30 passages au lieu de 15 |
| `ctx` | `CHAT_VSI_RERANK_COLUMNS=REF,semantic_headers,chunk_text` | Le reranker lit en plus la REF et les titres de section du passage, avant le texte. Sans effet mesurable : le texte commence déjà par `[Source: REF \| Title \| …]` |
| `k20` | `CHAT_VSI_RERANK_TOP_K=20` | 20 passages reclassés par requête au lieu de 12 |
| `b35k`, `b25k`, `budget` | `CHAT_VSI_RERANK_TOP_K=25` + `CHAT_VSI_CONTEXT_BUDGET_CHARS=35000` ou `25000` | On prend 25 passages reclassés, puis on les garde **dans l'ordre du classement jusqu'à 35 000 (ou 25 000) caractères** de contexte, quel que soit leur nombre. But : un contexte de taille fixe, donc un coût fixe |
| `cap3` | `CHAT_VSI_MAX_PASSAGES_PER_DOC=3` | Au plus 3 passages par document, pour laisser la place à d'autres documents |
| `ref` | `CHAT_VSI_REF_LOOKUP=on` | Si la question (ou un tour précédent) cite une REF connue du catalogue (« résume le MI-14242 »), on fait **une recherche de plus, limitée à ce document** et à ses versions linguistiques (6 REF au plus, 8 passages). Ses passages passent **en premier** |
| `v2` | `CHAT_VSI_INSTRUCTIONS=v2` | Nos instructions VSI réécrites (`server/config/chat_vsi_v2/`) **à la place** de celles du KA |
| `v3` | `CHAT_VSI_INSTRUCTIONS=v3` | Instructions du KA **plus** un ajout : règles d'ancrage, glossaire des types de documents |
| `s55` | `CHAT_VSI_LLM_ENDPOINT=databricks-claude-sonnet-5-5` | Sonnet 5.5 rédige **et** réécrit (plafonds relevés : il réfléchit avant de répondre) |
| `run2` | — | La même configuration relancée, pour mesurer le bruit |
| `title` | `CHAT_VSI_TITLE_LOOKUP=on` | On compare les mots de la question aux **titres des 7 600 documents du catalogue**. Les 3 documents dont le titre correspond le mieux ont droit à une recherche limitée à eux (6 passages), **ajoutée à la fin** du contexte |
| `bi` | `CHAT_VSI_REWRITE=bilingual` | La réécriture produit une requête française **et** une requête anglaise, acronymes développés. **3 recherches** au lieu de 2 (question, FR, EN) |
| `luna` | `CHAT_VSI_REWRITE_ENDPOINT=databricks-gpt-5-6-luna` | La réécriture est faite par GPT-5.6 Luna, petit modèle rapide, et plus par le modèle de réponse |
| `1lang` | `CHAT_VSI_ONE_LANGUAGE=on` | Pour un document présent en plusieurs langues (Q0102QP_GB, Q0102QP_BG), on ne garde que les passages de la version **la mieux classée** |
| (toujours) | `CHAT_VSI_SEARCH_RETRIES=2` | Une recherche refusée (rafale, délai) est relancée 2 fois avant d'échouer |

---

## 1. Comment on a mesuré

**Évaluation golden** (`golden_eval_ka_vs_vsi.py`, table `dev_landingzone.qualibot.eval_golden_runs`)
- 21 questions de `dev_landingzone.qualibot.qualibot_eval_golden`. Pour chacune, la réponse
  complète est générée, puis notée par les juges MLflow `Correctness` et
  `ExpectationsGuidelines`. On mesure aussi `golden_doc_recall`, la part des documents attendus
  retrouvés.
- **Bruit : ±2 questions, soit environ ±10 points, entre deux runs identiques.** Deux
  preuves : `rerank-union-ctx` et `union-ctx-run2` sont la même configuration et font 76,2 %
  contre 71,4 %. `union-ctx-s55-ref` n'a jamais déclenché sa recherche par REF, il était donc
  identique à `union-ctx-s55`, et il fait 71,4 % contre 81 %.
- 7 questions sur 21 sont des refus ou des cas « pas dans la documentation » que tout le monde
  réussit. Il reste environ 14 questions qui départagent vraiment, et chacune pèse 7 points.

**Évaluation de la recherche seule** (`retrieval_eval.py`, table `dev_landingzone.qualibot.eval_retrieval_runs`)
- Pour chaque question, on vérifie si les documents attendus sont dans les passages envoyés au
  modèle. Pas de génération, pas de juge : c'est reproductible et ça coûte environ 0,001 € par
  question (seule la réécriture appelle un LLM).
- **65 questions** :
  - 15 du golden ;
  - 34 synthétiques (`uat_landingzone.qualibot.synthetic_retrieval_questions_v2`) ;
  - 16 retours négatifs où l'utilisateur a nommé le document attendu
    (`uat_landingzone.qualibot.feedback_failure_cases`).
- **Limite** : les documents « pertinents » des questions synthétiques ont été choisis parmi
  ceux que le KA d'UAT avait retrouvés. Ce jeu favorise donc les recherches qui ressemblent à
  celle du KA. Les **retours négatifs** sont le jeu le plus réaliste, mais il ne compte que
  16 questions.
- Le premier run des configurations avec reranker a eu des erreurs : le service d'embedding
  refuse les rafales de requêtes. Un score calculé sur moins de questions est faussé. C'est
  corrigé : 2 relances dans l'app, erreurs relancées question par question, tableaux calculés
  sur les questions réussies par toutes les configurations.

---

## 2. Configurations testées

### 2.1 Golden : qualité des réponses (21 questions)

| Essai | Modèle | Correct | Docs trouvés | Commentaire |
|---|---|---|---|---|
| `union-ctx-s55` | Sonnet 5.5 | **81 %** | 79 % | Meilleur score, mais sur un seul run. Premier mot à 8,3 s, 0,091 €/question |
| `rerank-union-ctx` | Sonnet 4.6 | 76 % | 79 % | Union, le reranker lit aussi REF et titres de section |
| `rerank-union` | Sonnet 4.6 | 76 % | 78 % | Union, reranker sur le texte seul |
| `union-ctx-run2` | Sonnet 4.6 | 71 % | 83 % | Même config que `rerank-union-ctx` : mesure du bruit. 0,088 €/question |
| `union-ctx-s55-ref` | Sonnet 5.5 | 71 % | 71 % | REF jamais déclenchée (corrigé depuis) : c'est du bruit |
| `union-ctx-s55-budget` | Sonnet 5.5 | 67 % | 86 % | 25 passages reclassés par requête, contexte plafonné à 35 000 caractères. 37 % moins cher (0,057 €) |
| `baseline` (2 runs) | Sonnet 4.6 | 67 % / 67 % | 58 % / 57 % | Le code livré par Mehdi |
| `prompt-v2` | Sonnet 4.6 | 67 % | 57 % | Recherche baseline, prompt VSI v2 |
| `best-v2` | Sonnet 4.6 | 67 % | 68 % | Union, v2, budget 35 000, REF |
| `ka` | KA | 62 % | 66 % | Le plus rapide (8,4 s en médiane) |
| `union-v2` | Sonnet 4.6 | 62 % | 65 % | Union avec le prompt v2 |
| `union-ctx-s55-v3` | Sonnet 5.5 | 62 % | 71 % | Prompt KA, règles d'ancrage, glossaire des préfixes |
| `rerank` | Sonnet 4.6 | 48 % | 64 % | Le reranker remplace la recherche brute |
| `union-ctx-s55-all` | Sonnet 5.5 | 48 % | 71 % | v3, REF et budget ensemble |

Ce qu'on en retient :
- **Le reranker seul fait perdre des réponses.** Il concentre les résultats sur ce qui
  ressemble le plus à la question. Sur « quels documents parlent de X », il a remplacé les
  documents Level 3 par des listes de documentation. Il a aussi choisi une table des matières
  plutôt que la définition (glossaire APO). L'union corrige ces deux défauts.
- **Nos prompts réécrits n'ont jamais fait mieux que le prompt du KA.** Ils ont même fait
  perdre des questions : on garde le prompt KA.
- **Le juge est capricieux.** Au moins trois verdicts étaient contestables (REP_OUT, peinture,
  FAI). Une attente du golden est même fausse : REP_OUT existe bien dans SF-1271.
- **Sonnet 5.5** :
  - il coûte autant que Sonnet 4.6 : son tarif est 33 % plus bas, mais son tokenizer compte
    environ 40 % de tokens de plus en entrée (31 800 contre 22 750), et il écrit 2,5 fois plus
    en sortie, réflexion comprise ;
  - son premier mot arrive à 8 s au lieu de 3 s.

### 2.2 Recherche seule : documents attendus trouvés (65 questions)

| Config | Global | Retours négatifs | Contexte | Commentaire |
|---|---|---|---|---|
| `union-ctx-ref` | 79 % | 38 % | 14 300 tok | Recherche par REF citée. À remesurer (10 erreurs) |
| `union` | 72 % | 27 % | 14 500 tok | À remesurer (12 erreurs) |
| `union-ctx-k20` | 72 % | 29 % | 18 700 tok | 20 passages reclassés : plus cher, pas mieux (9 erreurs) |
| `union-ctx` | 72 % | 33 % | 14 700 tok | **Référence actuelle** (6 erreurs) |
| `rerank` | 68 % | 31 % | 9 600 tok | Remplace la recherche brute |
| `union-ctx-b35k` | 66 % | 19 % | 8 600 tok | Plafond de 35 000 caractères |
| `union-ctx-b25k` | 64 % | 21 % | 6 200 tok | Plafond de 25 000 caractères (9 erreurs) |
| `baseline` | 61,5 % | 12,5 % | 9 000 tok | Recherche d'origine |

Les erreurs du premier run faussent les scores : une question en erreur est simplement
retirée du calcul de sa configuration. Le prochain run relance ces questions et compare toutes
les configurations sur les mêmes questions.

- **Toutes les configurations avec reranker battent la baseline.** Sur les retours négatifs,
  elles trouvent 2 à 3 fois plus de documents attendus.
- **Plafonner le contexte fait perdre des documents**, surtout pour les questions qui en
  attendent plusieurs.
- **Donner la REF et les titres de section au reranker ne change rien** (`union` contre
  `union-ctx`). C'est normal : chaque passage commence déjà par
  `[Source: REF | Title | Division | Category | Date]` (voir P5).
- **Les retours négatifs restent le point faible** : 38 % au mieux. Une partie des documents
  attendus n'est peut-être même pas dans l'index. La requête Q2 du § 7 le vérifie, et ça
  change la lecture de ce chiffre.

### 2.3 Vague 3 : prête, pas encore mesurée

Chaque config part de `union-ctx` et ne change qu'une chose, sauf `u-all`.

| Config | Ce qui change | Pourquoi |
|---|---|---|
| `u-title` | Recherche dans les titres du catalogue (3 documents au plus) | Beaucoup de questions demandent un document par son sujet. Prototype local : « processus Stocker » → IQ22-223, « work centers » → IN_MRPC009, FAI → Q0102QP |
| `u-bi` | Réécriture en français **et** en anglais, acronymes développés | Des documents attendus n'existent qu'en anglais (QP-2270, MI-14183, Q0062MI_GB). Un test de juillet (`translation_bridge.py`) montrait déjà que l'anglais ramène des documents plus variés |
| `u-bi-luna` | `u-bi` avec la réécriture faite par GPT-5.6 Luna | Mesure si un petit modèle rapide réécrit aussi bien. La colonne `search_p50_s` donne le gain de temps |
| `u-1lang` | Une seule langue par document, la mieux classée | Q0102QP_GB et Q0102QP_BG disent la même chose et prennent deux places |
| `u-1lang-k20-cap3` | Une langue, 20 passages, 3 au plus par document | Plus de documents différents dans le même contexte |
| `u-all` | REF + titres + bilingue + une langue | Tout ensemble |

Deux mesures ont aussi été ajoutées :
- **`recall_top5_pct`** : la part des documents attendus parmi les 5 premiers du contexte.
  Elle compare les configurations à taille égale : une config qui met plus de documents ne
  gagne plus mécaniquement.
- **`titled` et `en_query`**, enregistrés pour chaque question.

### 2.4 Ce qu'on garde, ce qu'on écarte

- **On garde** : le mode union, `CHAT_VSI_RERANK_COLUMNS=REF,semantic_headers,chunk_text`
  (neutre mais sans coût), 12 passages reclassés, le prompt KA, les 2 relances de recherche.
- **On écarte** : le reranker seul, les prompts v2 et v3, le budget de contexte serré, 20
  passages.
- **À trancher** :
  - la recherche par REF, à la vague 3 ;
  - le titre, le bilingue et une langue par document, à la vague 3 ;
  - le modèle de réponse, par la comparaison côte à côte (§ 3.3).
- **Pas encore fait** : mettre la configuration retenue par défaut dans l'app DEV.
  `app.yaml` est toujours sur `CHAT_VSI_VARIANT=baseline`.

### 2.5 Tout ce qui reste à tester, au même endroit

**Prêt, il suffit de lancer** (`retrieval_eval`, vague 3, § 2.3)
- `u-title`, `u-bi`, `u-bi-luna`, `u-1lang`, `u-1lang-k20-cap3`, `u-all`.
- Remesure sans erreurs de `union-ctx-ref`, `union`, `union-ctx-k20`, `union-ctx-b25k` : le
  même run relance leurs questions en erreur.

**Après la vague 3, sur la meilleure recherche**
- Un run golden de la meilleure configuration, avec Sonnet 4.6 : vérifie que le gain de
  recherche se retrouve dans les réponses.
- La comparaison côte à côte des modèles de réponse (§ 3.3) : Sonnet 4.6, Sonnet 5.5, Sonnet
  5.5 avec moins de réflexion, Haiku 4.5, GPT-5.6 Luna. Notebook à écrire.

**Côté app, à coder (§ 4)** : passages voisins (R1), ordre du document (R2), métadonnées une
seule fois (R3), requête mots-clés (R6), filtre par type (R7), suivi de conversation (R8), seuil
« je ne sais pas » (R9), langue de la question (R10), reranker par LLM (R11), nombre de passages
adaptatif (R12), consigne de requête Qwen (R5, après un essai d'API). R4 (reranker sur le texte
seul) est déjà mesuré : c'est `union` contre `union-ctx`, sans différence.

**Côté parsing, sur un index de test DEV (§ 5)**
- Découpage corrigé à taille égale (v2a), découpage plus court (v2b), contre l'actuel (v1).
- Passages d'image avec légende et section, retranscriptions longues découpées (P7).
- Métadonnées type, indice, langue dans l'index (P10), tables des matières et textes répétés
  marqués (P9), préfixe `[Source: …]` avec ou sans (P15).
- Enrichissements : fiche par document (E1), contexte par passage (E2).

**Décisions métier, pas des tests** : fiches des documents d'avant 2018 dans le chatbot (P11),
numéros de page et de slide avec re-parsing GPU (P13).

### 2.6 Réponses côte à côte : GPT-6 Luna contre Sonnet 5.5 (2026-10-08)

`pairwise_answers`, eval_id `luna6-versions` : 40 questions (21 golden + 19 vraies questions DEV
auxquelles le KA a répondu). Référence Sonnet 5.5 + `union-ctx`. Juge GPT-5.6 Luna, dans les deux
ordres : une version gagne une question seulement quand les deux lectures sont d'accord.

| Concurrent | Gagnés / perdus / égalités | Fidélité (con / réf) | Exactitude (con / réf) | Inventions (con / réf) | Juge constant |
|---|---|---|---|---|---|
| Luna @ `u-all` | 16 / 15 / 9 | 2.60 / 2.49 | 2.51 / 2.64 | 0.76 / 1.40 | 78 % |
| Luna @ `union-ctx` | 15 / 16 / 9 | 2.43 / 2.41 | 2.50 / 2.84 | 0.99 / 1.41 | 78 % |
| Luna @ `u-title` | 13 / 16 / 11 | 2.60 / 2.48 | 2.55 / 2.70 | 0.83 / 1.36 | 73 % |
| Luna @ `u-bi` | 13 / 19 / 8 | 2.43 / 2.51 | 2.40 / 2.70 | 1.05 / 1.35 | 80 % |
| KA (réponses stockées) | 1 / 36 / 3 | 1.70 / 2.64 | 1.69 / 2.80 | 2.33 / 0.98 | 93 % |

- Luna @ `u-all` fait jeu égal avec Sonnet 5.5 et invente deux fois moins, pour 0.0034 € par
  question contre 0.093 € (≈ 4 %). Premier token : 6.8 s contre 6.1 s.
- Le contexte large aide vraiment Luna : `u-all` est sa meilleure version.
- Le KA est loin derrière.
- Défauts vus dans le détail :
  - Luna répond à des demandes hors sujet (recette, liste de courses) que Sonnet refuse : l'éval
    tournait avec les instructions `ka`, qui n'ont pas de règle de refus (elle est dans `v3`).
  - Réponses dans une autre langue que la question. En partie un défaut de l'éval : elle ne
    retraduisait pas les réponses alors que l'app le fait (`translate_answer_back`, pont de
    traduction activé sur les cibles). Le reste (anglais → bulgare) est un vrai écart du modèle :
    la règle de langue est en tête des instructions, 20 à 25k tokens avant la question.

Rerun préparé (`luna6-prompt`, défauts du notebook) : Luna @ `u-all` seul (témoin), `+v3`,
`+v3+lang` (`CHAT_VSI_LANGUAGE_REMINDER` : une ligne après la question), réponses retraduites comme
dans l'app (widget `translate_back`). Recherches et réponses déjà en cache : seules les réponses
`v3` et les jugements sont à payer (≈ 2 €).

**Résultat `luna6-prompt`** (même référence, même juge, réponses retraduites comme dans l'app) :

| Concurrent | Gagnés / perdus / égalités | Fidélité (con / réf) | Exactitude (con / réf) | Inventions (con / réf) | Juge constant |
|---|---|---|---|---|---|
| Luna @ `u-all+v3+lang` | **19 / 16 / 5** | **2.71** / 2.39 | 2.61 / 2.63 | **0.63** / 1.59 | 88 % |
| Luna @ `u-all` (témoin) | 15 / 13 / 12 | 2.44 / 2.39 | 2.45 / 2.73 | 0.99 / 1.48 | 70 % |
| Luna @ `u-all+v3` | 14 / 17 / 9 | 2.61 / 2.41 | 2.56 / 2.71 | 0.74 / 1.55 | 78 % |

0.0036 € par question (Sonnet : 0.089 €), premier token 6.9 s (Sonnet : 6.0 s), ≈ 27k tokens d'entrée.

- **Retenu : GPT-6 Luna + `u-all` + `v3` + rappel de langue.** Seule version qui bat Sonnet 5.5, avec
  2,5 fois moins d'inventions et la même exactitude.
- `v3` seul réduit les inventions mais perd des duels ; c'est le rappel de langue qui fait la différence.
- Les demandes hors sujet sont maintenant refusées (`v3`). Le juge préfère encore souvent le refus de
  Sonnet, plus explicatif : c'est de la forme, pas une invention.
- Défaut corrigé dans le code le même jour : Luna refusait de donner l'URL du SharePoint OPEX écrite
  dans INAQ-742 (« je ne peux pas reproduire l'URL ici »), à cause de la consigne « Do not write
  URLs ». `v3` autorise désormais à citer une URL écrite dans un passage quand on demande un lien
  (`V3_CITATION_RULE`, `chat_vsi_prompts.py`).
- Restent des défauts de recherche, pas de modèle : confusion AIPS 02-01-003 / 01-02-003 (deux REF
  voisines dans le même plan), document cité absent des résultats (Q0062MI). Réponses de Luna plus
  courtes et parfois moins complètes que Sonnet (NDT, qualification peinture).
- Configuration mise dans `target_env.json` (DEV) : `operations_dev.md`, bloc L. **Aucun modèle Claude
  dans le chatbot** (décision du 2026-10-08) : la réécriture passe aussi sur GPT-6 Luna (elle coûtait
  plus que la réponse avec Sonnet 4.6) ; `u-all-luna6` dans `retrieval_eval` mesure ce que ça change.

---

## 3. Modèles

### 3.1 Où l'app utilise un modèle

| Rôle | Modèle actuel | Où le changer |
|---|---|---|
| Réponse du Chat VSI | Sonnet 4.6 | `CHAT_VSI_LLM_ENDPOINT` |
| Réécriture de la question avant recherche | Le même que la réponse | `CHAT_VSI_REWRITE_ENDPOINT` (nouveau) |
| Traduction des questions ni FR ni EN | GPT-5.6 Luna | `CHAT_TRANSLATE_ENDPOINT` |
| Reranking | `databricks_reranker` (Vector Search) | `chat_vsi_rerank.py` |
| Embedding de l'index | `databricks-qwen3-embedding-0-6b` | création de l'index (`5_Sync_Vector_Indexes.py`) |
| Description des images, OCR des scans | GPT-5.6 Luna | `PARSING_LLM_ENDPOINT` |
| Juge qualité (production, golden builder) | GPT-6 Luna | `judge_endpoint` des notebooks |

Prix connus, en dollars par million de tokens (entrée / sortie), relevés dans la console du
workspace et notés dans `server/services/streaming.py` :

| Modèle | Entrée | Sortie |
|---|---|---|
| Sonnet 4.6 | 3,63 | 18,17 |
| Sonnet 5.5 | 2,42 | 12,11 |
| GPT-5.4 mini | 1,82 | 10,90 |
| GPT-5 mini | 0,55 | 2,42 |
| Gemini 3.1 Flash-Lite | 0,55 | 3,27 |
| GPT-5.6 Luna | 0,24 | 2,18 |

Les prix de Haiku 4.5 et de GPT-6 Luna ne sont pas confirmés (voir P16 pour Haiku). La
commande `databricks serving-endpoints list --profile DEV` donne les modèles réellement
disponibles en DEV.

### 3.2 Testés

- **Sonnet 4.6** (réponse) : la référence. Environ 74 % de réponses correctes en moyenne sur
  deux runs en union, premier mot à 3 s, 0,088 € par question.
- **Sonnet 5.5** (réponse) : 81 % sur un seul run, même coût, premier mot à 8 s. Il réfléchit
  par défaut : il a fallu relever les plafonds (`CHAT_VSI_ANSWER_MAX_TOKENS`,
  `CHAT_VSI_REWRITE_MAX_TOKENS`) et retirer la température forcée. Prometteur, pas tranché.
- **KA** : 62 % de réponses correctes, le plus rapide (8,4 s en médiane). Il interroge le même
  index (`chunks_index_v1`), avec son propre reranking interne.
- **`databricks_reranker`** : utile seulement en union. Il ne lit que les 2 000 premiers
  caractères des colonnes qu'on lui donne (voir P6).
- **Qwen3-Embedding 0.6B** : utilisé, jamais comparé à un autre modèle.

### 3.3 Non testés, par rôle

**Réponse**
- **Comparaison côte à côte, plutôt qu'un run golden de plus.** On cherche les passages une
  seule fois et on les donne aux deux modèles. Un juge compare les deux réponses, dans les deux
  ordres. On ajoute une vérification d'invention : la réponse affirme-t-elle des choses
  absentes des passages ? Le tout sur 60 à 100 vraies questions DEV. C'est la façon fiable de
  départager Sonnet 4.6 et 5.5. Le notebook n'est pas encore écrit.
- **Sonnet 5.5 avec moins de réflexion** : c'est le moyen le plus direct de ramener le premier
  mot vers 3 s. Il faut vérifier quels paramètres l'endpoint Databricks accepte.
- **GPT-5.6 Luna** : 5 à 10 fois moins cher que Sonnet 5.5. Il est déjà retenu par
  l'équipe comme juge de l'impact search. Le risque porte sur le respect des citations `[n]` et
  sur le français des réponses longues.
- **Haiku 4.5** : plus rapide et environ 2,5 fois moins cher que Sonnet 5.5, au prix public de
  1 / 5 $. Même famille, donc la règle de citation devrait tenir. Qualité à mesurer.
- **GPT-6 Luna** : c'est le juge de l'équipe. Si on le teste comme rédacteur, il faut changer
  de juge, sinon il se note lui-même.
- **Gemini 3.1 Flash-Lite** : à écarter. Il a produit des faux positifs reproductibles sur
  l'impact search.

**Réécriture de la question**
- Aujourd'hui, c'est le modèle de réponse qui réécrit la question. Or la réécriture est sur le
  chemin critique : elle passe avant la recherche, donc avant le premier mot.
- **GPT-5.6 Luna** est prêt à mesurer (`u-bi-luna`). C'est un modèle qui réfléchit : il lui
  faut un plafond de 2 000 tokens, sinon la réécriture revient vide.
- **Haiku 4.5** : même essai, si l'endpoint existe en DEV.
- `retrieval_eval` est le bon outil pour ça : il mesure directement l'effet de la réécriture
  sur les documents trouvés, sans générer de réponse.

**Reranking**
- **Reranker par LLM** : un modèle reclasse les 30 meilleurs passages. D'après les chiffres de
  Databricks, Sonnet 4.5 en reranker est presque au niveau du modèle interne du KA (nDCG@10
  80,1 contre 81,0). Mais c'est 1 à 3 s et quelques centimes de plus par question. À garder
  pour plus tard, si la vague 3 laisse encore des écarts.
- **Reranker ré-entraîné sur nos données** : Databricks permet d'affiner son reranker. Il faut
  des paires question / passage pertinent, que le golden, les retours et les synthétiques
  commencent à fournir. C'est un projet à part.

**Embedding**
- **Consigne de requête pour Qwen** : Qwen3-Embedding recommande de préfixer les questions
  (`Instruct: … Query: …`), pas les documents. Sans consigne, Qwen annonce environ 1 à 5 % de
  perte. Vector Search envoie la question telle quelle. Pour corriger, il faudrait calculer
  nous-mêmes le vecteur de la question et l'envoyer avec le texte (`query_vector` +
  `query_text`). Il faut d'abord vérifier par un essai que l'API l'accepte sur un index à
  embedding géré.
- **Un plus gros modèle d'embedding** (Qwen3-Embedding 4B ou 8B, bge-m3,
  multilingual-e5-large-instruct) : il faudrait le servir nous-mêmes sur GPU (coût fixe) et
  refaire l'index.
- **`gte-large-en` est à écarter** : il ne gère que l'anglais.
- La taille de passage n'est pas limitée par l'embedding : Qwen accepte 32 000 tokens.

**Description des images**
- GPT-5.6 Luna, avec un prompt qui interdit de répéter le texte autour de l'image. Voir P7
  pour ce que ça coûte à la recherche.
- Redécrire avec un modèle plus fort (Sonnet 5.5) coûterait environ 10 fois plus, pour un gain
  inconnu. À tester sur un échantillon (`utils/parsing_sandbox/Test_Prompt.py`) avant tout
  passage à l'échelle.

---

## 4. Recherche côté app : pistes non testées

Toutes se font dans `chat_vsi_rerank.py`, derrière un réglage désactivé par défaut, comme les
précédentes.

| # | Piste | Gain attendu | Coût | Comment le mesurer |
|---|---|---|---|---|
| R1 | **Passages voisins** : ajouter le passage avant et après les meilleurs résultats | Tableaux et procédures coupés en deux, slides | +20 à 40 % de contexte | Golden ou côte à côte (même document, donc invisible pour `retrieval_eval`) |
| R2 | **Ordre du document** : passages d'un même document triés par `chunk_index`, pas par rang | Lecture plus cohérente | Nul | Côte à côte |
| R3 | **Métadonnées une seule fois par document** : retirer le `[Source: …]` répété à chaque passage et le mettre en tête du document | Environ 1 500 tokens en moins par question (5 à 7 %) | Nul | Coût mesuré, qualité au côte à côte |
| R4 | **Reranker sur le texte seul**, sans les titres de section | Le reranker lit plus de contenu dans ses 2 000 caractères | Nul | `retrieval_eval` |
| R5 | **Consigne de requête Qwen** (§ 3.3) | 1 à 5 % de rappel selon Qwen | Un appel d'embedding par requête | `retrieval_eval`, après un essai d'API |
| R6 | **Recherche par mots-clés** : une requête courte avec les codes et acronymes seuls | Codes (EN 4179, R80, 651), acronymes | Une requête de plus | `retrieval_eval` |
| R7 | **Filtre par type de document** quand la question le dit (« procédure », « formulaire », « template ») | Moins de bruit | Demande la colonne `type_document` dans l'index (P10) | `retrieval_eval` |
| R8 | **Suivi de conversation** : réutiliser les documents du tour précédent pour « et pour IS ? » | Questions de suivi | Faible | Golden (questions multi-tours) |
| R9 | **Seuil « je ne sais pas »** : si aucun passage n'est vraiment pertinent, le dire | Moins d'inventions (slide 15, mouvement 651) | Faible | Côte à côte avec vérification d'invention |
| R10 | **Langue de la question** : parmi les versions d'un document, garder celle dans la langue de la question | Réponses plus fidèles au texte cité | Demande la colonne `langue` (P10) | Côte à côte |
| R11 | **Reranker par LLM** (§ 3.3) | Précision | 1 à 3 s, quelques centimes | `retrieval_eval` |
| R12 | **Nombre de passages adaptatif** : couper sous un score de reranking | Moins de contexte inutile | Nul | `retrieval_eval`, `recall_top5_pct` |

Déjà fait dans cette série : les relances de recherche (`CHAT_VSI_SEARCH_RETRIES`, sans elles
un utilisateur aurait eu « Document search failed » sous charge) et le modèle de réécriture
séparé.

---

## 5. Audit du pipeline de parsing

### 5.1 Ce que fait le pipeline aujourd'hui

- **Parsing** (`3_Parse_Pipeline.py`, GPU) : Docling convertit chaque document en markdown.
  Les tableurs passent par openpyxl et deviennent des lignes « colonne : valeur ». Les `.doc`,
  `.rtf` et `.odt` ont leurs convertisseurs. Le texte parsé de chaque document est gardé dans
  `_pipeline_checkpoint` (colonne `document_text`).
- **Découpage** (`utils.py`, `chunk_document`) :
  - le markdown est coupé sur les titres `#`, `##` et `###`, puis par paragraphe ;
  - les paragraphes sont fusionnés jusqu'à environ 500 tokens (250 au minimum, 1 000 au
    maximum, comptés avec le tokenizer `cl100k` d'OpenAI) ;
  - chaque passage est préfixé par `[Source: REF | Title | Division | Category | Date]` ;
  - au plus 100 passages par tableur.
- **Images** (`4_Describe_Images_LLM.py`) : chaque image retenue est décrite par GPT-5.6
  Luna et devient un passage à part. Les pages scannées sont retranscrites de la même façon.
- **Index** : `chunks` → `chunks_index` (Delta Sync, embedding Qwen3 0.6B géré par
  Databricks, recherche hybride). En DEV : `chunks_v1`, 75 086 passages.

### 5.2 Constats et propositions

#### P1 🟠 Le découpeur Docling n'est jamais utilisé

- **Problème.** `chunk_document` essaie d'abord le `HybridChunker` de Docling, qui découpe
  selon la structure réelle du document (titres, tableaux, listes). Mais `build_chunks_udf`
  l'appelle toujours avec `docling_doc=None` (`utils.py:1113`) : le document Docling n'existe
  plus à ce stade, seul le markdown est gardé. **Tout le corpus passe donc par le découpeur de
  secours**, qui travaille sur le markdown. Le `HybridChunker` et son tokenizer
  (`all-MiniLM-L6-v2`, un tokenizer anglais, `utils.py:333`) sont du code mort dans le
  pipeline. Le notebook `utils/parsing_sandbox/Test_Chunking.py` les compare, mais sur 3
  documents.
- **Proposition.** Garder le découpeur markdown, plus simple et sans GPU, et le corriger
  (P2 à P5). Supprimer ou documenter le chemin mort pour qu'on ne croie plus qu'il sert.

#### P2 🔴 Les titres de section enregistrés sont faux dès que deux sections sont fusionnées

- **Problème.** Quand deux morceaux sont fusionnés, `_merge_meta` (`utils.py:1019`) mélange
  leurs titres (`{**a, **b}`). Le titre de niveau 3 de la première section survit à côté du
  titre de niveau 2 de la seconde.
- **Preuve** (test local du découpeur, sur une procédure de 5 sections) : le seul passage
  produit porte `{"Header 2": "5. Acuité visuelle", "Header 3": "3.2 Responsable de site"}`.
  Cette lignée n'existe pas : la section 5 n'a pas de sous-section 3.2.
- **Impact.**
  - `semantic_headers` sert d'indice de section au juge de l'impact search.
  - Le reranker le lit avec `CHAT_VSI_RERANK_COLUMNS=REF,semantic_headers,chunk_text`.
  - Sur un passage fusionné, l'indice de section est donc faux.
- **Proposition.** Garder dans `semantic_headers` les titres du **premier** morceau du passage,
  ou la liste des sections couvertes. Pas un mélange.

#### P3 🟠 Un passage peut mélanger deux sections

- **Problème.** `merge_small_chunks` fusionne vers 500 tokens sans regarder les titres.
  Dans le test, les sections 1 et 2 forment un seul passage de 993 tokens, étiqueté
  « 2. Section 2 » uniquement. Un passage qui couvre deux sujets donne un vecteur moyen, moins
  net pour la recherche.
- **Proposition.** Ne jamais fusionner par-dessus un titre de niveau 1 ou 2. On accepte des
  passages plus petits en fin de section : le minimum de 250 tokens ne s'applique alors qu'à
  l'intérieur d'une section.

#### P4 🟠 Le chevauchement de 12 % ne s'applique presque jamais

- **Problème.** `CHUNK_OVERLAP_RATIO = 0.12` (`config.py:216`) n'est utilisé que par le
  découpeur de caractères, qui ne sert qu'aux paragraphes de plus de 500 tokens. Entre deux
  passages fusionnés, aucun chevauchement. Le test le confirme : aucun passage ne reprend la
  fin du précédent. Le commentaire de la config dit que le réglage ne s'applique « qu'au
  prochain re-découpage », mais même un re-découpage ne l'appliquerait pas.
- **Impact.** Une information à cheval sur deux passages (une étape et sa condition, une
  ligne de tableau et son en-tête) n'est complète dans aucun des deux.
- **Proposition.** Reprendre le dernier paragraphe, ou les dernières lignes, du passage
  précédent, à l'intérieur d'une même section. Côté app, les passages voisins (R1) traitent le
  même problème sans re-découper.

#### P5 🟠 Le titre est répété dans chaque passage, parfois deux fois

- **Problème.** Le préfixe de section `[Titre > Section]` est ajouté à **chaque paragraphe**,
  avant la fusion (`utils.py:985`). Un passage de 5 paragraphes le contient donc 5 fois. Dans
  le test, le titre du document apparaît 7 fois dans un même passage. Quand le titre de
  niveau 1 est le titre du document, il apparaît en plus dans `[Source: … | Title: …]`.
- **Impact.** Des tokens perdus pour le modèle. Des mots répétés qui pèsent dans la partie
  mots-clés de la recherche hybride. Moins de vrai contenu dans les 2 000 caractères du
  reranker.
- **Proposition.** Un seul préfixe de section par passage, en tête. Garder `[Source: …]`, qui
  porte la REF, le titre et la date : il a été choisi volontairement (`EMBED_SOURCE_PREFIX`).
  Ce choix a été validé sur 3 documents dans `Test_Chunking.py`, il peut maintenant être
  remesuré avec `retrieval_eval`.

#### P6 🟠 Le reranker ne lit que le début des passages longs

- **Problème.** Le reranker de Vector Search tronque le texte de ses colonnes à 2 000
  caractères. Or :
  - nos passages font 1 562 caractères en médiane et 3 410 au 90ᵉ centile ;
  - avec `ctx`, la REF et le JSON des titres passent devant ;
  - le préfixe `[Source: …]` (150 à 250 caractères) prend aussi de la place.
  Le reranker juge donc environ 1 500 caractères de contenu. Pour une part importante des
  passages (la requête Q1 donne le chiffre exact), la fin ne compte pas.
- **Proposition.** C'est le seul argument concret pour **raccourcir** les passages : viser
  1 200 à 1 500 caractères de contenu, environ 300 à 400 tokens. À tester (§ 5.4). Sans
  re-découper, R4 (reranker sur le texte seul) gagne déjà quelques centaines de caractères.

#### P7 🟠 Les descriptions d'images : la moitié de l'index, et mal placées

- **Constat.** D'après le README du pipeline, il y avait environ 38 700 passages d'images en
  août, pour 75 086 passages dans `chunks_v1`. **Environ la moitié de l'index serait donc des
  descriptions générées par LLM** (Q1 le confirme).
- **Problèmes :**
  1. **La légende n'est pas dans le texte indexé.** La légende de la figure (« Figure 3 –
     Logigramme de traitement des NC ») est rangée dans `semantic_headers`, pas dans
     `chunk_text` (`4_Describe_Images_LLM.py:393`). Or c'est souvent la meilleure
     description de l'image pour la recherche.
  2. **Le prompt interdit de reprendre le texte autour de l'image.** La description ne dit
     donc pas de quoi parle la section. Le passage d'image est rangé après tout le texte
     (`chunk_index` = dernier passage + 1 + numéro d'image), sans titre de section.
  3. **Les pages scannées ne sont pas découpées.** Elles sont retranscrites jusqu'à 8 192
     tokens de sortie (`LLM_OCR_MAX_TOKENS`) et indexées en un seul passage, dont le vecteur
     moyenne une page entière. C'est probablement l'origine du maximum observé de 20 848
     caractères (Q1 le dira).
  4. **Les images de moins de 5 % de la page ne sont pas décrites** (`MIN_AREA_RATIO`). Un
     petit logigramme peut y passer. À vérifier sur un échantillon.
- **Propositions :**
  - mettre la légende et le titre de section le plus proche dans le texte du passage d'image.
    C'est sans LLM : la légende est déjà stockée, le texte autour aussi (`context_text`) ;
  - découper les retranscriptions longues avec le même découpeur que le texte ;
  - mesurer la part d'images dans ce que la recherche renvoie, en enregistrant
    `chunk_content_type` dans `retrieval_eval`. Si les images prennent trop de place, en
    limiter le nombre par question.

#### P8 🟡 Les tokens sont comptés avec le tokenizer d'OpenAI

- **Problème.** Les tailles (250, 500 et 1 000 tokens) sont comptées avec `cl100k`, le
  tokenizer de GPT-4. Ni l'embedding (Qwen) ni les modèles de réponse (Claude) ne l'utilisent.
  Il est en outre probablement très économe sur les répétitions : une ligne de points de table
  des matières ou des soulignés de formulaire font beaucoup de caractères pour peu de tokens.
  Un passage peut alors rester « sous 1 000 tokens » avec un texte très long.
- **Proposition.** Fixer les limites en **caractères**, ce que voient réellement le reranker et
  le coût du modèle, ou au moins plafonner les deux. La requête Q3 compte les passages où le
  rapport caractères / tokens est anormal.

#### P9 🟠 Tables des matières, cartouches et textes répétés dans l'index

- **Problème.**
  - Les tables des matières sont riches en mots-clés et vides de contenu. Le reranker en a
    choisi une à la place d'une définition (glossaire APO, § 2.1).
  - Il en va de même pour les cartouches de première page (« Rédigé par / Vérifié par /
    Approuvé par »), les tableaux d'historique des révisions et les mentions répétées dans des
    centaines de documents (propriété, confidentialité).
  - Ces passages remontent sur beaucoup de questions et prennent la place de vrais passages.
- **Proposition.**
  - Repérer ces passages au découpage : lignes à points de conduite suivies d'un numéro de
    page, mots « Sommaire » ou « Table des matières », paragraphes identiques dans plus de
    20 documents.
  - Les **marquer** (`chunk_content_type = 'toc'`, `'front_matter'`, `'boilerplate'`) plutôt
    que les supprimer. La recherche les filtre alors (`filters_json`), sauf si la question
    porte sur une révision ou une approbation.
  - Les requêtes Q3 et Q4 mesurent le volume avant de décider.

#### P10 🟠 L'index n'a pas les métadonnées dont la recherche aurait besoin

- **Problème.** Le pipeline connaît, pour chaque document : `type_document` (« 05 - Procédure -
  QP »…), `indice` (la révision), `niveau_plus_2` (le sous-processus), `titre` et `auteur`. Mais
  la table de passages ne garde que `REF`, `division`, `url` et `doc_date`
  (`3_Parse_Pipeline.py:946`).
- **Deux défauts en plus :**
  - `langue` est écrite en dur à `fr-FR` pour **tous** les documents (`selection.py:426`) :
    elle ne sert à rien aujourd'hui ;
  - `columns_to_sync` n'est pas fixé sur l'index (`docs/ka-migration.md`), donc toutes les
    colonnes sont synchronisées. Qu'elles soient toutes filtrables n'a pas été vérifié.
- **Proposition.**
  - Ajouter `type_document`, `indice`, `titre`, `niveau_plus_2` et une vraie `langue` à la
    table de passages. La langue se tire du suffixe de la REF, ou d'une détection sur le texte
    avec le modèle fastText déjà présent dans `server/data/`.
  - Mettre aussi « Type : Procédure » dans le préfixe `[Source: …]`.
  - Ça permet de filtrer par type (R7), de préférer la langue de la question (au lieu de
    « la mieux classée »), et d'afficher l'indice dans les sources.
- **Coût.** Pas de LLM ni de GPU. Le préfixe change, donc tout l'index est ré-embeddé.

#### P11 🟠 Les documents publiés avant 2018 sont invisibles pour le chatbot

- **Problème.** Environ 1 500 documents toujours en vigueur (`courant=1`) mais publiés avant
  2018 ne sont pas dans l'index du chatbot (`DOC_DATE_CUTOFF`). Une question sur l'un d'eux ne
  peut rien trouver. Les fiches d'identification (`parsing_archive_notices_in_rag`) sont
  prêtes mais désactivées.
- **Proposition.** Mesurer d'abord combien de documents attendus par l'évaluation sont dans ce
  cas (requête Q2). Si c'en est une part notable, activer les fiches d'identification, ou
  indexer ces documents avec un avertissement de date. C'est une décision métier.

#### P12 🟡 Tableurs : en-têtes devinés, plafond de 100 passages, indicateur faux

- **Problèmes :**
  - la première ligne non vide est prise comme en-tête (`utils.py:850`). Sur un tableur qui
    commence par un titre ou un logo, chaque valeur devient « col3 : … » et l'en-tête réel
    est perdu ;
  - au-delà de 100 passages, le reste du tableur n'est pas indexé ;
  - `chunks_truncated` vaut `true` pour **tous** les tableurs, tronqués ou non
    (`3_Parse_Pipeline.py:1060`).
- **Proposition.**
  - Choisir comme en-tête la première ligne qui remplit la plupart des colonnes.
  - Mesurer combien de tableurs touchent le plafond (Q6) avant de le relever.
  - Calculer l'indicateur à partir du nombre réel de passages.

#### P13 🟡 Pas de numéro de page ni de slide

- **Problème.** Le markdown exporté ne garde ni les numéros de page des PDF ni les numéros de
  slide des PPTX : Docling les connaît, mais ils sont perdus à l'export. « Résume la slide
  15 » ne peut donc pas viser la bonne slide, et les sources ne peuvent pas citer de page. Les
  images, elles, gardent leur page.
- **Proposition.** Faire écrire un marqueur de saut de page dans le markdown au parsing
  (Docling le propose à l'export, option `page_break_placeholder`, à vérifier dans la version
  installée), puis reporter le numéro de page dans les métadonnées de chaque passage.
- **Coût.** **Il faut re-parser les documents sur GPU**, donc à réserver à un besoin confirmé
  (PPTX, citations par page). On peut aussi le faire progressivement : les nouveaux documents
  et les révisions d'abord.

#### P14 🟡 Questions sur la fraîcheur et la couverture

- Les documents en `ERROR`, `EMPTY_TEXT` ou `SKIPPED_*` ne sont pas dans l'index : la requête
  Q7 les compte.
- L'index se synchronise en `TRIGGERED` à la fin de la chaîne quotidienne, qui est en pause
  sur DEV. C'est normal pour l'évaluation, mais à savoir quand on compare à l'UAT.

#### P15 🟡 Le choix du préfixe `[Source: …]` n'a jamais été mesuré sur la recherche

- Le commentaire de `config.py:250` dit que l'enlever « a été mesuré » comme nuisible. La
  seule trace est `Test_Chunking.py` : 3 documents, une similarité entre passages, une
  question. Ce n'est pas une mesure de recherche. À refaire avec `retrieval_eval` sur un index
  de test (§ 5.5), en même temps que P5 et P10, qui changent ce préfixe.

#### P16 🟡 Hors parsing, vu en passant

- **Coût de Haiku sous-estimé.** `streaming.py` déclare deux fois le prix de Haiku 4.5
  (lignes 19 et 26). La seconde entrée (0,8 / 4 $) écrase la première (1 / 5 $) : les coûts
  Haiku affichés sont sous-estimés d'environ 20 %, si le vrai tarif est bien celui de la
  ligne 19. À vérifier dans la console.
- **Colonnes de recherche.** La recherche ne demande pas `chunk_index` ni
  `chunk_content_type` (`vector_search.py:28`). Il les faut pour R1, R2 et la mesure de la
  part d'images (P7).

### 5.3 Enrichir l'index

Ces propositions **ajoutent** du contenu à l'index sans rien enlever. Les coûts sont estimés
au tarif de GPT-5.6 Luna (§ 3.1).

| # | Proposition | Pour quelles questions | Coût estimé |
|---|---|---|---|
| E1 | **Fiche par document** : un passage de synthèse par document (objet, domaine d'application, sujets, rôles, documents cités), généré par LLM. Le mécanisme existe déjà pour les fiches d'archive | « Quel document traite de… », « trouve-moi le template de… » | ≈ 15 € pour environ 6 000 documents |
| E2 | **Contexte par passage** : une ou deux phrases générées en tête de chaque passage pour le situer (« Ce passage de QP-1518 décrit l'examen pratique du Level 2 ») | Toutes : sur ses jeux de test, Anthropic réduit les échecs de recherche de 35 % avec cette méthode, et de 49 % en l'appliquant aussi à la recherche par mots-clés | ≈ 50 € pour les ~37 000 passages de texte |
| E3 | **Légendes et section dans les passages d'image** (P7) | Logigrammes, tableaux en image | 0 € (données déjà stockées) |
| E4 | **Glossaire des acronymes** tiré des glossaires du corpus (IN_APO_0006…), donné à la réécriture | Acronymes maison (APO, CMP, DANAFF, R80) | Faible |
| E5 | **Questions hypothétiques** : les questions auxquelles chaque passage répond, indexées en plus | Questions courtes ou mal formulées | ≈ 20 €, mais l'index double de taille : à garder pour plus tard |

Hypothèses de coût :
- E1 : environ 6 000 documents (Q1 donne le nombre exact), 6 000 tokens lus et 300 écrits par
  document ;
- E2 : chaque appel lit environ 5 000 tokens du document et écrit 80 tokens. Le coût monte
  avec la longueur des documents ;
- les prix sont ceux de GPT-5.6 Luna, sans cache de prompt.

E1 et E2 sont les deux propositions les plus prometteuses pour notre corpus :
- les titres sont courts et souvent génériques, et un passage isolé dit rarement de quel
  processus il parle ;
- les deux se font sans GPU, à partir du texte déjà parsé ;
- E2 se fait d'abord sur les documents les plus consultés, pour vérifier le gain avant de
  l'étendre.

### 5.4 Faut-il changer la taille des passages ?

**Pas en premier.** Ce qui est prouvé, ce sont des défauts de **structure** : titres faux,
sections mélangées, pas de chevauchement, titres répétés. On les corrige sans changer la
taille. Le seul argument concret pour raccourcir est la fenêtre de 2 000 caractères du
reranker (P6). En face, des passages plus courts coupent plus souvent une procédure ou un
tableau, ce qui demande les passages voisins (R1) pour garder le contexte.

Il faut donc **mesurer** trois découpages sur le même texte :
- **v2a** : même taille (250 / 500 / 1 000), avec les corrections P2 à P5 et P9. Mesure l'effet
  de la structure seule ;
- **v2b** : passages plus courts (environ 150 / 300 / 450 tokens, soit 500 à 1 600 caractères),
  avec les mêmes corrections. À évaluer avec les passages voisins activés ;
- **v1** : l'index actuel, comme référence.

On choisit sur `retrieval_eval` (documents trouvés, `recall_top5_pct`, taille du contexte),
puis on confirme la meilleure par un run golden ou une comparaison côte à côte.

### 5.5 Tester sans toucher à l'UAT

Le DEV a déjà tout ce qu'il faut, copié de l'UAT par `copy_uat_to_dev` :
`_pipeline_checkpoint_v1` (le texte parsé de chaque document), `processed_files_v1`,
`image_metadata_v1`, `parse_manifest_v1`.

1. **Un notebook DEV de re-découpage**, à écrire :
   - il lit le texte parsé dans `_pipeline_checkpoint_v1`, pour les documents actuellement
     dans `chunks_v1` ;
   - il applique un découpage paramétré (v2a, v2b) et reprend les passages d'image, enrichis
     ou non (E3) ;
   - il écrit `chunks_v2a`, `chunks_v2b`…
   - Pas de GPU, pas de LLM, sauf pour E1 et E2.
2. **Un index par variante** sur l'endpoint Vector Search DEV. Le seul coût est l'embedding,
   environ 35 millions de tokens par variante, prix à vérifier dans la console.
3. **`retrieval_eval` avec une config par index**. L'app lit l'index à chaque appel
   (`CHAT_VSI_INDEX_ALL`), donc une config suffit :
   `{'CHAT_VSI_INDEX_ALL': 'dev_landingzone.qualibot.chunks_index_v2a', ...}`.
   Il faut forcer la division `ALL` pour toutes les questions, sinon les questions AS et IS
   resteraient sur les anciens index : un widget à ajouter.
4. **Seulement ensuite**, porter le découpage retenu dans `utils.py`.
   - En UAT, un run `full` re-découpe tout depuis le checkpoint : les fichiers déjà parsés sont
     sautés (même chemin, même empreinte), donc rien n'est re-parsé sur GPU.
   - Il réécrit `chunks` et `processed_files`, puis l'index se ré-embedde entièrement.
   - Le KA de l'UAT lit le même index : c'est à faire avec ton accord.
   - `qualibot-uat-test` n'a pas de pipeline de parsing, et le DEV ne se recharge que par
     copie de l'UAT (`copy_uat_to_dev`).

---

### 5.6 Résultat du test DEV (2026-10-08) : v2b retenu

`retrieval_eval`, 65 questions, toutes sur l'index ALL de chaque variante. `-all` = recherche `u-all`
(celle du chat) ; sans suffixe = `union-ctx` ; `-clean` = `union-ctx` sans sommaires, cartouches ni
textes répétés (`CHAT_VSI_SKIP_NOISE`).

| Config | Documents attendus trouvés | Dans les 5 premiers | Au moins un | Contexte (tokens) |
|---|---|---|---|---|
| **`idx-v2b-all`** | **80.9 %** | **68.1 %** | 83.1 % | **11 038** |
| `u-all-luna6` (index actuel, réécriture GPT-6 Luna) | 80.0 % | 65.8 % | 81.5 % | 20 889 |
| `idx-v2a-all` | 79.5 % | 62.9 % | 81.5 % | 20 435 |
| `idx-v1-all` (index actuel) | 75.8 % | 61.2 % | 76.9 % | 21 105 |
| `idx-v2b` | 69.8 % | 61.9 % | 73.8 % | 7 861 |
| `idx-v2a-clean` | 69.1 % | 60.3 % | 72.3 % | 13 814 |
| `idx-v1` | 68.5 % | 57.6 % | 70.8 % | 15 000 |
| `idx-v2a` | 67.9 % | 62.4 % | 70.8 % | 14 185 |
| `idx-v2b-clean` | 58.2 % | 47.7 % | 64.6 % | 8 003 |

- **v2b (150 / 300 / 450 tokens, 1 600 caractères) est retenu** : +5 points sur l'index actuel, +7 points
  dans les 5 premiers, avec **deux fois moins de contexte** (11k tokens au lieu de 21k : réponse moins
  chère et plus rapide). Gain net sur le golden (96.1 % contre 91.7 %) et sur les retours utilisateurs
  (43.8 % contre 25 %, soit 3 questions sur 16) ; égalité sur les questions synthétiques, qui ont été
  tirées des passages actuels et favorisent donc l'ancien découpage. Les échantillons sont petits :
  la vérification sur les réponses (`pairwise_answers`, `luna6-v2b`) reste à faire.
- **v2a** (même taille, découpage corrigé) gagne 4 points à contexte égal : les corrections de découpage
  comptent, la taille aussi.
- **Écarter le « bruit » fait perdre** (−11 points sur v2b) : des passages marqués sommaire, cartouche ou
  texte répété contiennent des réponses. `CHAT_VSI_SKIP_NOISE` reste désactivé ; le marquage reste dans
  les tables comme simple information.
- **Réécriture par GPT-6 Luna** : `u-all-luna6` = 80.0 %, comme l'ancien `u-all` réécrit par Sonnet 4.6
  (80 %, même index, même routage) : aucune perte, la réécriture reste sur GPT-6 Luna.
- Fait dans le code : tailles v2b par défaut dans `utils/parsing_pipeline/config.py`. Le corpus UAT est
  re-découpé seulement avec l'accord de l'utilisateur (`OPERATIONS.md`, D5).
- **Vérifications du même jour (réécriture GPT-6 Luna partout)** :
  - second run : `idx-v2b-all` 83.6 % contre 78.5 % pour `idx-v1-all` (+5 points, comme au premier run),
    11k tokens contre 21k ;
  - `idx-v2b-all-k20` (20 passages reclassés) : 80.5 %, **moins bien** que 12 passages (83.6 %) avec plus
    de contexte → on garde `CHAT_VSI_RERANK_TOP_K` par défaut (12) ;
  - réponses (`pairwise_answers`, `luna6-v2b`, GPT-6 Luna des deux côtés, seul l'index change) :
    10 gagnés / 10 perdus / 20 égalités, exactitude 2.81 contre 2.61, fidélité 2.81 contre 2.73,
    inventions 0.40 contre 0.53 ; **0.0022 € par question contre 0.0036 €** (−39 %), premier token
    5.9 s contre 8.1 s. Juge constant 53 % seulement : les réponses sont proches, l'écart en duels n'est
    pas significatif ; les notes vont toutes dans le sens de v2b.
  - Exemples : v2b trouve NF-10065 (template CMP) et ŘLCZ-05/15 (příkaz ředitele sur le tabac) que v1
    ratait ; v1 trouve la définition d'APO dans le glossaire que v2b rate.
- **Conclusion : v2b validé** (recherche +5 points, réponses au moins aussi bonnes, −39 % de coût,
  −2 s au premier token). Prochaine étape : re-découpage UAT (`OPERATIONS.md`, D5), avec accord.

## 6. L'évaluation elle-même

- **Agrandir le golden à 60–80 questions.** `utils/evaluation/Build_Golden_Dataset.py` le fait
  déjà à partir des vrais logs : il suffit de relever son quota. Il faut aussi corriger les
  cas douteux : REP_OUT (présent dans SF-1271), slide 15 (est-elle indexée ?), mouvement 651.
- **Plus de retours négatifs avec document attendu.** C'est le jeu le plus réaliste, et il ne
  compte que 16 questions. Chaque 👎 commenté devrait y entrer.
- **Séparer « document absent de l'index » et « document raté par la recherche »** (requête
  Q2). Aucun réglage de recherche ne retrouve un document qui n'est pas indexé.
- **Enregistrer le type des passages renvoyés** (texte, tableau, image) dans `retrieval_eval`,
  pour mesurer P7.
- **Comparer les modèles côte à côte**, et pas avec plus de runs golden (§ 3.3).

---

## 7. Requêtes de diagnostic

À lancer dans l'éditeur SQL du workspace DEV. Elles lisent les copies `_v1` et la table
d'évaluation, et n'écrivent rien.

**Q1 — Composition de l'index : types de passages et tailles**
```sql
SELECT chunk_content_type,
       count(*) AS passages, count(DISTINCT IDDOC) AS documents,
       percentile(length(chunk_text), 0.5) AS p50_chars,
       percentile(length(chunk_text), 0.9) AS p90_chars,
       max(length(chunk_text)) AS max_chars,
       round(avg(CASE WHEN length(chunk_text) > 2000 THEN 1 ELSE 0 END) * 100, 1) AS pct_over_2000_chars
FROM dev_landingzone.qualibot.chunks_v1
GROUP BY ALL ORDER BY passages DESC;
```

**Q2 — Documents attendus par l'évaluation mais absents de l'index**
```sql
WITH expected AS (
  SELECT DISTINCT source, doc
  FROM dev_landingzone.qualibot.eval_retrieval_runs LATERAL VIEW explode(expected) t AS doc
  WHERE config = 'baseline'),
indexed AS (
  SELECT DISTINCT regexp_replace(regexp_replace(upper(REF), '[-_. ]+(FR|EN|GB|MX|BG|CZ|BR|ES)$', ''), '[^A-Z0-9]', '') AS doc
  FROM dev_landingzone.qualibot.chunks_v1),
files AS (
  SELECT regexp_replace(regexp_replace(upper(ref), '[-_. ]+(FR|EN|GB|MX|BG|CZ|BR|ES)$', ''), '[^A-Z0-9]', '') AS doc,
         max(parse_status) AS parse_status, max(CAST(filtered_by_date AS INT)) AS before_2018, max(doc_date) AS doc_date
  FROM dev_landingzone.qualibot.processed_files_v1 GROUP BY 1)
SELECT e.source, e.doc, f.parse_status, f.before_2018, f.doc_date
FROM expected e
LEFT JOIN indexed i ON e.doc = i.doc
LEFT JOIN files f ON e.doc = f.doc
WHERE i.doc IS NULL
ORDER BY e.source, e.doc;
```
Une ligne sans `parse_status` signale un document inconnu du pipeline : hors périmètre, REF
mal saisie dans le retour, ou fichier absent du volume.

**Q3 — Tables des matières et passages anormalement longs pour leur nombre de tokens**
```sql
SELECT chunk_content_type,
       count_if(size(regexp_extract_all(chunk_text, '\\.{5,}\\s*\\d+', 0)) >= 3) AS toc_like,
       count_if(length(chunk_text) / greatest(chunk_token_count, 1) > 8) AS many_chars_per_token,
       count(*) AS passages
FROM dev_landingzone.qualibot.chunks_v1
GROUP BY ALL;
```

**Q4 — Paragraphes répétés dans de nombreux documents (mentions, cartouches)**
```sql
WITH paras AS (
  SELECT IDDOC, trim(p) AS para
  FROM dev_landingzone.qualibot.chunks_v1
  LATERAL VIEW explode(split(regexp_replace(chunk_text, '^\\[Source:[^\\]]*\\]\\s*', ''), '\n\n')) t AS p
  WHERE chunk_content_type <> 'image')
SELECT left(para, 140) AS paragraph, count(DISTINCT IDDOC) AS documents
FROM paras WHERE length(para) >= 80
GROUP BY para HAVING documents >= 20
ORDER BY documents DESC LIMIT 30;
```

**Q5 — Profondeur des titres de section, par format**
```sql
SELECT p.source_file_extension,
       CASE WHEN c.semantic_headers LIKE '%Header 3%' THEN 3
            WHEN c.semantic_headers LIKE '%Header 2%' THEN 2
            WHEN c.semantic_headers LIKE '%Header 1%' THEN 1 ELSE 0 END AS heading_depth,
       count(*) AS passages
FROM dev_landingzone.qualibot.chunks_v1 c
JOIN (SELECT DISTINCT IDDOC, source_file_extension FROM dev_landingzone.qualibot.processed_files_v1) p USING (IDDOC)
WHERE c.chunk_content_type <> 'image'
GROUP BY ALL ORDER BY 1, 2;
```

**Q6 — Tableurs qui touchent le plafond de 100 passages**
```sql
SELECT count(*) AS spreadsheets, count_if(n >= 100) AS at_cap
FROM (SELECT c.IDDOC, count(*) AS n
      FROM dev_landingzone.qualibot.chunks_v1 c
      JOIN (SELECT DISTINCT IDDOC, source_file_extension FROM dev_landingzone.qualibot.processed_files_v1) p USING (IDDOC)
      WHERE lower(p.source_file_extension) IN ('xlsx', 'xls', 'xlsm') AND c.chunk_content_type <> 'image'
      GROUP BY c.IDDOC);
```

**Q7 — Couverture du corpus : statut de chaque document**
```sql
SELECT parse_status, filtered_by_date, include_in_rag, count(*) AS documents
FROM dev_landingzone.qualibot.processed_files_v1
GROUP BY ALL ORDER BY documents DESC;
```

**Q8 — Catégories des descriptions d'images**
```sql
SELECT coalesce(nullif(regexp_extract(description, '^#\\s*\\[([A-Z_]+)\\]', 1), ''), label) AS category,
       count(*) AS images, round(avg(length(description))) AS avg_chars
FROM dev_landingzone.qualibot.image_metadata_v1
WHERE status = 'DONE'
GROUP BY ALL ORDER BY images DESC;
```

---

## 8. Ordre proposé

| Étape | Quoi | Effort | Coût |
|---|---|---|---|
| 1 | Vague 3 de `retrieval_eval` (§ 2.3) et requêtes Q1 à Q8 | Lancer | ≈ 1 € |
| 2 | Configuration retenue par défaut dans l'app DEV | Une ligne dans `app.yaml` | 0 |
| 3 | Côte à côte Sonnet 4.6 / 5.5 / Haiku sur 60 à 100 vraies questions (§ 3.3) | Un notebook à écrire | ≈ 20 € |
| 4 | Côté app, sans re-découper : R2, R3, R1, puis R4 et R6 | Petit | 0 |
| 5 | Notebook de re-découpage DEV : v2a et v2b, plus E3 (§ 5.4, § 5.5) | Moyen | Embedding seul |
| 6 | Métadonnées dans l'index (P10), marquage des tables des matières et du texte répété (P9) | Moyen, dans le même re-découpage | Embedding seul |
| 7 | Fiche par document (E1), puis contexte par passage (E2), sur un index de test | Moyen | ≈ 15 € puis ≈ 50 € |
| 8 | Porter ce qui gagne dans le pipeline, run `full` en UAT avec ton accord (§ 5.5) | Moyen | Re-embedding de l'index |
| Plus tard | Numéros de page et de slide (P13, re-parsing GPU), reranker par LLM ou ré-entraîné, autre modèle d'embedding | Gros | À chiffrer |

## 9. Décidé et codé le 2026-10-08

Décisions prises avec l'utilisateur, toutes acceptées. Code poussé, **rien n'a tourné sur
Databricks** : le test se fait en DEV (`operations_dev.md`, bloc R), l'UAT seulement ensuite
(`OPERATIONS.md`, D5).

| Constat | Ce qui est codé | Où |
|---|---|---|
| P1 | Le chemin `HybridChunker` (jamais utilisé) est retiré du pipeline | `utils.py` |
| P2 | `semantic_headers` = le chemin de titres **commun** à tout le passage : toujours une vraie lignée | `chunking.py` |
| P3 | Jamais deux sections de niveau 1 ou 2 dans un passage. Une section minuscule (moins d'un tiers du minimum, ex. « 1. Objet » de deux lignes) rejoint la suivante, avec sa ligne `[1. Objet]` | `chunking.py` |
| P4 | Chevauchement réel (12 % de la cible) entre passages d'une même section. Le réglage n'était même pas transmis aux workers Spark : il valait 0 partout | `chunking.py`, `3_Parse` (`configure`) |
| P5 | La ligne de section une seule fois en tête du passage ; une sous-section qui commence au milieu a sa ligne `[3.1 Level 3]` une fois | `chunking.py` |
| P6 | Taille inchangée (250 / 500 / 1 000 tokens) ; une variante courte se mesure en DEV (`v2b`) | `config.py` (réglable par variable d'environnement) |
| P7 | Passage d'image : `Section : …` et `Légende : …` dans le texte indexé, rattaché au passage où l'image se trouve (`anchor_chunk_index`), longues retranscriptions découpées (`…-IMG-003-2`). Widget `rebuild_image_chunks` pour tout reconstruire sans LLM. Nouveau prompt pour les **futures** images : la première phrase nomme le sujet | `chunking.py`, `4_Describe`, `config.py` |
| P8 | Plafond de 4 000 caractères en plus des tokens (`PARSING_MAX_CHUNK_CHARS`) | `chunking.py`, `config.py` |
| P9 | `chunk_content_type` = `toc` (sommaires), `front_matter` (cartouches, historiques), `boilerplate` (même texte dans 20 documents ou plus). Décidé **paragraphe par paragraphe** depuis le 2026-10-08 (premier essai `v2a` : des résumés collés au cartouche étaient marqués avec lui) : le cartouche est coupé du résumé qui le suit, un passage ne mélange jamais cartouche ou sommaire et contenu, les sommaires sans points de conduite ou en tableau sont reconnus. Côté app, `CHAT_VSI_SKIP_NOISE=on` les écarte (désactivé par défaut) | `chunking.py`, `3_Parse`, `chat_vsi_rerank.py` |
| P10 | Colonnes `titre`, `type_document`, `indice`, `langue` (vraie langue : suffixe de la REF, sinon le texte), `body_sha256` ; `Type :` dans le préfixe `[Source: …]` | `3_Parse`, `utils.py`, `4_Describe` |
| P12 | Ligne d'en-tête des tableurs = la première qui remplit au moins 60 % des colonnes (les lignes de titre au-dessus restent en texte) ; `chunks_truncated` vrai seulement au-delà de 100 passages | `chunking.py`, `utils.py`, `3_Parse` |
| P15 | Variante DEV sans préfixe (`v2c`) | `rechunk_experiment.py` |
| E1, E2 | Fiche par document et contexte par passage, **désactivés par défaut** (widgets `doc_cards`, `chunk_context`, `enrich_max_docs`) | `rechunk_experiment.py` |

Pas fait : P11 (documents d'avant 2018, décision métier), P13 (numéros de page, re-parsing
GPU), P16 (prix Haiku, colonnes de recherche).

Robustesse du chat (même jour, `docs/chat_vsi_robustesse_2026-10.md`) : GPT-6 Luna avec GPT-5.6
Luna en secours, relances, reprise d'une réponse coupée, file d'attente, recherche partielle
acceptée, détection de langue nettoyée (`chat_vsi_llm.py`). Mise en service : `operations_dev.md`,
bloc L.

Effets à connaître :
- les nouvelles colonnes arrivent dans les tables existantes par `mergeSchema` / `autoMerge` ;
  les lignes anciennes restent à `NULL` jusqu'au run `full` ;
- `generate_synthetic_retrieval_questions.py` ne tire que des passages `text`, `table`,
  `mixed` : il ignore désormais les sommaires et cartouches, c'est voulu ;
- `chunk_index` ne change pas de sens (rang du passage dans le document) : le golden builder
  continue de trouver les voisins par `chunk_index`.

## Sources

- [Qwen3-Embedding-0.6B, fiche du modèle (consigne de requête, 32 000 tokens)](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B)
- [Databricks : annonce de l'embedding Qwen3 sur Foundation Model APIs](https://www.databricks.com/blog/sota-embedding-model-agentic-workflows-now-public-preview)
- [Databricks : paquet Python Vector Search (reranker, troncature à 2 000 caractères)](https://api-docs.databricks.com/python/vector-search/databricks.vector_search.html)
- [Databricks : interroger un index (hybride, filtres, reranker)](https://docs.databricks.com/gcp/en/vector-search/query-vector-search)
- [Databricks : reranking dans Vector Search](https://www.databricks.com/blog/reranking-mosaic-ai-vector-search-faster-smarter-retrieval-rag-agents)
- [Databricks : guide de qualité de recherche](https://docs.databricks.com/aws/en/vector-search/vector-search-retrieval-quality)
- [Databricks : affiner le reranker sur ses données](https://docs.databricks.com/aws/en/ai-search/reranker-finetuning)
- [Anthropic : Contextual Retrieval](https://www.anthropic.com/news/contextual-retrieval)
- `docs/ka-migration.md` : fonctionnement interne du KA (Instructed Retriever, IR-1).

## 5.7 Nombre de passages (2026-10-08, index `chunks_index`, découpage retenu)

`retrieval_eval`, 65 questions, chaque configuration = le chat avec un seul réglage changé.

| Config | Trouvés | Dans les 5 premiers | Au moins un | Contexte (tokens) | Golden | Synthétiques | Retours (16) |
|---|---|---|---|---|---|---|---|
| `raw5` (12 reclassés + 5 bruts) | 80.0 % | 65.4 % | 83.1 % | 9 277 | 94.4 % | 90.7 % | 43.8 % |
| `chat` (12 + 10, actuel) | 79.4 % | 67.8 % | 81.5 % | 11 307 | **96.1 %** | **94.6 %** | 31.3 % |
| `cap40` (plafond 40) | 78.5 % | 68.3 % | 81.5 % | 10 717 | 87.8 % | 93.6 % | 37.5 % |
| `cap25` (plafond 25) | 78.1 % | 65.5 % | 81.5 % | 7 808 | 86.1 % | 90.7 % | 43.8 % |
| `rerank-only` (12 reclassés, 0 brut) | 76.6 % | 65.5 % | 80.0 % | 7 653 | 86.6 % | 90.7 % | 37.5 % |
| `rerank8-only` (8 reclassés, 0 brut) | 70.9 % | 68.7 % | 75.4 % | 5 558 | 75.2 % | 84.8 % | 37.5 % |

- **Les passages bruts restent utiles avec le nouveau découpage** : sans eux, −10 points sur le golden (la définition d'APO dans le glossaire INAPO0006, QP1151, Q0627GO disparaissent).
- **Plafonner coûte sur le golden** (−8 à −10 points) : les questions qui attendent beaucoup de documents (10 pour « documents qui parlent de qualification CND ») perdent les derniers.
- **`raw5` fait jeu égal avec l'actuel** (80.0 % contre 79.4 %) avec 18 % de contexte en moins, mais perd 2 et 4 points sur golden et synthétiques. Les écarts sur les retours (16 questions : 1 question = 6 points) sont du bruit.
- Défaut de l'éval corrigé le même jour : les questions des retours utilisateurs portaient encore le préfixe « [Division: AS] (system routing note…) » du Knowledge Assistant ; `retrieval_eval` et `pairwise_answers` le retirent désormais (`_strip_division`), comme l'app.
- **Décision** : rien ne change tant que les réponses n'ont pas été comparées (`pairwise_answers`, `chat` contre `raw5`).

