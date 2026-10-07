# Conception — remplacement du Knowledge Assistant (KA) dans le Chat

Date : 2026-10-06. Statut : options en discussion avec le client, aucune décision prise, aucun code écrit.

## Contexte

- Le KA est passé en Legacy le 2026-09-30. La dépréciation est visée pour fin 2026. À l'End of Support, au plus tôt en février 2027, les endpoints s'arrêtent (voir `docs/ka-migration.md`).
- Les voies proposées par Databricks ont été écartées pour Qualibot :
  - **Genie Agents** : pas de prise en charge des index Vector Search, nouvelle API, pas de traçage MLflow.
  - **Content Search** : beta, utilisable seulement via Genie Agents, et repart de fichiers dans un Volume, donc abandon de notre pipeline.
- Objectif : un composant qui remplace le KA de la façon la plus transparente possible, et qui se comporte comme lui dans les grandes lignes (contrat : `docs/ka-black-box-io-contract.md`).

## Règles

1. **Pas de reproduction à l'identique des mécanismes internes du KA.**
2. **Pas d'usine à gaz : réutiliser l'existant au maximum.**

## Philosophie

- Qualibot possède déjà la partie lourde : parsing, chunking, descriptions d'images, index. Le KA ne fait que la couche fine (comprendre la question, chercher, rédiger avec des citations). C'est elle qu'on remplace, et elle doit rester fine.
- On reproduit le contrat observable (`docs/ka-replacement-mapping.md`), pas la mécanique interne.
- Le prompt (les instructions actuelles des KA) est le levier principal.
- On commence par le plus simple. On ajoute une étape (reformulation, filtres, plusieurs requêtes, reranking) uniquement si la comparaison avec le KA montre un écart mesuré.
- Le plan `docs/plan/chat-vsi-ka-replacement.md` (7 étapes, Instructed Retriever complet dès la v1) va à l'encontre de cette philosophie. Il est à réviser.

## Voies discutées avec le client

- **Voie 1 — Faux KA (Turc mécanique).** On garde le code actuel qui parle au KA et tous ses points de contact. On remplace seulement la boîte noire par du code qui produit la même chose, dans le même format.
- **Voie 2 — Vector Search en direct.** On repart de Vector Search et on construit autour, notamment le parsing et le collage des sources et des réponses dans l'UI.
- **Voie 3 — Agent custom LangChain** avec Vector Search comme outil.

## Comparaison

| | Voie 1 : faux KA | Voie 2 : Vector Search en direct | Voie 3 : agent LangChain |
|---|---|---|---|
| Changement dans l'app | Quasi nul : on garde `stream_chat()` et tout l'aval | Seulement à l'endroit de `stream_chat()` : un nouveau producteur des mêmes événements internes | Même endroit, plus un framework nouveau |
| Reproduit des mécanismes du KA ? | **Oui, au niveau du format** : il faut émettre ses événements (`url_citation`, positions à l'arrivée), que l'app re-décode ensuite. Deux adaptateurs pour rien. | Non | Non, mais ajoute une boucle d'agent |
| Réutilisation de l'existant | L'aval de l'app | L'aval de l'app + `_fetch_chunks` de Compare. La REF et l'URL viennent directement des lignes Vector Search, plus simple qu'avec le KA. | L'aval de l'app ; LangChain absent du projet |
| Risque d'usine à gaz | Faible, mais on émule un format en fin de vie | **Le plus faible** | Le plus fort : framework, boucle d'outils, comportement moins déterministe |
| Latence et maîtrise | Bonnes | Bonnes : une recherche, puis une génération en streaming | Au moins un appel LLM de plus avant la recherche (déduction) ; citations plus dures à contrôler |
| Double fonctionnement avec le KA | Par configuration | Par configuration (moteur ka / vsi) | Par configuration |

## Recommandation

**Voie 2, limitée à l'endroit de `stream_chat()`.**

- Le « construire autour » est déjà fait. L'app sait déjà coller les sources dans l'UI : marqueurs `⟦n⟧`, pastilles de sources, regroupement par langue, persistance Lakebase, rechargement des sessions.
- Il suffit de lui fournir, dans le format interne existant, le texte en flux, des `sources {REF, url}` et des `citations {n, pos}`. On ne réécrit ni le parsing ni l'UI.
- **Ce qu'il y a à écrire :**
  1. le choix de l'index selon la division ;
  2. une recherche hybride Vector Search avec la question ;
  3. le prompt : instructions des KA + passages numérotés + conversation ;
  4. la réponse en streaming ;
  5. la transformation des marqueurs `[n]` en `{n, pos}` et en sources ;
  6. l'émission des événements, y compris les erreurs ;
  7. le choix du moteur (ka / vsi) et la prop `engine` du front.
- **Voie 1 seulement si** un endpoint compatible KA est exigé par d'autres consommateurs (scripts d'évaluation, autres apps). Dans ce cas, le même code est empaqueté en `ResponsesAgent`.
- **Voie 3 seulement si** l'évaluation montre que les questions multi-étapes ou de suivi échouent avec la voie 2.

