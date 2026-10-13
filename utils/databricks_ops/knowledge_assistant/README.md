# Knowledge Assistant (Agent Bricks) — bugs constatés côté Databricks

Ce dossier gère le provisioning des Knowledge Assistants Qualibot (`ka_profiles.py`,
`provision_knowledge_assistant_job.py`). Ce README documente les dysfonctionnements
réels constatés sur le produit managé Databricks lui-même (Agent Bricks), pas sur
notre code applicatif — matière à remonter à Databricks.

Preuves complètes (scripts de reproduction, logs bruts, chiffres) dans
`utils/databricks_ops/evaluation/multilingual_audit/` et
`utils/databricks_ops/evaluation/chat_citation_feedback_loop.md`. Ce document en est
la synthèse consolidée.

| # | Problème | Sévérité | Statut |
|---|---|---|---|
| 1 | Routing non déterministe sur un Knowledge Assistant multi-sources | Élevée | Contourné (archi mono-source), pas corrigé côté Databricks |
| 2 | Collision d'ID de requête sur les sous-requêtes internes | Élevée | Réapparu en prod malgré le contournement du #1 |
| 3 | Provisioning de la fonctionnalité "Exemples" cassé sur certains KA (dont la prod) | Moyenne | API OK sur un KA neuf ; définitivement raté sur au moins 1 KA de prod |
| 4 | Biais de retrieval multilingue (reproduit hors KA en HYBRID direct, symptôme identique en prod) | Élevée | Contourné côté app (traduction), pas corrigé |
| 5 | Retrieval fragile sur les questions de comparaison en langage naturel | Élevée | Reproduit hors KA, mécanisme interne non observable |
| 6 | Rate-limit interne du KA sous charge concurrente (429 "Rate limit exceeded") | Élevée | Pas corrigé côté Databricks ; 2 bugs applicatifs qui aggravaient l'expérience ont été corrigés côté app |

---

## 1. Un Knowledge Assistant à plusieurs Knowledge Sources ne route pas fiablement

**Ce qui a été testé** (2026-07-21) : un Knowledge Assistant unique configuré avec 3
Knowledge Sources simultanées (index Vector Search AS / IS / ALL), avec un prompt
système lui donnant explicitement la règle de routage ("si la question porte le tag
`[Division: AS]`, interroge la source AS", etc.). Objectif visé : fusionner les 3
Knowledge Assistants de production actuels (un par division, chacun avec une seule
source) en un seul, plus simple à maintenir côté application — c'est l'app, pas le
Knowledge Assistant, qui choisit aujourd'hui l'endpoint à appeler.

**Protocole** : la même question, avec le même tag de division, envoyée 32 fois
(16 par division : 8 séquentielles + 8 concurrentes). Lecture de la trace
d'exécution (`databricks_options.return_trace`, span `docs`) pour voir quelle
Knowledge Source a réellement été interrogée par le Knowledge Assistant.

**Résultat chiffré** :

| Tag envoyé | Bonne source | Repli sur ALL (sûr) | **Mauvaise source** | Vide |
|---|---|---|---|---|
| `[Division: AS]` (16 appels) | 3 | 2 | **10 (62,5 %)** | 1 |
| `[Division: IS]` (16 appels) | 7 | 8 | 1 (6 %) | 0 |

Pour les questions AS, le Knowledge Assistant interroge la source de l'**autre**
division plus d'une fois sur deux. Chaque appel se termine en HTTP 200 avec des
documents valides et bien formés — **rien ne signale l'erreur**, ni exception, ni
code retour différent, ni champ dans la réponse. Le seul moyen de détecter le
problème est de comparer après coup la division des documents retournés au tag
demandé dans la question.

**Ce n'est pas un problème de concurrence** : taux comparable en séquentiel (6/8
mauvaise source pour AS) et en concurrent (4/8) — écarte l'hypothèse d'une course
entre sous-requêtes (voir §2) pour ce phénomène précis. Le choix de source est
simplement non déterministe, à charge égale.

