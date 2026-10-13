# Chatbot Qualibot — configuration retenue et mesures (2026-10-06 → 2026-10-09)

Ce qui a été mesuré sur le chatbot : la configuration gardée (= le code actuel), les résultats qui ont décidé, ce qui a
été écarté, ce qui reste à tester. Le journal complet des essais (configurations KA, vague 3, audit détaillé du
parsing P1 à P16, prix des modèles) est dans l'historique du dépôt, à la version de ce fichier au commit `f545093`.

Code : `server/services/chat/engine.py` (+ `chat/llm.py`, `translation_bridge.py`). Notebooks de mesure gardés :
`utils/evaluation/retrieval_eval.py` (recherche seule) et `pairwise_answers.py` (réponses côte à côte) ;
`load_test_chat.py` et `load_test_vector_search.py` (charge). Le code des variantes testées puis écartées
(`chat_vsi_rerank.py`, `golden_eval_ka_vs_vsi.py`, `rechunk_experiment.py`…) a été supprimé du dépôt : commit `f545093`.
Robustesse (secours, relances) : `docs/chat_robustesse_2026-10.md`.

# Partie 1 — Ce qu'on garde

## A. Configuration retenue

| Étape | Ce qui se passe | Mesuré dans |
|---|---|---|
| Langue | fastText détecte la langue. Une question ni française ni anglaise est traduite en anglais (GPT-5.6 Luna, secours GPT-6 Luna) ; la réponse est retraduite | Juillet (`translation_bridge.py`) |
| Réécriture | GPT-6 Luna (secours GPT-5.6 Luna) écrit une requête en français **et** une en anglais, acronymes développés | `u-bi` (§ 2), `u-all-luna6` (§ 4) |
| Recherche | 3 requêtes (question, français, anglais), chacune HYBRID : 12 passages reclassés par le reranker de Vector Search (colonnes `REF`, `semantic_headers`, `chunk_text`), **plus** 10 passages bruts sur la réécriture française seulement (`CHAT_RAW_ON=fr`, sur la question si la réécriture échoue), fusionnés par rang : 4 requêtes Vector Search | `union-ctx`, `rawfr` (§ 5) |
| Documents cités par REF | Jusqu'à 6 REF nommées dans la conversation : 8 passages de ces documents, placés en tête | `union-ctx-ref` |
| Documents cités par titre | **Retiré le 2026-10-12** : le titre est déjà dans le préfixe `[Source: …]` de chaque passage, donc dans l'embedding ; aucun gain isolé mesuré (`u-title` : 13 / 16 / 11 contre 15 / 16 / 9 pour `union-ctx`, § 2) | `u-title` |
| Une langue par document | Q0102QP_GB et Q0102QP_BG ne prennent qu'une place | `u-1lang` |
| Division (AS / IS) | **Un seul index** `chunks_index` ; le choix AS / IS est un filtre Vector Search sur la colonne `division`, appliqué dans la requête elle-même (pas après) : déterministe, sans coût mesurable | Déjà utilisé pour les REF (filtre `REF`) |
| Prompt | Instructions de la division (`instructions_<div>.md`), puis les règles de réponse (`answer_rules.md` : seulement les documents, refus hors sujet, glossaire des préfixes de REF, règle de citation avec exception pour une URL écrite dans un passage), puis les documents numérotés, la question, et une ligne qui **nomme** la langue de réponse | `luna6-prompt` (`u-all+v3+lang`) |
| Réponse | GPT-6 Luna, secours GPT-5.6 Luna, 8 000 tokens maximum (réflexion comprise) | `luna6-versions`, `luna6-prompt` |
| Index | Découpage 150 / 300 / 450 tokens, 1 600 caractères maximum, 12 % de chevauchement, préfixe `[Source: …]`, sommaires et cartouches **marqués** (`chunk_content_type`) mais **gardés** | `idx-v2b-all` (§ 4) |

**Aucun modèle Claude dans le chatbot** (décision du 2026-10-08) : réécriture, réponse, traduction et secours sont des
modèles Luna.

Le filtre de division : `filters_json={"division": ["AS"]}` restreint la recherche dans l'index avant le classement : un
passage IS ne peut pas sortir pour une question AS, et la requête coûte le même temps. Les index par division
(`chunks_as_index`, `chunks_is_index`) et les tables `src_chunks_as` / `src_chunks_is` n'existaient que pour le
Knowledge Assistant, qui ne savait pas filtrer : ils disparaissent.

## B. Les noms des essais

Ils ne figurent plus dans le code.