## Faits vérifiés sur lesquels s'appuie cette analyse

| Fait | Source |
|---|---|
| `stream_chat()` est le seul appel au KA. `chat.py`, Lakebase et le front n'en dépendent pas directement. | code, `docs/ka-usage-scan.md` |
| Les 3 index des KA (`chunks_index_v1` / `_as_` / `_is_`) sont interrogeables en HYBRID et renvoient `REF` et `url`. `filters_json` fonctionne sur `division` et `doc_date`. | vérifié 2026-10-06 |
| `url` est le même lien Intraqual que celui cité par le KA | vérifié 2026-10-06 |
| `streaming.py` sait déjà streamer `databricks-claude-sonnet-4-6` | code |
| Les KA n'ont aucun exemple (ALHF non utilisé) et une seule source chacun | vérifié 2026-10-05 |
| LangChain est absent du projet (ni dépendance, ni import) | code |
| Databricks recommande `ResponsesAgent` et le déploiement des agents custom en Databricks Apps (route `/responses`). Helpers `create_text_delta()` et `create_annotation_added()`. | doc publique (MLflow ResponsesAgent ; Databricks « Author an agent », mise à jour 2026-09-15) — non testé |

## Points ouverts

- Décision client entre les voies.
- Droits du SP de l'app sur les index AS et IS (seul `chunks_index_v1` est accordé). USE_CATALOG sur `dev_landingzone` : à obtenir auprès d'un owner ou admin.
- Lakebase : base `doccompare` partagée avec l'app dev, ou base isolée.
- Jeu de questions et seuils d'acceptation pour comparer avec le KA (qualité, citations, latence).
- Endroit où vivra le code : la copie locale n'a pas de remote.

## Premier test sans KA (2026-10-06)

### Montage

- Script jetable `/tmp/vsi_test.py`, hors du repo.
- Question : « What documents reference the NDT/NDI qualification requirements? ». Aucune division précisée, donc index ALL `chunks_index_v1`.
- Recherche hybride Vector Search, 10 passages (le nombre transmis à la génération dans la trace KA capturée).
- Génération : `databricks-claude-sonnet-4-6`, température 0. Prompt = instructions du KA ALL en service, mot pour mot, + une règle de citation `[n]` + passages numérotés.
- Post-traitement de l'app réutilisé sans modification : `_with_today_date`, `_apply_citation_markers`, `augment_sources`, `_number_sources`.
- Référence : une réponse du KA fournie par le client pour la même question. Documents cités : QP-1518, MR-1465, MI-14059, MR-1462, QM-1063, Q0451MQ_GB (+ Q0451MQ sans numéro).

### Résultats

| | Réponse KA (référence) | Essai 1 : question telle quelle (anglais) | Essai 2 : requête reformulée en français par le LLM |
|---|---|---|---|
| QP-1518, MR-1465, MI-14059, MR-1462 | ✅ | ✅ | ✅ |
| Q0451MQ_GB | ✅ | ❌ | ✅ |
| QM-1063 | ✅ | ❌ | ❌ |
| Documents en plus | — | Q0403MR, NF-10830, NF-10856, MI-13667_GB | Q0403MR, NF-10830, NF-10381, NF-10856, MI-14047_GB |
| Marqueurs de citation | dans le texte | sur des lignes isolées | dans le texte |
| Latence | — | Vector Search 0,7 s ; LLM 14,2 s (sans streaming) | Reformulation 1,2 s + Vector Search 0,8 s + premier token 1,1 s = **premier mot à 3,1 s** ; génération complète 15,8 s |

Recherche Vector Search seule, sans LLM, pour isoler la cause de l'écart sur les documents :