**Conséquence pratique** : aucun levier côté appelant ne permet d'épingler la source
à interroger. Fusionner les 3 Knowledge Assistants de production en un seul
multi-sources **dégraderait** la fiabilité par rapport à l'architecture actuelle —
c'est précisément cette architecture (un Knowledge Assistant dédié par division,
une seule Knowledge Source chacun) qui a été retenue en production pour éviter ce
problème.

**Reconfirmé 2026-09-04, sur un Knowledge Assistant reconstruit à neuf** (l'ancien KA
de test pointait vers des index `_v2` supprimés depuis le fix de rétention du
2026-08-18, cf. mémoire projet — `ka-af52aa74-endpoint`/`qualibot_test_routing_3idx_v1`,
3 sources = les index `_v1` réels de production, aucun nouveau compute Vector Search
créé) : même protocole, 32 appels.

| Tag envoyé | Bonne source | Repli sur ALL (sûr) | **Mauvaise source** | Vide |
|---|---|---|---|---|
| `[Division: AS]` (16 appels) | 14 | 0 | **0 (0 %)** | 2 |
| `[Division: IS]` (16 appels) | 2 | 2 | **12 (75 %)** | 0 |

Le phénomène se reproduit intégralement, en pire, et l'asymétrie s'est **inversée** :
en juillet c'était AS qui était mal routé 62,5 % du temps, ici c'est IS à 75 %. Ce
n'est donc pas un biais fixe lié à une des deux divisions — c'est la sélection de
source elle-même qui reste non fiable, indépendamment de quel Knowledge Assistant
précis est testé ou de quelle division souffre le plus à un instant donné.
(`classify()` dans le script référencait encore le suffixe `_v2` — corrigé le même
jour pour rester agnostique au suffixe d'index, sans quoi tout appel correct était
silencieusement classé "mixed" au lieu de "correct-source".)

Script : `evaluation/multilingual_audit/probe_ka_routing_consistency.py`.

---

## 2. Sous-requêtes parallèles internes au KA : collision d'ID de requête

Problème distinct du §1 mais lié : touche le Knowledge Assistant lui-même quand il
interroge Vector Search en interne, **que ce soit plusieurs Knowledge Sources
hébergées sur le même endpoint Vector Search (cas d'origine, 2026-06-25 — 2 sources,
1 seul endpoint Vector Search partagé) ou une seule source qu'il découpe lui-même en
plusieurs recherches pour une question complexe (cas de production, 2026-07-16 —
1 seule source).** Point commun aux deux cas : plusieurs recherches internes partent
en même temps vers le MÊME endpoint Vector Search.

**Mécanique** : pour répondre à un tour de conversation, le Knowledge Assistant
managé peut lancer plusieurs recherches en parallèle contre l'endpoint Vector Search
(une par Knowledge Source à consulter, ou plusieurs sous-questions dérivées d'une
question complexe — le détail exact du découpage nous est invisible). Toutes ces
recherches parallèles **réutilisent le même identifiant de requête interne**
(`<uuid-du-tour>-0`). Databricks applique une garde anti-doublon par identifiant de
requête sur l'endpoint Vector Search — quand deux de ces recherches tournent en même
temps sous le même identifiant, la garde rejette la seconde :
`BAD_REQUEST: "Request id <uuid>-0 already running."`, comme si on avait envoyé deux
fois la même requête. Une seule des recherches survit par tour, au hasard — quand
c'est la mauvaise, l'utilisateur reçoit "aucun document trouvé" alors que le
document existe bel et bien dans l'autre source (ou l'autre sous-recherche)
recherchée en parallèle.

**Preuve que ce n'est pas un problème d'endpoint** (diagnostiqué le 2026-06-25,
`debug/ka_is_diagnosis.log` + `debug/ka_request_id_collision.log`) : appeler
directement l'endpoint d'embedding et l'index Vector Search avec un **identifiant de
requête forcé identique**, en concurrence (6-10 appels simultanés), ne déclenche
**jamais** l'erreur — 200 OK à chaque fois, sur les deux endpoints testés
séparément. Le garde-fou "already running" vit strictement à l'intérieur du client
de serving interne au Knowledge Assistant, sur des endpoints par ailleurs
parfaitement sains.