| Nom | Ce que c'était | Aujourd'hui |
|---|---|---|
| KA, `ka` | Knowledge Assistant (Agent Bricks), le premier chatbot | Retiré le 2026-10-08 |
| VSI | Le chatbot sans KA : Vector Search + un LLM | Le chatbot, onglet « Chat » |
| `baseline` | Première version : une requête, 10 passages, Sonnet 4.6 | Remplacée |
| `rerank`, `union`, `union-ctx`, `u-*`, `u-all` | Réglages de la recherche ; `rerank` = reclassés seuls, `union` = reclassés (12 par requête) + bruts (10), `ctx` = le reranker lit aussi REF et titres de section (neutre) | `u-all` = la recherche actuelle, sans réglage |
| Instructions `v2` | Instructions réécrites pour le VSI | Écartées |
| Instructions `v3`, « addendum » | Instructions KA + règles d'ancrage, refus hors sujet, glossaire | `answer_rules.md` |
| `bi`, `luna`, `1lang`, `title`, `ref` | Réécriture bilingue ; réécriture par GPT-5.6 Luna ; une langue par document ; recherche par titre ; recherche par REF citée | Voir § A |
| `lang`, `CHAT_LANGUAGE_REMINDER` | Ligne de langue après la question | Toujours active |
| `CHAT_SKIP_NOISE`, `-clean` | Écarter sommaires et cartouches de la recherche | Écarté (−11 points) |
| Index `v1` | Découpage 250 / 500 / 1 000 tokens, l'index UAT copié en DEV | Remplacé |
| Index `v2a` | Découpage corrigé, même taille que `v1` | Écarté |
| Index `v2b` | Découpage corrigé, 150 / 300 / 450 tokens | **Le découpage actuel** : table `chunks`, index `chunks_index` |
| Index `v2c` | `v2b` sans préfixe `[Source: …]` | Préparé, non mesuré |
| `idx-<v>`, `idx-<v>-all` | Une recherche (`union-ctx` ou `u-all`) sur l'index de test `<v>` | — |
| `luna6-versions`, `luna6-prompt`, `luna6-v2b`, `passages-rawfr` | Runs de `pairwise_answers` (`eval_id`) | — |

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
| Recherche brute sur la réécriture française seulement (`rawfr`) | 81,9 % contre 79,4 %, −20 % de contexte, −33 % de requêtes | `rawfr` |
| KA (réponses stockées) contre VSI | 1 gagné / 36 perdus / 3 égalités | KA retiré |

## D. Testé et écarté

- Le reranker seul (`rerank`), sans les résultats bruts.
- Les instructions réécrites `v2`, seules ou avec union.
- Le budget de contexte (25 000 ou 35 000 caractères) : moins de documents trouvés.
- 20 ou 25 passages reclassés par requête.
- 3 passages au plus par document (`u-1lang-k20-cap3`).
- Écarter les passages marqués sommaire / cartouche / texte répété (`CHAT_SKIP_NOISE`).
- Le découpage `v2a` (même taille corrigée) : battu par `v2b`.
- Sonnet 4.6, Sonnet 5.5 pour répondre ; Sonnet 4.6 et GPT-5.6 Luna pour réécrire.
- Gemini 3.1 Flash-Lite : faux positifs reproductibles sur l'impact search.
- Le Knowledge Assistant.

## E. À tester plus tard

| Piste | Ce que c'est | Où en est le code |
|---|---|---|
| E1 — fiche par document | Un passage de synthèse par document, généré par LLM (objet, domaine, sujets, rôles, documents cités). ≈ 15 € pour environ 6 000 documents | Le script d'essai est supprimé du dépôt (commit `f545093`) |
| E2 — contexte par passage | Une ou deux phrases en tête de chaque passage pour le situer (méthode « Contextual Retrieval »). ≈ 50 € | Idem |
| `v2c` — sans préfixe `[Source: …]` | Mesure l'effet du préfixe sur la recherche (jamais mesuré sur la recherche : seule trace, un test local sur 3 documents) | Idem |
| **Nombre de passages** | Jusqu'à 3 × 12 reclassés + 10 bruts, sans plafond. Les passages bruts ont été gardés parce que le reranker seul perdait des réponses (48 % contre 76 %), mais c'était avec les passages de 4 000 caractères dont le reranker ne lit que les 2 000 premiers. Avec le découpage actuel (≤ 1 600), la comparaison des réponses reste à faire (§ 5) | Réglages `CHAT_RERANK_TOP_K`, `CHAT_RAW_TOP_K`, `CHAT_MAX_SEARCH_PASSAGES` ; `operations_dev.md`, bloc T |
| Pistes côté recherche R1 à R12 | § 6 | Rien de codé |
| Documents d'avant 2018 | Leur fiche dans le chatbot | Décision métier ; pipeline prêt (`parsing_archive_notices_in_rag`) |
| Numéros de page et de slide | Le markdown exporté ne garde ni les pages des PDF ni les slides des PPTX ; il faudrait un marqueur de saut de page à l'export Docling (option `page_break_placeholder`, à vérifier) et un re-parsing GPU | Rien de codé |

Pour mesurer une piste : construire un index de test en DEV, puis `retrieval_eval` avec
`indexes = chat,essai=dev_landingzone.qualibot.<index de test>` et, si la recherche gagne, `pairwise_answers` avec le
même index en concurrent.

---

# Partie 2 — Mesures

## 0. Lexique

