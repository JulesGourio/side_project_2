# Audit retrieval multilingue Qualibot — tout ce qui ne fonctionne pas

Compilation des problèmes trouvés le 2026-07-21 en auditant le comportement
multilingue du retrieval Qualibot (Vector Search + Knowledge Assistant).
Sert de base à une demande d'explication à Databricks (brouillon plus bas).

Contexte : Qualibot est en phase de test à l'international chez Latécoère
(Mexique, Bulgarie, Tchéquie…) — le caractère multilingue de la base
documentaire n'est donc pas un cas limite ponctuel mais une exigence réelle
et actuelle du déploiement.

Trois problèmes distincts, avec un point commun : le Knowledge Assistant
délègue à des composants internes Databricks (le scoring `HYBRID`, la
sélection de source documentaire, l'exécution des sous-requêtes internes)
qui n'exposent aucun réglage ni garantie côté client :

1. **Biais linguistique du retrieval** — une question posée hors fr/en perd
   ses sources, à cause du mode `HYBRID` et non du modèle d'embeddings.
2. **Sélection de source non déterministe, quand un Knowledge Assistant a
   plusieurs sources documentaires** — même quand la division est précisée
   explicitement dans la question, l'assistant interroge parfois la
   mauvaise source, sans qu'aucune erreur ne soit levée.
3. **Échecs "Vector search failed" en production, sous charge** — un
   incident réel du 16/07/2026 sur l'endpoint de production montre le
   Knowledge Assistant échouer à retrouver quoi que ce soit pour un tour de
   conversation, avec une erreur explicite cette fois (contrairement au
   point 2) — déjà partiellement diagnostiqué fin juin, pas résolu.

---

## 1. Biais linguistique du retrieval

Déclencheur : feedback du 13/07/2026 — une question posée en espagnol sur
l'obligation de tampon pour un magasinier a dérivé, au tour suivant, vers un
document de site non pertinent ; l'utilisateur a signalé « Esa IF no aplica
a mi pais » (« cette IF ne s'applique pas à mon pays »).

Rapport détaillé (tables, méthodologie complète) : artifact du 2026-07-21
[Biais linguistique — Qualibot](https://claude.ai/code/artifact/e859ca51-eea0-443f-b54c-f0738023fd88).
Résumé des trois tests :

1. **Géométrie de l'embedding seul** (`probe_embedding_geometry.py`) — Qwen3-0.6B
   aligne correctement le sens entre fr/en/es pris isolément (similarité
   moyenne 0.75 pour une paire même-sens-langues-différentes, contre 0.36
   pour même-langue-sens-différent ; plus proche voisin correct pour les 5
   phrases espagnoles testées). **Le modèle d'embeddings n'est pas en cause.**
2. **Index réel, HYBRID vs ANN** (`probe_index_language_bias.py`) — sur le
   vrai corpus, le recouvrement des documents remontés entre fr/en/es pour la
   même question est quasi nul (indice de Jaccard souvent à 0.00), en HYBRID
   comme en ANN. Le document correct pour le concept "tampon magasinier"
   (`MI-1331-GB`) n'est retrouvé que par la question **anglaise** ; le
   français et l'espagnol remontent d'autres documents, dont des formulaires
   propres à un site (`_MX`) pour l'espagnol — exactement le symptôme du
   feedback déclencheur.
3. **Knowledge Assistant de production** — la question espagnole brute
   obtient **0 source citée**, contre 2-3 en français/anglais. Pré-traduire
   la question en anglais avant l'appel au Knowledge Assistant restaure le
   sourçage.

**Cause exacte** : le corpus est majoritairement fr/en ; les rares documents
natifs dans d'autres langues sont concentrés sur des instructions de site.
Le `query_type` de l'index Vector Search sous-jacent ne propose que `HYBRID`
ou `FULL_TEXT` — pas de réglage du poids mot-clé (BM25) / vecteur, et pas de
mode vecteur pur (`ANN`) accessible depuis le Knowledge Assistant lui-même
(seulement en appelant l'API Vector Search directement, en dehors du
Knowledge Assistant, comme dans les scripts de ce dossier). En `HYBRID`, la
composante mot-clé ne trouve aucun recouvrement lexical entre une question
espagnole et un document fr/en, ce qui tire le score combiné vers les
quelques documents natifs en espagnol — pertinents ou non pour le pays de
l'utilisateur. Rien, côté client, ne permet de corriger ce déséquilibre.

**Suivi mis en place** : `utils/databricks_ops/evaluation/eval_rag_vs_agent.py`
contient maintenant un jeu `LANGUAGE_PARITY_QUESTIONS` (3 concepts × fr/en/es,
repris de cet audit) et une section "Language parity" dans le rapport HTML
généré — une ligne rouge signale qu'une langue obtient 0 source citée quand
une autre langue en obtient. À rejouer périodiquement.

**Mitigation actuelle et limites** : `server/services/translation_bridge.py`
(commit 2026-07-08/09) traduit toute question non fr/en vers l'anglais
avant d'atteindre le Knowledge Assistant, puis retraduit la réponse dans la
langue d'origine — un contournement, pas une correction du retrieval
lui-même. Il reste trois limites : un appel LLM supplémentaire à chaque
tour de conversation (latence, coût) ; un risque de perte de fidélité sur
les termes techniques ou les codes de référence documentaire lors de
l'aller-retour de traduction ; et surtout, ça ne résout rien quand le
contenu correspondant n'existe tout simplement pas dans la langue de la
question — traduire ne fait pas apparaître un document qui n'a jamais été
traduit. Désactivé par défaut, activé seulement sur les environnements de
test — pas en production (dont la config chat multilingue est de toute
façon encore à finaliser).

---

## 2. Sélection de source non déterministe sur un Knowledge Assistant multi-sources

### Rappel : qu'est-ce qu'un Knowledge Assistant, et une Knowledge Source

Un **Knowledge Assistant** (produit Databricks Agent Bricks) est un service
managé : on lui donne des instructions (un prompt système) et une ou
plusieurs **Knowledge Sources**, chacune étant un pointeur vers un index
Vector Search (colonne texte, colonne URL de document, description). Le
tout est packagé derrière un unique endpoint de serving, appelé comme un
modèle de chat (API Responses).

**Notre architecture de production actuelle évite volontairement le
problème décrit ici** : elle utilise **un Knowledge Assistant par
division** (un pour AS, un pour IS, un troisième "ALL" pour le combiné) —
chacun avec une **seule** Knowledge Source. C'est l'application (pas le
Knowledge Assistant) qui choisit l'endpoint à appeler selon la division
sélectionnée par l'utilisateur. Il n'y a donc, dans ce schéma, aucune
décision de routage interne à faire : chaque Knowledge Assistant n'a qu'un
seul endroit où chercher.

### Ce qu'on a testé

L'idée testée ici est différente : un **seul** Knowledge Assistant, mais
configuré avec **trois Knowledge Sources en même temps** — un index
Vector Search contenant uniquement les documents AS, un contenant
uniquement les documents IS, et un troisième contenant l'ensemble combiné
(ALL), utilisé en repli quand la question est transversale ou ambiguë. Le
prompt système lui indique explicitement, par instruction, quelle Knowledge
Source utiliser selon un tag de division présent dans la question
(`[Division: AS]` ou `[Division: IS]`). L'intérêt d'un tel montage, s'il
fonctionnait, serait de fusionner les 3 endpoints de production actuels en
un seul Knowledge Assistant qui route lui-même — plus simple à maintenir
côté application.

**C'est ce choix de Knowledge Source, fait par le Knowledge Assistant lui-même
à chaque tour de conversation, qui s'avère peu fiable.**

### Le test et les chiffres

La même question, avec le même tag de division, est envoyée 32 fois (16 par
division : 8 appels séquentiels, 8 appels concurrents). Pour chaque appel,
on lit dans la trace d'exécution retournée par l'API
(`databricks_options.return_trace`, span `docs`) laquelle des 3 Knowledge
Sources a effectivement été interrogée, et la division des documents
obtenus :

| | bonne Knowledge Source | repli sur la source ALL (sûr) | **mauvaise Knowledge Source** | vide |
|---|---|---|---|---|
| `[Division: AS]` (16 appels) | 3 | 2 | **10 (62,5 %)** | 1 |
| `[Division: IS]` (16 appels) | 7 | 8 | 1 (6 %) | 0 |

« Mauvaise Knowledge Source » = le Knowledge Assistant a interrogé la
source de l'**autre** division — une question taguée AS n'a retourné que
des documents de la Knowledge Source IS, aucun document AS. Pour les
questions AS, c'est arrivé **plus d'une fois sur deux** — pas un cas limite.

**Mécanique exacte, pour être précis (il ne s'agit pas d'une erreur
technique)** : chaque appel se termine avec succès — HTTP 200, documents
valides et correctement formés, avec leurs métadonnées. Rien ne lève
d'exception, rien n'échoue côté API. Le problème est un choix silencieux :
à chaque tour, une étape interne au Knowledge Assistant décide laquelle des
3 Knowledge Sources attachées interroger, et cette décision n'est pas
déterministe — pour une question et un tag de division strictement
identiques, elle change d'un appel à l'autre. On ne le détecte qu'en
comparant, après coup, la division des documents retournés au tag de
division demandé dans la question — jamais par un message d'erreur, un
code retour différent, ou un signal quelconque dans la réponse elle-même.

**Ce n'est pas un effet de la concurrence** : le taux d'erreur est
comparable en séquentiel (6/8 mauvaise Knowledge Source pour AS) et en
concurrent (4/8) — l'hypothèse de départ (course de requêtes, cf. mémoire
`is-source-vector-search-race` — un vrai bug de concurrence déjà rencontré
ailleurs, symptôme "Request id already running") est donc écartée. Ce qui
reste : dès qu'un Knowledge Assistant a plusieurs Knowledge Sources
candidates, la sélection de la bonne n'est simplement pas fiable,
indépendamment de la charge.

**Conséquence** : rien, côté client, ne permet de forcer ou d'épingler
quelle Knowledge Source doit être interrogée pour un appel donné — le seul
levier disponible (le tag de division dans le texte de la question) n'est
pas suffisant pour garantir le bon routage. En l'état, fusionner les 3
Knowledge Assistants de production en un seul avec 3 Knowledge Sources
dégraderait la fiabilité par rapport à l'architecture actuelle (un
Knowledge Assistant dédié par division, une seule Knowledge Source chacun).

---

## 3. Échecs "Vector search failed" en production, sous charge

### L'incident réel qui a motivé ce test

Extrait des logs applicatifs de production, 16/07/2026 :

```
2026-07-16 09:56:08 UTC - server.services.streaming - WARNING - stream_chat KA
retrieval error on ka-7679a56e-endpoint: [ERROR] Vector search failed for
index 'chunks_index_v2': Failed to call Model Serving endpoint. Error message:
{"error_code":"BAD_REQUEST","message":"Request id a79d2215-84c6-43e3-945e-
e1151dc4f639-0 already running.\n"}
```

C'est sur `ka-7679a56e-endpoint` — l'endpoint **de production**, pas un
endpoint de test. Ce message ("Request id ... already running") n'est pas
nouveau : il a déjà été diagnostiqué le 25/06/2026 (mémoire
`is-source-vector-search-race`) — le Knowledge Assistant découpe une
question complexe en plusieurs sous-requêtes internes exécutées en
parallèle, pour un seul et même tour de conversation, et **réutilise le même
identifiant de requête** pour ces sous-requêtes. Elles se percutent alors
entre elles côté Databricks avec `BAD_REQUEST: "Request id ...-0 already
running"`. Une seule des sources survit par tour, au hasard.

À l'époque, ce problème avait été contourné (pas corrigé) en passant à une
architecture "un Knowledge Assistant par division, une seule Knowledge
Source chacun" (cf. point 2 ci-dessus) — l'idée étant qu'avec une seule
source, il n'y a plus besoin de sous-requêtes parallèles internes. **Le log
du 16/07 montre que ça n'a pas suffi** : l'incident s'est reproduit trois
semaines plus tard, sur l'endpoint de production censé en être protégé.

Le code applicatif surveille déjà spécifiquement ce message
(`server/services/streaming.py`, recherche de `'Vector search failed'` ou
`'already running'` dans le flux de raisonnement du Knowledge Assistant)
pour le faire apparaître dans les logs — mais ça ne fait que le rendre
visible, pas disparaître.

### Ce qu'on a testé, et ce qu'on a (et n'a pas) reproduit

Aucun script du dossier ne testait ce point avant — corrigé avec
`probe_ka_request_collision.py`, contre `ka-7679a56e-endpoint` directement
(en streaming, comme l'app), avec des questions à plusieurs volets
(susceptibles de pousser le Knowledge Assistant à se découper en
sous-requêtes).

- **En séquentiel** (un seul appel à la fois, aucune charge générée par le
  script) : aucune erreur sur 12 appels au total (2 séries de 6).
- **En concurrent** (6 appels envoyés en même temps) : un premier essai a
  déclenché `[ERROR] Vector search failed for index 'chunks_index_v2':
  Request is rejected due to heavy load. Please retry with backoff.` sur
  4 appels sur 6 ; un second essai, immédiatement après, n'en a déclenché
  **aucun** (0/6).

Cette variation est en soi une information : elle correspond à une
limitation de débit dépendante de la charge réelle du moment sur l'endpoint
partagé (présente quand autre chose sollicite l'endpoint en même temps,
absente sinon), pas à un bug qu'on peut déclencher à volonté depuis un
script isolé. On n'a donc **pas reproduit exactement** le message précis du
16/07 (`"Request id ...-0 already running"`) — seulement un message
apparenté (`"heavy load"`), capté par le même filtre de log applicatif.
Les deux sont réels, les deux sont invisibles pour l'utilisateur final
(l'assistant répond quand même, juste sans certaines sources), et aucun des
deux n'est reproductible à la demande depuis notre côté — ce qui est
justement le point à poser à Databricks.

---

## Brouillon de mail à Databricks

Contient un troisième point (déploiement de modèles en Serving) sans rapport
avec l'audit retrieval — une question distincte de Jules regroupée dans le
même message plutôt qu'envoyée séparément. Le point 3 ci-dessus (échecs
Vector Search sous charge) n'est pas encore repris dans le brouillon
ci-dessous — à ajouter si besoin avant l'envoi.

> Bonjour l'équipe Databricks !
>
> Désolé de vous déranger, j'avais quelques questions pour mes cas d'usage
> (surtout sur Qualibot).
>
> Qualibot est bien en phase de test à l'international chez Latécoère
> (Mexique, Bulgarie, Tchéquie…), et je me suis rendu compte de plusieurs
> problèmes. Notre base de connaissances est multilingue — la majorité des
> documents n'existent que dans une seule langue, mais une partie non
> négligeable est traduite dans plusieurs — ce qui pose plusieurs problèmes.
>
> **1. Un biais de retrieval multilingue, sans levier de réglage.** Une même
> question posée en français, anglais ou espagnol retourne des documents
> presque totalement différents (recouvrement quasi nul), en HYBRID comme en
> ANN. J'ai isolé le modèle d'embeddings et constaté qu'il aligne correctement
> le sens entre langues pris isolément : le biais vient de la composante
> mot-clé de HYBRID, qui ne peut pas matcher une question non-fr/en contre des
> documents fr/en, combinée au déséquilibre linguistique du corpus. Sur un
> Knowledge Assistant, une question espagnole qui obtient 2-3 sources citées
> en français/anglais en obtient zéro en espagnol. La configuration exposée
> ne permet ni de régler le poids mot-clé/vecteur de HYBRID, ni de forcer un
> mode vecteur pur au niveau du Knowledge Assistant.
>
> Actuellement, je contourne ça en traduisant les questions et les réponses
> à la volée (pivot anglais), mais ça laisse des problèmes : un appel LLM
> supplémentaire à chaque tour (latence, coût), un risque de perte de
> fidélité sur les termes techniques ou les codes de référence documentaire,
> et surtout ça ne résout rien quand le document correspondant n'existe tout
> simplement pas dans la langue de la question.
>
> **2. Sélection de Knowledge Source non déterministe.** Sur un Knowledge
> Assistant configuré avec trois Knowledge Sources (une par division chez
> Latécoère — AS/IS —, plus une combinée), la même question avec un tag de
> division explicite, envoyée 16 fois à l'identique, interroge la mauvaise
> Knowledge Source (uniquement des documents de l'autre division, aucune
> erreur levée) dans 10 cas sur 16 (62,5 %) — taux comparable en séquentiel
> et en concurrent, donc pas un problème de charge. Aucun moyen d'épingler la
> source côté appelant.
>
> J'aimerais aussi savoir comment se déroule le déploiement des modèles en
> Serving sur Databricks : certains modèles récents sont déployés en Serving
> alors que d'autres non (comme Claude Sonnet 5.0) — y a-t-il des étapes
> particulières à suivre de notre côté, ou est-ce plutôt lié à la région du
> workspace ?
>
> Merci pour votre temps et vos réponses, je reste disponible pour plus de
> précisions ou un appel sur le sujet. Bonne journée.
>
> Cordialement,
> Jules Gourio

---

## Reproductibilité

Scripts dans ce dossier (lecture seule — aucune requête n'écrit de données),
tous utilisant `databricks.sdk.core.Config(profile=PROFILE)` (par défaut
`UAT`, nécessite `databricks auth login --profile UAT` au préalable) :

- `probe_embedding_geometry.py [PROFILE]` — problème 1, géométrie de
  l'embedding.
- `probe_index_language_bias.py [PROFILE]` — problème 1, index réel
  HYBRID/ANN.
- `probe_ka_endpoint_diagnostics.py [ENDPOINT] [PROFILE]` — diagnostic de
  trace bas niveau (spans `docs`/`examples`), utile en amont du test de
  routage pour vérifier qu'un Knowledge Assistant a bien des Knowledge
  Sources fonctionnelles avant de mesurer son taux de mauvais routage.
- `probe_ka_routing_consistency.py [ENDPOINT] [PROFILE]` — problème 2,
  quantifie le taux de mauvaise sélection de Knowledge Source sur un
  Knowledge Assistant multi-sources.
- `probe_ka_request_collision.py [PROFILE]` — problème 3, tente de
  reproduire les échecs "Vector search failed" sous charge sur l'endpoint de
  production (`ka-7679a56e-endpoint`), en séquentiel puis en concurrent.

Le test de non-régression "parité de langue" côté Knowledge Assistant de
production est dans `../eval_rag_vs_agent.py` (`LANGUAGE_PARITY_QUESTIONS` +
section "Language parity" du rapport HTML), pas dupliqué ici.

## Repères techniques

- Endpoint de production (problème 1) : `ka-7679a56e-endpoint`.
- Knowledge Assistant de test à 3 Knowledge Sources utilisé pour le
  problème 2, **temporaire, à supprimer une fois la vérification finie** :
  `ka-087d89b6-endpoint` (display name `qualibot_test_routing_3idx`, id
  `087d89b6-a191-4886-b144-acab1555176d` — recréé de zéro le 21/07/2026 avec
  les index `_v2` à jour ; un précédent Knowledge Assistant de test pointait
  vers des index entretemps renommés). Suppression :
  `databricks knowledge-assistants delete-knowledge-assistant "knowledge-assistants/087d89b6-a191-4886-b144-acab1555176d" --profile UAT`
- Index Vector Search actuels (endpoint `qualibot`) : `chunks_index_v2` (ALL),
  `chunks_as_index_v2` (AS), `chunks_is_index_v2` (IS).