**Contourné, pas corrigé, et le contournement n'a pas tenu** : le passage à "un
Knowledge Assistant par division, une seule source chacun" (juin 2026) devait
éliminer le besoin de sous-requêtes parallèles. **Le 16/07/2026, l'incident s'est
reproduit en production** (`ka-7679a56e-endpoint`, endpoint réel, log applicatif à
l'appui) — la mono-source ne suffit donc pas à garantir l'absence de découpage
interne en sous-requêtes :

```
2026-07-16 09:56:08 UTC - server.services.streaming - WARNING - stream_chat KA
retrieval error on ka-7679a56e-endpoint: [ERROR] Vector search failed for
index 'chunks_index_v2': Failed to call Model Serving endpoint. Error message:
{"error_code":"BAD_REQUEST","message":"Request id a79d2215-84c6-43e3-945e-
e1151dc4f639-0 already running.\n"}
```

**Tentative de reproduction ciblée** (`probe_ka_request_collision.py`, en direct sur
l'endpoint de prod) : 12/12 appels séquentiels sans erreur ; en concurrent
(6 appels simultanés), un essai a donné 4/6 erreurs
`"Request is rejected due to heavy load. Please retry with backoff."` (message
apparenté, pas identique), un second essai immédiatement après 0/6. Dépend de la
charge réelle du moment sur l'endpoint partagé — non reproductible à la demande
depuis notre côté, ce qui est justement le point à poser à Databricks.

Le code applicatif surveille déjà ce message précis
(`server/services/streaming.py`, recherche de `'Vector search failed'` /
`'already running'` dans le flux de raisonnement du Knowledge Assistant) pour le
faire apparaître dans les logs — ça le rend visible, pas plus.

Scripts : `evaluation/multilingual_audit/probe_ka_request_collision.py`,
`probe_ka_endpoint_diagnostics.py`.

---

## 3. La fonctionnalité native "Exemples" : pas cassée partout, mais son provisioning l'est sur au moins un Knowledge Assistant de production

En cherchant un mécanisme d'exemples pour corriger des cas de mauvaise
citation/hallucination détectés via feedback utilisateur (chat), sans ajouter
d'infra propre (pas de nouvel endpoint à créer) :

- L'API native Databricks pour les exemples (`create_example` / `list_examples`,
  rattachée directement au Knowledge Assistant) a renvoyé une
  **`InternalError: "Failed to fetch internal resources"`** le 2026-07-30, et à
  nouveau le 2026-09-04 en reconfirmation sur un Knowledge Assistant existant. Pas
  de workaround identifié côté client à l'époque : erreur serveur, pas un problème
  de payload.
- Faute d'API d'exemples utilisable, deux contournements ont été testés à la place
  (voir `evaluation/chat_citation_feedback_loop.md` pour le détail complet) :
  injection few-shot brute dans l'historique de messages, et distillation LLM du
  feedback en "leçon" injectée en un seul tour. Résultats mitigés dans les deux cas
  — rien déployé en prod, ce n'est pas un vrai substitut à l'API native.

**Correction importante (2026-09-04)** : sur un Knowledge Assistant **flambant neuf**
(`ka-af52aa74-endpoint`, créé le jour même pour §1), l'API fonctionne — `create_example`
réussit en 2 s, l'exemple apparaît ensuite dans `list_examples`. **La fonctionnalité
n'est donc pas cassée en soi.** Ce qui est cassé : le premier `list_examples` sur ce
même KA, juste après sa création, est resté bloqué (aucune réponse, ni succès ni erreur)
pendant **~5 minutes** avant de finir par répondre `count=0`, sans aucun signal de
progression côté client — la ressource interne de retrieval d'exemples semble se
provisionner de façon asynchrone, lente, et invisible.

**Sur un Knowledge Assistant plus ancien (dont la production), ce provisioning ne
s'est manifestement jamais terminé.** Sur la vraie trace de production analysée en §5
(`tr-8413e827ab495cb447d8766f848646ff`, `ka-7679a56e-endpoint`), le span interne
`examples` (`EXAMPLES`, distinct du span `docs`/`RETRIEVER`) lève systématiquement :
```
ResourceDoesNotExist: Unity Catalog entity
__databricks_internal_catalog_tiles_arclight_3155144190025826.7679a56e_a5426ae239b94360.ka_7679a56e_examples_index
does not exist.
```
L'index Vector Search interne que Databricks provisionne pour la fonctionnalité
d'exemples de CE Knowledge Assistant (en production, live depuis avril 2026) n'existe
tout simplement pas — l'exception est avalée silencieusement (le tour continue,
`examples` renvoie `None`, rien dans les logs applicatifs ni dans la réponse à
l'utilisateur), confirmé sur cet appel réel donc très probablement sur tous les appels
à cet endpoint depuis sa création.

**Conclusion révisée** : la fonctionnalité "Exemples" fonctionne quand son
provisioning aboutit, mais (a) ce provisioning est lent et totalement opaque côté
client (aucun état "en cours" observable, juste un appel qui pend), et (b) au moins un
Knowledge Assistant réel de production tourne depuis des mois avec ce provisioning
définitivement raté, sans qu'aucune erreur ne remonte nulle part côté application —
seule la trace MLflow brute (voir §5) révèle le problème, et seulement si on va la
chercher.

---

## Autre anomalie constatée en creusant #3

`get_knowledge_assistant` sur le Knowledge Assistant de production
(`ka-7679a56e-endpoint`) a renvoyé **`state: "FAILED"`** avec
`error_info: "Vector search endpoint failed to provision (status=OFFLINE)"` au
même moment où cet assistant répondait normalement à toutes les requêtes de test.
Le statut exposé par l'API ne reflète pas l'état réel de fonctionnement du
service — probable reliquat d'une mise à jour de config bloquée, jamais creusé
séparément.

---

## 4. Biais de retrieval multilingue — reproduit sur Vector Search direct, symptôme identique (mais non prouvé identique en mécanisme) sur le Knowledge Assistant

**Distinction importante** : le Knowledge Assistant fait du "retrieval agentique" —
il décide lui-même, en interne, comment interroger ses sources, sans qu'on puisse
observer ni régler ce mécanisme (query_type, poids mot-clé/vecteur, etc.). Ce qui
suit a été **mesuré directement sur l'index Vector Search sous-jacent, en le
questionnant nous-mêmes** (paramètre `query_type=HYBRID`, celui qu'on contrôle) —
PAS en observant ce que fait le Knowledge Assistant en interne. On a ensuite observé
le même symptôme final (des sources en moins pour une langue donnée) en passant par
le vrai Knowledge Assistant de production, mais sans preuve que le mécanisme interne
soit identique au `HYBRID` qu'on a testé nous-mêmes — seulement que le symptôme
concorde.

Une même question posée en fr/en/es retourne des documents presque totalement
différents (indice de Jaccard souvent à 0,00) quand on interroge nous-mêmes l'index
en `HYBRID` ou en `ANN` (vecteur pur), sur le vrai corpus. Isolé au niveau du modèle
d'embeddings seul (Qwen3-0.6B) : il aligne correctement le sens entre langues
(similarité moyenne 0,75 même-sens/langues différentes vs 0,36 même-langue/sens
différent) — **le modèle n'est pas en cause**.

Sur le Knowledge Assistant de production (ce qu'un vrai utilisateur vit) : une
question espagnole obtient **0 source citée**, contre 2-3 en français/anglais, pour
le même besoin d'information — le même symptôme que sur notre test direct.
Hypothèse (non confirmée côté Databricks) : le corpus est majoritairement fr/en ; en
`HYBRID`, la composante mot-clé (BM25) ne trouve aucun recouvrement lexical entre une
question espagnole et un document fr/en, ce qui tire le score combiné vers les rares
documents natifs en espagnol — pertinents ou non pour l'utilisateur. Si le Knowledge
Assistant utilise en interne un mécanisme comparable, ça expliquerait le symptôme
observé côté vrais utilisateurs — mais on n'a aucun moyen de le vérifier. Aucun
réglage du poids mot-clé/vecteur, ni de mode vecteur pur (`ANN`), n'est exposé au niveau du
Knowledge Assistant (seulement en appelant l'API Vector Search directement, en
dehors du Knowledge Assistant).

**Mitigation actuelle, pas une correction** : `server/services/translation_bridge.py`
traduit toute question non fr/en vers l'anglais avant le Knowledge Assistant, puis
retraduit la réponse — un contournement avec un coût/latence réel et qui ne
résout rien quand le document correspondant n'existe simplement pas dans la
langue de la question. Désactivé en production.

Détail complet, méthodologie, artifact avec tableaux :
`evaluation/multilingual_audit/README.md` (section 1) et l'artifact lié dedans.
Non-régression : `evaluation/eval_rag_vs_agent.py` (`LANGUAGE_PARITY_QUESTIONS`).

---

## 5. Une question de comparaison en langage naturel fait échouer le retrieval — même quand les deux documents sont individuellement trouvables

Trouvé en creusant un cas réel (2026-09-03, session `72b28d89-c569-4c6d-b918-2d91ea2ce217`,
division `ALL`, endpoint de production `ka-7679a56e-endpoint` — **un seul Knowledge
Assistant mono-source, aucun multi-sources en jeu ici**, donc un problème distinct des
§1/§2) :

**Message 2913** (utilisateur) : *"quelles sont les différences entre NF-10845 et
NS-1868"*.

**Message 2914** (assistant) : refus complet — *"Je n'ai pas trouvé d'informations dans
les documents fournis concernant les références NF-10845 et NS-1868. Les résultats de
recherche ne contiennent pas ces documents."*

**Confirmé sur la vraie trace de production** (`tr-8413e827ab495cb447d8766f848646ff`,
récupérée depuis l'expérience MLflow `4171178917767011` de cet endpoint — voir la note
méthodologique en fin de section) : le span `docs` (`RETRIEVER`) de cet appel réel a
retourné exactement 10 chunks, **aucun ne référence NF-10845 ni NS-1868** :
`NS-1683` (×3, quasi-homonyme de NS-1868), `DTAS.00.010`, `IQ_14_251`, `QP-1151` (×2),
`GO-1264`, `GO-1265` (×2). **Le retrieval a réellement échoué** — la réponse du modèle
était honnête, pas une hallucination de refus malgré des sources trouvées.

**La suite de la conversation confirme que les deux documents sont pourtant bien
indexés et individuellement trouvables** : décomposée en deux questions séparées ("il y
a quoi avec 10845", puis "et 1868?"), chacune obtient une réponse complète et
correctement sourcée (message 2916 pour NF-10845, message 2918 pour NS-1868). Une fois
les deux documents "chargés" dans la conversation, "quelles sont les différences ?"
(message 2920) obtient enfin une comparaison complète et correcte.

**Comparaison avec un RAG classique** (requête directe sur `chunks_index_v1`, `HYBRID`,
sans passer par le Knowledge Assistant — même principe que `eval_rag_vs_agent.py`),
testée le 2026-09-04, qui reproduit le même échec :

| Requête envoyée telle quelle | NF-10845 dans le top 10 ? | NS-1868 dans le top 10 ? |
|---|---|---|
| `quelles sont les differences entre NF-10845 et NS-1868` (question complète) | Non | Non |
| `NF-10845 et NS-1868` (codes seuls, sans habillage) | Oui (rang 5) | Oui (rang 3) |
| `NF-10845` seul | Oui (rang 2) | — |
| `NS-1868` seul | — | Oui (rang 2, 4, 5) |
| `il y a quoi avec 10845` (texte réel tapé par l'utilisateur) | **Non** | — |

La formulation "quelles sont les différences entre X et Y" dilue le score `HYBRID` au
point de faire disparaître les deux codes REF qu'elle contient pourtant explicitement —
réduire la requête aux codes bruts suffit à les faire réapparaître. **C'est exactement
le même mécanisme qui a fait échouer le vrai retrieval du Knowledge Assistant** (les 10
chunks réels ci-dessus sont dominés par des REF phonétiquement/lexicalement proches
comme `NS-1683`, pas par NF-10845/NS-1868).

Reste un point non expliqué : sur le message 2916 ("il y a quoi avec 10845", texte réel
de l'utilisateur, non reformulé), le Knowledge Assistant a produit une réponse détaillée
et correcte — alors qu'un `HYBRID` direct sur ce même texte ne retrouve PAS NF-10845
dans le top 5 non plus. Le Knowledge Assistant fait donc autre chose qu'un simple
`HYBRID` sur le texte brut dans au moins ce cas (reformulation interne ? extraction du
code ?), sans qu'on puisse observer comment ni pourquoi ce mécanisme, capable de mieux
faire sur une question à un seul REF, échoue sur la question à deux REF posée en une
seule fois plutôt que de la décomposer lui-même en deux sous-requêtes (comme il le fait
apparemment ailleurs, voir §2). Aucune visibilité côté client sur ce mécanisme de
reformulation, ni sur pourquoi il n'a pas été appliqué (ou a échoué) sur la question à
deux REF.

**Note méthodologique — comment la trace réelle a été récupérée** : `mlflow.get_trace(trace_id)`
seul ne suffit pas (l'ID doit être résolu dans l'expérience MLflow de l'endpoint —
`4171178917767011` pour `ka-7679a56e-endpoint`/ALL, voir `DIVISION_EXPERIMENT_IDS` dans
`evaluation/mlflow_genai_eval_qualibot_uat.py`) ; `mlflow.search_traces(experiment_ids=[...],
filter_string="timestamp_ms > X AND timestamp_ms < Y")` autour de l'horodatage du message
retrouve les traces candidates (le filtre ne supporte pas `trace_id` directement) — vérifier
ensuite le texte de la question dans chaque trace pour confirmer la bonne. Sur ce poste, le
téléchargement de l'artefact de trace (signé S3) échoue par défaut
(`SSLError: self-signed certificate in certificate chain`, proxy d'entreprise local) — un
patch de `requests.Session.request` pour forcer `verify=False` le temps du script contourne
ça. Les traces ne sont donc PAS irrécupérables après coup, contrairement à ce qu'une
première tentative trop rapide avait conclu à tort plus tôt dans cette investigation — à
refaire systématiquement avant de conclure qu'un cas n'est "pas vérifiable".

---

## 6. Rate-limit interne du KA sous charge concurrente — 429 "Rate limit exceeded"

Trouvé en stress-testant `qualibot-prod` (2026-09-14/15, endpoint réel, 20-50 tours de
chat concurrents via `/api/chat/ws`). Trois choses distinctes se sont mélangées avant
d'être démêlées ; seules les deux dernières restent des problèmes ouverts.

**Écarté : la Vector Search elle-même n'est pas le goulot.** 50 requêtes concurrentes
envoyées directement sur `chunks_index` (`query_index`, sans passer par le KA) : 50/50
OK en 2.16 s. Le ralentissement/échec apparaît seulement en passant par le KA.

**Bug applicatif #1 (corrigé, commit `53cd1ed`)** : `chat_ws` ne relayait jamais au
client WebSocket les commentaires SSE keepalive (`: keepalive\n\n`) que `stream_chat`
envoie déjà côté serveur toutes les 15 s. Un tour de chat lent (queue, retry) laissait
le WebSocket silencieux assez longtemps pour qu'un timeout d'inactivité (proxy en
amont, ~30 s) coupe la connexion avant tout message `done`/`error` — `websockets`
lève `ConnectionClosedOK` côté client, sans aucune explication. Fix : relayer un
ping léger (`{"type": "ping"}`) à chaque keepalive reçu.

**Bug applicatif #2 (corrigé, commit `d90aea2`)**, découvert après le fix #1 (les
coupures ont chuté de 42 % à 0 % des tentatives mais pas disparu) : le KA renvoie
parfois en plein flux un événement d'erreur explicite —
`{"type": "error", "code": "429", "message": "Rate limit exceeded. Please try again
later."}` — après avoir déjà répondu HTTP 200. `stream_chat` ne traitait aucun type
`"error"` (branche générique log-only), donc `chat_ws` recevait 0 delta sans signal
d'erreur non plus, et fermait la socket en silence — exactement le même symptôme
`ConnectionClosedOK` observé côté client, mais cette fois pour une vraie raison
applicative distincte du timeout. Fix : `stream_chat` relaie maintenant proprement
ce type d'événement, `chat_ws` le reçoit via son handler `error` déjà existant.

**Restant, non corrigé côté app (comportement de la plateforme managée) : le KA
répond 429 sous charge concurrente, de façon non déterministe.** Mesures après les
deux fixes ci-dessus, sémaphore côté client + retry (jitter 1-3 s, 3 tentatives) :

| Test | Concurrence | Résultat |
|---|---|---|
| Rafale brute, 1 endpoint (ALL) | 20 | 65-75 % succès, très variable d'un run à l'autre (35 % un jour, 75 % un autre, même charge) |
| Sémaphore(12)+retry, 1 endpoint | 40 (12 en vol) | 70-82 % succès selon les runs |
| Sémaphore(10)+retry, **2 endpoints** (ALL+AS) simultanés | 30+30 | **45 % combiné — pire que 1 seul endpoint**, ~20-25 req/min réussies max |

Diviser la charge sur 2 Knowledge Assistants différents n'améliore pas le débit
(voir tableau) — les deux se dégradent ensemble, ce qui va contre l'hypothèse d'une
limite strictement par-endpoint. Cause exacte non identifiable de notre côté (boîte
noire Agent Bricks) ; à poser à Databricks avec les chiffres ci-dessus.

**Retry côté app** : prototypé uniquement dans des scripts de test, jamais implémenté
dans `chat.py`/`streaming.py` — décision volontaire, en attente de validation de
l'approche avant de l'ajouter pour de vrai.

Scripts de reproduction : uniquement dans le scratchpad de la session (non versionnés
dans le repo) — `stress_test.py`, `concurrency_threshold_test.py`,
`deep_dive_connection_closed.py`, `two_agents_volume_test.py`. À rapatrier dans
`evaluation/` si on veut les rejouer plus tard.

---

## Reproductibilité

Tous les scripts sont en lecture seule (aucune requête n'écrit de données), via
`databricks.sdk.core.Config(profile=PROFILE)` (par défaut `UAT`, nécessite
`databricks auth login --profile UAT` au préalable) :

- `../evaluation/multilingual_audit/probe_ka_routing_consistency.py` — §1.
- `../evaluation/multilingual_audit/probe_ka_request_collision.py` — §2.
- `../evaluation/multilingual_audit/probe_ka_endpoint_diagnostics.py` — diagnostic
  de trace bas niveau (spans `docs`/`examples`), utile en amont de §1 et §3.
- `../evaluation/multilingual_audit/probe_embedding_geometry.py`,
  `probe_index_language_bias.py` — §4.

Repères techniques :
- Endpoint de production (§2, §3, anomalie d'état) : `ka-7679a56e-endpoint`
  (`qualibot_ALL_v2`).
- Knowledge Assistant de test à 3 Knowledge Sources (§1) et de test "Exemples" (§3) :
  `qualibot_test_routing_3idx_v1` (`ka-af52aa74-endpoint`), créé et supprimé le
  2026-09-04 une fois la vérification faite (comme son prédécesseur
  `ka-087d89b6-endpoint`/`qualibot_test_routing_3idx`, supprimé le même jour — ses
  index `_v2` sous-jacents n'existaient plus depuis le fix de rétention du
  2026-08-18). Recréer avec le même protocole (3 `KnowledgeSource` de type `index`
  pointant sur `chunks_{as,is}_index_v1`/`chunks_index_v1`, déjà en production —
  aucun nouveau compute Vector Search) pour toute vérification future.


## Résumé à envoyer à Databricks

Version condensée, prête à coller dans un mail : `DATABRICKS_SUPPORT_EMAIL.md`
(même dossier).