| Terme | Ce que c'est |
|---|---|
| **Passage** (*chunk*) | Un morceau d'un document : l'unité indexée et renvoyée par la recherche |
| **REF** | La référence Intraqual d'un document (QP-1518, MI-14183…). Une même REF existe souvent en plusieurs langues (QP-1518, QP-1518_GB, Q0102QP_BG…) |
| **Recherche hybride** | Vector Search combine la recherche par sens (embedding `databricks-qwen3-embedding-0-6b`) et par mots-clés |
| **Reranker** | `databricks_reranker` : relit la question et chaque candidat ensemble et reclasse. Ne voit que les 50 premiers candidats et que les 2 000 premiers caractères de chaque passage |
| **Réécriture** | Un LLM reformule la question en requêtes autonomes (français, anglais) avant de chercher ; elle retarde le premier mot |
| **Golden** | Le jeu de 21 questions de référence avec réponses et documents attendus (`qualibot_eval_golden`) |
| **Juge** | Un LLM qui note une réponse par rapport à l'attendu (juges MLflow `Correctness`, `ExpectationsGuidelines`) |
| **Documents trouvés** (rappel) | Part des documents attendus qui sont dans les passages envoyés au LLM. `recall_top5_pct` : limité aux 5 premiers documents du contexte (compare des configurations de taille différente) ; `at_least_one_pct` : part des questions où au moins un document attendu est trouvé |
| **Contexte** | Tout ce qu'on envoie au LLM : instructions + passages + question. Sa taille fait le coût |
| **Premier mot** | Temps avant que la réponse commence à s'afficher |

## 1. Comment on a mesuré

**Évaluation golden** : 21 questions de `qualibot_eval_golden`. La réponse complète est générée, puis notée par les juges
MLflow ; on mesure aussi la part des documents attendus retrouvés. **Bruit : ±2 questions, soit environ ±10 points, entre
deux runs identiques** (deux runs de la même configuration : 76,2 % et 71,4 %). 7 questions sur 21 sont des refus ou des cas
« pas dans la documentation » que tout le monde réussit : il reste environ 14 questions qui départagent, chacune pèse 7 points.