| Requête | Rang des documents attendus |
|---|---|
| Anglais, top 10 | QM-1063, Q0451MQ_GB, Q0451MQ absents |
| Anglais, top 30 | QM-1063 13ᵉ, Q0451MQ_GB 21ᵉ, Q0451MQ absent |
| Français écrit à la main (« … qualification du personnel CND (NDT/NDI) ? »), top 10 | **les 7 documents attendus présents** : Q0451MQ 2ᵉ, Q0451MQ_GB 3ᵉ, MI-14059 6ᵉ, MR-1462 7ᵉ, QM-1063 8ᵉ, MR-1465 9ᵉ, QP-1518 10ᵉ |

### Constats

1. **La langue de la recherche est déterminante.** Ça confirme la règle « Search language » des instructions du KA : chercher en français même si la question est en anglais.
2. **La formulation de la requête française change le résultat.** La requête générée (« … qualification NDT/NDI ») perd QM-1063, que la version écrite à la main contenait.
3. **Les citations sont dans une unité différente de l'interface.** Le LLM cite des passages, l'interface numérote des documents. Tous les passages mentionnant QP-1518, la ligne QP-1518 reçoit `[1]…[9]`, et la numérotation par première apparition devient déroutante. Le KA a cité 5 fois au total.
4. **Le format est plus lourd que la référence** : titres, tableaux, tableau final des documents qui double les pastilles, « Diffused » au lieu de « Diffusé ». Pourtant les instructions sont identiques à celles du KA.
5. **Les sorties varient d'une exécution à l'autre.** Deux exécutions identiques (température 0) ont donné une mise en forme différente (tableau ou liste pour les documents Level 3). Le KA lui-même n'a pas un format constant : sur les deux réponses KA disponibles, l'une finit par une liste « Documents référencés », l'autre non.

### Méthode retenue : pas de bricolage

Ajuster les prompts pour coller à une seule réponse du KA sur une seule question, c'est du sur-ajustement. À la place :

1. **Mesurer d'abord.** Un jeu de vraies questions avec les réponses du KA, et des critères mesurables :
   - documents cités par rapport au KA ;
   - justesse des citations ;
   - langue de la réponse ;
   - latence.
2. **Corriger la structure plutôt que le prompt, symptôme par symptôme :**
   - **Citations** : présenter au LLM des documents numérotés (passages regroupés par REF) plutôt que des passages. Il cite alors dans l'unité qu'affiche l'interface.
   - **Recherche** : interroger avec la question telle quelle et avec sa version française, puis fusionner. On ne dépend plus d'une seule formulation.
   - **Format** : le juger sur l'ensemble du jeu, pas sur un exemple.
3. **Ne garder que ce qui améliore la mesure.**

## Correction des écarts : méthode

**Principe : diagnostiquer avant de corriger.** Pour chaque question où VSI fait moins bien que le KA, on dispose de la trace complète de VSI (requête, passages retrouvés, réponse) et de la liste des documents cités par le KA. Ça permet de classer chaque écart :

| Catégorie | Comment la reconnaître | Leviers |
|---|---|---|
| **Écart de recherche** | Un document cité par le KA n'est pas dans nos passages | Langue de la requête (brute + française), nombre de passages, index de la division |
| **Écart de génération** | Le document est dans nos passages, mais la réponse ne l'utilise pas ou se trompe | Unité de citation (documents numérotés), insistance des instructions, modèle |
| **Pas un écart** | Le juge estime la réponse de VSI équivalente ou meilleure | Aucun |

**Règle :**
1. On classe toutes les questions.
2. On traite la catégorie la plus fréquente avec un seul levier structurel.
3. On relance le jeu entier.
4. On ne garde le changement que si le résultat global s'améliore.

On ne corrige jamais question par question : c'est ça, le bricolage.

**Exemple (test du 2026-10-06) :** QM-1063 manquait.
- Il était absent de nos passages, donc c'est un écart de recherche.
- Rang 13 en anglais, 8 avec la requête française écrite à la main ; absent avec la requête française générée.
- Levier candidat : chercher avec la question brute et avec sa version française, puis fusionner. À retenir seulement s'il améliore le jeu entier.

**Inconnu :** on ne sait pas si ces leviers suffiront à égaler le KA, dont la recherche reste opaque. Si des écarts de recherche persistent, le levier suivant serait le reranking, uniquement si la mesure le montre.