**Évaluation de la recherche seule** (`retrieval_eval.py`, table `eval_retrieval_runs`) : pour chaque question, les
documents attendus sont-ils dans les passages envoyés au modèle ? Pas de génération, pas de juge : reproductible,
≈ 0,001 € par question. **65 questions** : 15 du golden, 34 synthétiques (`synthetic_retrieval_questions_v2` de l'UAT),
16 retours négatifs où l'utilisateur a nommé le document attendu (`feedback_failure_cases`).
- Limite : les documents « pertinents » des questions synthétiques ont été choisis parmi ceux que le KA d'UAT avait
  retrouvés, ce qui favorise les recherches qui ressemblent à celle du KA. Les retours négatifs sont le jeu le plus
  réaliste mais ne comptent que 16 questions (une question = 6 points).
- Les erreurs (le service d'embedding refuse les rafales) retirent une question du calcul de sa configuration ; les
  tableaux sont calculés sur les questions réussies par toutes les configurations.

**Réponses côte à côte** (`pairwise_answers.py`, table `eval_pairwise_runs`) : mêmes passages, deux modèles ou deux
réglages ; un juge (GPT-5.6 Luna) compare dans les deux ordres, une version ne gagne que si les deux lectures sont d'accord ;
fidélité, exactitude et inventions notées. « Juge constant » = part des questions où le verdict est le même dans les deux ordres.

## 2. Réponses côte à côte : GPT-6 Luna contre Sonnet 5.5 (2026-10-08)

`luna6-versions` : 40 questions (21 golden + 19 vraies questions DEV auxquelles le KA a répondu). Référence Sonnet 5.5 + `union-ctx`.

| Concurrent | Gagnés / perdus / égalités | Fidélité (con / réf) | Exactitude (con / réf) | Inventions (con / réf) | Juge constant |
|---|---|---|---|---|---|
| Luna @ `u-all` | 16 / 15 / 9 | 2.60 / 2.49 | 2.51 / 2.64 | 0.76 / 1.40 | 78 % |
| Luna @ `union-ctx` | 15 / 16 / 9 | 2.43 / 2.41 | 2.50 / 2.84 | 0.99 / 1.41 | 78 % |
| Luna @ `u-title` | 13 / 16 / 11 | 2.60 / 2.48 | 2.55 / 2.70 | 0.83 / 1.36 | 73 % |
| Luna @ `u-bi` | 13 / 19 / 8 | 2.43 / 2.51 | 2.40 / 2.70 | 1.05 / 1.35 | 80 % |
| KA (réponses stockées) | 1 / 36 / 3 | 1.70 / 2.64 | 1.69 / 2.80 | 2.33 / 0.98 | 93 % |

Luna @ `u-all` fait jeu égal avec Sonnet 5.5 et invente deux fois moins, pour 0.0034 € par question contre 0.093 €.
Défauts vus : Luna répondait à des demandes hors sujet (les instructions `ka` n'ont pas de règle de refus, elle est dans `v3`),
et parfois dans une autre langue que la question (en partie un défaut de l'éval, qui ne retraduisait pas les réponses).

**`luna6-prompt`** (même référence, même juge, réponses retraduites comme dans l'app) :

| Concurrent | Gagnés / perdus / égalités | Fidélité (con / réf) | Exactitude (con / réf) | Inventions (con / réf) | Juge constant |
|---|---|---|---|---|---|
| Luna @ `u-all+v3+lang` | **19 / 16 / 5** | **2.71** / 2.39 | 2.61 / 2.63 | **0.63** / 1.59 | 88 % |
| Luna @ `u-all` (témoin) | 15 / 13 / 12 | 2.44 / 2.39 | 2.45 / 2.73 | 0.99 / 1.48 | 70 % |
| Luna @ `u-all+v3` | 14 / 17 / 9 | 2.61 / 2.41 | 2.56 / 2.71 | 0.74 / 1.55 | 78 % |

0.0036 € par question (Sonnet : 0.089 €), premier token 6.9 s (Sonnet : 6.0 s), ≈ 27k tokens d'entrée.
- **Retenu : GPT-6 Luna + `u-all` + `v3` + rappel de langue.** `v3` seul réduit les inventions mais perd des duels ; c'est le
  rappel de langue qui fait la différence.
- Luna refusait de donner l'URL du SharePoint OPEX écrite dans INAQ-742 à cause de la consigne « Do not write URLs » :
  `v3` autorise désormais à citer une URL écrite dans un passage quand on demande un lien.
- Restent des défauts de recherche, pas de modèle : confusion AIPS 02-01-003 / 01-02-003, document cité absent des
  résultats (Q0062MI). Réponses de Luna plus courtes et parfois moins complètes que Sonnet (NDT, qualification peinture).
- Sonnet 5.5 coûte autant que Sonnet 4.6 (tarif −33 % mais ≈ 40 % de tokens d'entrée en plus, 2,5 fois plus de sortie, réflexion
  comprise) et son premier mot arrive à 8 s au lieu de 3 s.

## 3. Recherche seule : quelques repères

| Config | Documents trouvés | Contexte |
|---|---|---|
| `baseline` (2 requêtes, 10 passages) | 61,5 % | 9 000 tok |
| `rerank` (reclassés seuls) | 68 % | 9 600 tok |
| `union-ctx` (12 reclassés + 10 bruts) | 72 % | 14 700 tok |
| `union-ctx-k20` (20 reclassés) | 72 % | 18 700 tok |
| `union-ctx-b35k` / `-b25k` (contexte plafonné) | 66 % / 64 % | 8 600 / 6 200 tok |
| `u-all` | 80 % | — |

- Toutes les configurations avec reranker battent la baseline ; sur les retours négatifs elles trouvent 2 à 3 fois plus
  de documents attendus. Plafonner le contexte fait perdre des documents.
- Donner la REF et les titres de section au reranker ne change rien (chaque passage commence déjà par
  `[Source: REF | Title | Division | Category | Date]`).
- Les retours négatifs restent le point faible (38 % au mieux) : une partie des documents attendus n'est peut-être pas dans
  l'index (requête Q2 du § 8 : sépare « absent de l'index » de « raté par la recherche »).

## 4. Découpage : `v2b` retenu (2026-10-08)

`retrieval_eval`, 65 questions, toutes sur l'index ALL de chaque variante. `-all` = recherche `u-all` ; sans suffixe =
`union-ctx` ; `-clean` = `union-ctx` sans sommaires, cartouches ni textes répétés (`CHAT_SKIP_NOISE`).

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

- **v2b (150 / 300 / 450 tokens, 1 600 caractères) est retenu** : +5 points sur l'ancien index, +7 points dans les 5
  premiers, avec **deux fois moins de contexte**. Gain net sur le golden (96.1 % contre 91.7 %) et sur les retours utilisateurs
  (43.8 % contre 25 %) ; égalité sur les questions synthétiques (tirées des anciens passages, elles favorisent l'ancien découpage).
- **v2a** (même taille, découpage corrigé) gagne 4 points à contexte égal : les corrections de découpage comptent, la taille aussi.
- **Écarter le « bruit » fait perdre** (−11 points sur v2b) : des passages marqués sommaire, cartouche ou texte répété contiennent
  des réponses. `CHAT_SKIP_NOISE` reste désactivé ; le marquage reste dans les tables comme information.
- **Réécriture par GPT-6 Luna** : `u-all-luna6` = 80.0 %, comme l'ancien `u-all` réécrit par Sonnet 4.6 : aucune perte.
- Vérifications du même jour : second run `idx-v2b-all` 83.6 % contre 78.5 % pour `idx-v1-all` ; `idx-v2b-all-k20` 80.5 %, moins
  bien que 12 passages → `CHAT_RERANK_TOP_K` reste à 12 ; réponses (`luna6-v2b`, GPT-6 Luna des deux côtés, seul l'index change) :
  10 gagnés / 10 perdus / 20 égalités, exactitude 2.81 contre 2.61, inventions 0.40 contre 0.53, **0.0022 € par question contre
  0.0036 €**, premier token 5.9 s contre 8.1 s, juge constant 53 % seulement (les réponses sont proches).
- v2b trouve NF-10065 (template CMP) et ŘLCZ-05/15 que v1 ratait ; v1 trouve la définition d'APO dans le glossaire que v2b rate.

## 5. Nombre de passages (2026-10-08) et recherche brute (2026-10-09)

`retrieval_eval`, 65 questions, chaque configuration = le chat avec un seul réglage changé.

| Config | Trouvés | Dans les 5 premiers | Au moins un | Contexte (tokens) | Golden | Synthétiques | Retours (16) |
|---|---|---|---|---|---|---|---|
| `raw5` (12 reclassés + 5 bruts) | 80.0 % | 65.4 % | 83.1 % | 9 277 | 94.4 % | 90.7 % | 43.8 % |
| `chat` (12 + 10) | 79.4 % | 67.8 % | 81.5 % | 11 307 | **96.1 %** | **94.6 %** | 31.3 % |
| `cap40` (plafond 40) | 78.5 % | 68.3 % | 81.5 % | 10 717 | 87.8 % | 93.6 % | 37.5 % |
| `cap25` (plafond 25) | 78.1 % | 65.5 % | 81.5 % | 7 808 | 86.1 % | 90.7 % | 43.8 % |
| `rerank-only` (12 reclassés, 0 brut) | 76.6 % | 65.5 % | 80.0 % | 7 653 | 86.6 % | 90.7 % | 37.5 % |
| `rerank8-only` | 70.9 % | 68.7 % | 75.4 % | 5 558 | 75.2 % | 84.8 % | 37.5 % |

Les passages bruts restent utiles avec le nouveau découpage (sans eux −10 points sur le golden : la définition d'APO dans
INAPO0006, QP1151, Q0627GO disparaissent) ; plafonner coûte −8 à −10 points sur le golden.

**Recherche brute sur une réécriture seulement** :

| Config | Requêtes / question | Trouvés | Golden | Synthétiques | Retours (16) | Contexte | Recherche p50 |
|---|---|---|---|---|---|---|---|
| `chat` (brute sur question, fr, en) | 6 | 79.4 % | **96.1 %** | 94.6 % | 31.3 % | 11 307 | 4.9 s |
| `rawq1` (1 brute, sur la question) | 4 | 80.0 % | 87.8 % | 93.6 % | 43.8 % | 8 946 | 3.3 s |
| `rawfr` (brute sur la réécriture FR) | 4 | **81.9 %** | 93.8 % | 94.6 % | **43.8 %** | 8 983 | 4.9 s |
| `rawfren` (brute sur fr + en) | 5 | 81.0 % | 85.4 % | 96.6 % | 43.8 % | 9 900 | 4.7 s |
| `rawen` (brute sur la réécriture EN) | 4 | 76.8 % | 87.1 % | 90.7 % | 37.5 % | 9 216 | 4.8 s |

`rawfr` : +2,5 points au total, +12,5 sur les retours utilisateurs, −2,3 sur le golden, −20 % de contexte, −33 % de requêtes
Vector Search. **Décision (2026-10-09) : `rawfr` est le défaut** (`CHAT_RAW_ON=fr`). Comparaison des réponses (`passages-rawfr`,
40 questions) :

| | `rawfr` | `rawall` (ancien) |
|---|---|---|
| Victoires (verdict stable dans les deux ordres) | 9 | 14 |
| Égalités ou verdicts instables | 17 | 17 |
| Fidélité / exactitude (sur 3) | 2,86 / 2,80 | 2,75 / 2,79 |
| Inventions par réponse | **0,35** | 0,44 |
| Premier mot p50 | **7,1 s** | 8,1 s |
| Tokens d'entrée / de sortie par réponse | 13 247 / 1 027 | ≈ 15 500 (estimé) / — |
| Coût par question | 0,0020 € | 0,0023 € |

Le juge préfère `rawall` 14 fois contre 9, presque toujours pour « plus complet » ; 9 contre 14 sur 23 verdicts n'est pas
significatif (test du signe, p ≈ 0,4). `rawfr` reste le défaut. Vu au passage : « resposta em frances » est répondu en
portugais par les deux réglages ; la langue de réponse suit la langue de la question, pas une langue demandée dans la question.

## 6. Tenue en charge (2026-10-08, DEV)

**Vector Search seul** (`load_test_vector_search.py`, sans LLM ni relance) :

| Requête | Servies/s sans refus | Plafond servies/s | Latence p50 (palier 8) |
|---|---|---|---|
| HYBRID 10 (brute) | 25.8 (8 simultanées) | ≈ 26 | 0.30 s |
| HYBRID 12 + reranker | 16.2 (8) | ≈ 26 | 0.48 s |
| ANN 10 (vecteurs seuls) | 49.3 (8) | ≈ 80 | 0.15 s |
| HYBRID + filtre de division | 22.6 (8) | ≈ 22 | 0.32 s |
| mélange du chat | 22.2 (8) | ≈ 25 | 0.40 s |

- Le reranker n'abaisse pas le plafond (≈ 26 requêtes/s avec ou sans) : il rend chaque requête plus lente (0,5 s contre 0,2 s).
  C'est la recherche HYBRID qui plafonne ; ANN seul monte trois fois plus haut. Le filtre ne coûte presque rien.
- Sans file, le chat de bout en bout (via l'app) donnait 100 % de réussite à 5 et 10 questions simultanées, puis 91 %, 84 %,
  76 % à 20, 40, 80, toutes les erreurs `Vector Search returned 429` : une question envoie 6 à 8 requêtes d'un coup et
  l'endpoint refuse au-delà de ≈ 16 requêtes en cours.
- **Corrigé dans le code** : `server/services/infra/vs_gate.py`, une file commune au chat et à l'impact search, au plus 8
  requêtes Vector Search en cours par instance (`VS_MAX_CONCURRENT_QUERIES`), un refus qui arrive quand même est relancé jusqu'à
  5 fois en ≈ 15 s en respectant `Retry-After` (`VS_QUERY_RETRIES`).
- Messages de refus : HYBRID `Request is rejected due to heavy load` → capacité de calcul de l'endpoint (ce que règle *Target
  QPS*, capacité réservée payante) ; ANN `Too many requests` → limite de débit, atteinte vers 60/s.

**Chat avec la file `vs_gate.py`** (`load_test_chat.py`, mode `engine`, 6 requêtes par question) :

| Questions en cours | Réussite | Premier mot p50 / p95 | Réponse complète p50 / p95 | Débit (notebook) |
|---|---|---|---|---|
| 10 | 100 % | 10,0 s / 16,0 s | 13,0 s / 19,6 s | 39 /min |
| 20 | 100 % | 11,0 s / 19,5 s | 12,6 s / 23,9 s | 66 /min |
| 30 | 100 % | 12,9 s / 22,3 s | 15,1 s / 26,4 s | 86 /min |
| 40 | 100 % | 15,8 s / 27,3 s | 19,4 s / 32,2 s | 91 /min |

Vector Search tient (aucune ligne `vs_gate:` à aucun palier). **La limite suivante est le modèle** : à 40 en cours, GPT-6 Luna
répond `429 rate_limit` ; ≈ 45 réécritures et ≈ 75 réponses sur 160 passent par GPT-5.6 Luna (aucun échec grâce au secours).

**Quota des modèles** (workspace DEV, identique pour GPT-6 Luna et GPT-5.6 Luna) : 1 000 000 tokens d'entrée/min,
**100 000 tokens de sortie/min**, 1 000 requêtes/s. Avec `rawfr` (13 247 tokens d'entrée, 1 027 de sortie par réponse) :
≈ 75 questions/min côté entrée, ≈ 97/min côté sortie ; l'entrée limite la première (avec `rawall`, ≈ 64/min). Pour tenir 80
questions/min en continu, il faut un quota plus haut ou moins de contexte. Ne pas passer la réécriture sur GPT-5.6 Luna
pour économiser le quota : elle retrouve moins bien les documents (80,0 % contre 75,8 %).

## 7. Pistes côté app non testées

| # | Piste | Gain attendu | Comment le mesurer |
|---|---|---|---|
| R1 | **Passages voisins** : ajouter le passage avant et après les meilleurs résultats | Tableaux et procédures coupés en deux (+20 à 40 % de contexte) | Côte à côte (même document : invisible pour `retrieval_eval`) |
| R2 | **Ordre du document** : passages d'un même document triés par `chunk_index` | Lecture plus cohérente, coût nul | Côte à côte |
| R3 | **Métadonnées une seule fois par document** : retirer le `[Source: …]` répété à chaque passage | Environ 1 500 tokens en moins par question | Coût mesuré, qualité au côte à côte |
| R5 | **Consigne de requête Qwen** (`Instruct: … Query: …`, recommandée pour les questions) : il faudrait calculer le vecteur de la question soi-même (`query_vector` + `query_text`) ; vérifier d'abord que l'API l'accepte sur un index à embedding géré | 1 à 5 % de rappel | `retrieval_eval` |
| R6 | **Recherche par mots-clés** : une requête courte avec les codes et acronymes seuls | Codes (EN 4179, R80, 651) | `retrieval_eval` |
| R7 | **Filtre par type de document** quand la question le dit (« procédure », « formulaire ») | Moins de bruit | `retrieval_eval` |
| R8 | **Suivi de conversation** : réutiliser les documents du tour précédent pour « et pour IS ? » | Questions de suivi | Golden multi-tours |
| R9 | **Seuil « je ne sais pas »** si aucun passage n'est pertinent | Moins d'inventions | Côte à côte avec vérification d'invention |
| R10 | **Langue de la question** : parmi les versions d'un document, garder celle de la langue de la question | Réponses plus fidèles | Côte à côte |
| R11 | **Reranker par LLM** sur les 30 meilleurs passages (Sonnet 4.5 en reranker ≈ modèle interne du KA selon Databricks, nDCG@10 80,1 contre 81,0) ; 1 à 3 s et quelques centimes de plus | Précision | `retrieval_eval` |
| R12 | **Nombre de passages adaptatif** : couper sous un score de reranking | Moins de contexte inutile | `retrieval_eval`, `recall_top5_pct` |

Autres pistes : reranker ré-entraîné sur nos données (projet à part), plus gros modèle d'embedding servi sur GPU
(Qwen3-Embedding 4B/8B, bge-m3 ; `gte-large-en` à écarter, anglais seulement), redécrire les images avec un modèle plus fort
(≈ 10 fois plus cher), modèles de réponse non testés (Haiku 4.5, Sonnet 5.5 avec moins de réflexion). Enrichissements d'index :
E1 et E2 (§ E), glossaire des acronymes tiré des glossaires du corpus (IN_APO_0006…) donné à la réécriture, questions
hypothétiques par passage (≈ 20 €, mais l'index double de taille).

## 8. Découpage et parsing : ce qui a été codé le 2026-10-08

Issu d'un audit du pipeline (P1 à P16, détail dans l'historique du dépôt). Code poussé ; l'UAT est re-découpée seulement avec accord
(`OPERATIONS.md`, D5).

| Constat | Ce qui est codé | Où |
|---|---|---|
| P1 | Le chemin `HybridChunker` (jamais utilisé) est retiré du pipeline | `utils.py` |
| P2 | `semantic_headers` = le chemin de titres **commun** à tout le passage : toujours une vraie lignée | `chunking.py` |
| P3 | Jamais deux sections de niveau 1 ou 2 dans un passage. Une section minuscule (moins d'un tiers du minimum) rejoint la suivante, avec sa ligne `[1. Objet]` | `chunking.py` |
| P4 | Chevauchement réel (12 % de la cible) entre passages d'une même section (le réglage n'était pas transmis aux workers Spark : il valait 0 partout) | `chunking.py`, `3_Parse` (`configure`) |
| P5 | La ligne de section une seule fois en tête du passage ; une sous-section qui commence au milieu a sa ligne `[3.1 Level 3]` | `chunking.py` |
| P6 | Une variante courte se mesure en DEV (`v2b`, retenue) | `config.py` (réglable par variable d'environnement) |
| P7 | Passage d'image : `Section : …` et `Légende : …` dans le texte indexé, rattaché au passage où l'image se trouve (`anchor_chunk_index`), longues retranscriptions découpées (`…-IMG-003-2`). Widget `rebuild_image_chunks` pour tout reconstruire sans LLM. Nouveau prompt pour les futures images : la première phrase nomme le sujet | `chunking.py`, `4_Describe`, `config.py` |
| P8 | Plafond de 4 000 caractères en plus des tokens (`PARSING_MAX_CHUNK_CHARS`) | `chunking.py`, `config.py` |
| P9 | `chunk_content_type` = `toc`, `front_matter`, `boilerplate` (même texte dans 20 documents ou plus), décidé **paragraphe par paragraphe** : un passage ne mélange jamais cartouche ou sommaire et contenu. Côté app, `CHAT_SKIP_NOISE=on` les écarte (désactivé par défaut) | `chunking.py`, `3_Parse` |
| P10 | Colonnes `titre`, `type_document`, `indice`, `langue`, `body_sha256` ; `Type :` dans le préfixe `[Source: …]` | `3_Parse`, `utils.py`, `4_Describe` |
| P12 | Ligne d'en-tête des tableurs = la première qui remplit au moins 60 % des colonnes ; `chunks_truncated` vrai seulement au-delà de 100 passages | `chunking.py`, `utils.py`, `3_Parse` |

Pas fait : P11 (documents d'avant 2018, décision métier ; ≈ 1 500 documents `courant=1` publiés avant 2018 ne sont pas dans
l'index du chatbot), P13 (numéros de page, re-parsing GPU), P16 (colonnes de recherche : `chunk_index` et `chunk_content_type` ne
sont pas demandées par la recherche, nécessaires pour R1, R2 et la mesure de la part d'images). Les nouvelles colonnes arrivent par
`mergeSchema` / `autoMerge` ; les lignes anciennes restent à `NULL` jusqu'à un run `full`.

## 9. Requêtes de diagnostic

À lancer dans l'éditeur SQL du workspace DEV (lecture seule). Elles lisent les tables du pipeline sans suffixe de
`dev_landingzone.qualibot` (depuis le 2026-10-08) et la table d'évaluation.

**Q1 — Composition de l'index : types de passages et tailles**
```sql
SELECT chunk_content_type,
       count(*) AS passages, count(DISTINCT IDDOC) AS documents,
       percentile(length(chunk_text), 0.5) AS p50_chars,
       percentile(length(chunk_text), 0.9) AS p90_chars,
       max(length(chunk_text)) AS max_chars,
       round(avg(CASE WHEN length(chunk_text) > 2000 THEN 1 ELSE 0 END) * 100, 1) AS pct_over_2000_chars
FROM dev_landingzone.qualibot.chunks
GROUP BY ALL ORDER BY passages DESC;
```

**Q2 — Documents attendus par l'évaluation mais absents de l'index**
```sql
WITH expected AS (
  SELECT DISTINCT source, doc
  FROM dev_proj.qualibot.eval_retrieval_runs LATERAL VIEW explode(expected) t AS doc
  WHERE config = 'baseline'),
indexed AS (
  SELECT DISTINCT regexp_replace(regexp_replace(upper(REF), '[-_. ]+(FR|EN|GB|MX|BG|CZ|BR|ES)$', ''), '[^A-Z0-9]', '') AS doc
  FROM dev_landingzone.qualibot.chunks),
files AS (
  SELECT regexp_replace(regexp_replace(upper(ref), '[-_. ]+(FR|EN|GB|MX|BG|CZ|BR|ES)$', ''), '[^A-Z0-9]', '') AS doc,
         max(parse_status) AS parse_status, max(CAST(filtered_by_date AS INT)) AS before_2018, max(doc_date) AS doc_date
  FROM dev_landingzone.qualibot.processed_files GROUP BY 1)
SELECT e.source, e.doc, f.parse_status, f.before_2018, f.doc_date
FROM expected e
LEFT JOIN indexed i ON e.doc = i.doc
LEFT JOIN files f ON e.doc = f.doc
WHERE i.doc IS NULL
ORDER BY e.source, e.doc;
```
Une ligne sans `parse_status` signale un document inconnu du pipeline : hors périmètre, REF mal saisie dans le retour, ou
fichier absent du volume.

**Q3 — Tables des matières et passages anormalement longs pour leur nombre de tokens**
```sql
SELECT chunk_content_type,
       count_if(size(regexp_extract_all(chunk_text, '\\.{5,}\\s*\\d+', 0)) >= 3) AS toc_like,
       count_if(length(chunk_text) / greatest(chunk_token_count, 1) > 8) AS many_chars_per_token,
       count(*) AS passages
FROM dev_landingzone.qualibot.chunks
GROUP BY ALL;
```

**Q4 — Paragraphes répétés dans de nombreux documents (mentions, cartouches)**
```sql
WITH paras AS (
  SELECT IDDOC, trim(p) AS para
  FROM dev_landingzone.qualibot.chunks
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
FROM dev_landingzone.qualibot.chunks c
JOIN (SELECT DISTINCT IDDOC, source_file_extension FROM dev_landingzone.qualibot.processed_files) p USING (IDDOC)
WHERE c.chunk_content_type <> 'image'
GROUP BY ALL ORDER BY 1, 2;
```

**Q6 — Tableurs qui touchent le plafond de 100 passages**
```sql
SELECT count(*) AS spreadsheets, count_if(n >= 100) AS at_cap
FROM (SELECT c.IDDOC, count(*) AS n
      FROM dev_landingzone.qualibot.chunks c
      JOIN (SELECT DISTINCT IDDOC, source_file_extension FROM dev_landingzone.qualibot.processed_files) p USING (IDDOC)
      WHERE lower(p.source_file_extension) IN ('xlsx', 'xls', 'xlsm') AND c.chunk_content_type <> 'image'
      GROUP BY c.IDDOC);
```

**Q7 — Couverture du corpus : statut de chaque document**
```sql
SELECT parse_status, filtered_by_date, include_in_rag, count(*) AS documents
FROM dev_landingzone.qualibot.processed_files
GROUP BY ALL ORDER BY documents DESC;
```

**Q8 — Catégories des descriptions d'images**
```sql
SELECT coalesce(nullif(regexp_extract(description, '^#\\s*\\[([A-Z_]+)\\]', 1), ''), label) AS category,
       count(*) AS images, round(avg(length(description))) AS avg_chars
FROM dev_landingzone.qualibot.image_metadata
WHERE status = 'DONE'
GROUP BY ALL ORDER BY images DESC;
```

## Sources

- [Qwen3-Embedding-0.6B, fiche du modèle (consigne de requête, 32 000 tokens)](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B)
- [Databricks : paquet Python Vector Search (reranker, troncature à 2 000 caractères)](https://api-docs.databricks.com/python/vector-search/databricks.vector_search.html)
- [Databricks : interroger un index (hybride, filtres, reranker)](https://docs.databricks.com/gcp/en/vector-search/query-vector-search)
- [Databricks : reranking dans Vector Search](https://www.databricks.com/blog/reranking-mosaic-ai-vector-search-faster-smarter-retrieval-rag-agents)
- [Databricks : guide de qualité de recherche](https://docs.databricks.com/aws/en/vector-search/vector-search-retrieval-quality)
- [Databricks : affiner le reranker sur ses données](https://docs.databricks.com/aws/en/ai-search/reranker-finetuning)
- [Anthropic : Contextual Retrieval](https://www.anthropic.com/news/contextual-retrieval)
