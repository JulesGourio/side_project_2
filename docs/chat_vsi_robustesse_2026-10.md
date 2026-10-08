# Chat VSI — robustesse : modèle de secours, relances, langue (2026-10-08)

Objectif : le chatbot répond toujours, même quand un modèle est saturé, absent ou coupe sa
réponse. Plan retenu : **GPT-6 Luna** pour répondre, **GPT-5.6 Luna** en secours.

Le code est poussé. Rien n'a changé dans l'app déployée : le basculement de modèle reste
inactif tant que `CHAT_VSI_LLM_FALLBACK_ENDPOINTS` est vide (valeur par défaut). La mise en
service est le bloc L de `operations_dev.md`.

## 1. Ce qui se passe maintenant à chaque question

| Étape | Avant | Maintenant |
|---|---|---|
| Langue de la question | fastText sur le texte brut ; un code document (`QP-1518`) pouvait être pris pour du polonais | Codes, numéros, liens, marqueurs `[n]` retirés avant la détection ; un code seul n'est plus « deviné » |
| Traduction (pont) | 1 modèle (GPT-5.6 Luna), relance sur 5xx seulement | Relance aussi sur 429, puis GPT-6 Luna en secours |
| Réécriture de la requête | 1 modèle ; s'il échoue ou renvoie du vide, recherche avec la question seule | Modèle de réécriture, puis la chaîne de secours (vide = modèle suivant) ; 45 s par appel, 90 s au total |
| Recherche Vector Search | Une requête en échec faisait échouer toute la recherche, relancée 2 fois en bloc | Chaque requête relancée sur 429/5xx/timeout (0,5 s, 1 s, 2 s + aléa). Si une requête sur trois échoue encore, on garde les deux autres. En mode `union`, si le côté reclassé tombe, le côté brut suffit (et inversement) |
| Réponse | 1 modèle. Sur 429 : attente de 60 s puis erreur. Coupure en cours de route : erreur | Chaîne de modèles, relances, reprise de la réponse coupée, file d'attente (détail § 2) |
| Rappel de langue | Ligne générique « dans la langue de la question » (option) | Ligne qui **nomme** la langue (« in French », « in Spanish ») quand elle est connue (option `CHAT_VSI_LANGUAGE_REMINDER`) |
| Retraduction de la réponse | Plafond fixe de 4 000 tokens ; un texte vide ou tronqué remplaçait la réponse | Plafond adapté à la longueur. Une traduction beaucoup plus courte que l'original est rejetée et la réponse d'origine gardée. Détection « mauvaise langue » plus stricte (confiance 0,8, sans citations ni tableaux) |

## 2. La réponse : `server/services/chat_vsi_llm.py`

Utilisé par les deux variantes du Chat VSI (`baseline` et `rerank`). Le prompt n'est pas
modifié.

**Ordre des modèles** : `CHAT_VSI_LLM_ENDPOINT`, puis ceux de `CHAT_VSI_LLM_FALLBACK_ENDPOINTS`.

**Quand on passe au modèle suivant :**

| Problème | Réaction |
|---|---|
| 429 (quota) | Modèle suivant **tout de suite**, sans attendre 60 s. Le modèle saturé est mis de côté 30 s pour **toutes** les questions de l'instance : les suivantes vont directement au secours |
| 5xx, timeout réseau | Modèle suivant ; mis de côté 30 s |
| 404 (endpoint absent du workspace) | Modèle suivant ; mis de côté 10 min, jamais retenté pendant la question |
| 400 (paramètre refusé, contexte trop long), 401/403 | Modèle suivant ; pas retenté pendant la question |
| Pas un mot de réponse après 90 s | Modèle suivant (un modèle qui raisonne pense avant d'écrire : GPT-6 Luna met 7 s en médiane) |
| Réponse vide | Modèle suivant |
| Tous en échec | Nouveau tour de la chaîne après 2 s, puis 8 s (ou le `Retry-After` du serveur, 30 s maximum). 3 tours, 240 s au plus. Ensuite un seul message clair : « Qualibot could not get an answer right now… » |

**Réponse coupée en cours de route** (connexion perdue, flux muet 60 s, erreur au milieu du flux) :
- le modèle suivant, ou le même s'il est seul, reçoit le début déjà écrit et la consigne de
  continuer exactement là où le texte s'arrête ;
- l'utilisateur voit une seule réponse continue ;
- au plus 2 reprises ; si aucune ne marche, la réponse affichée finit par « The answer was
  interrupted by a technical problem — please ask again. ».

**Plafond de sortie** : un modèle qui raisonne compte sa réflexion dans `max_tokens`. Quand la
chaîne passe sur un tel modèle, le plafond est porté à 6 000 au minimum
(`CHAT_VSI_REASONING_MIN_TOKENS`, 1 000 pour la réécriture). Un plafond de 2 000 prévu pour
Sonnet ne tronque donc plus Luna.

**Forte charge** :
- `CHAT_VSI_MAX_CONCURRENT_ANSWERS` (32 par défaut) réponses générées en même temps par
  instance de l'app ;
- les suivantes attendent leur tour, connexion maintenue par des keepalive, au lieu de toutes
  taper le quota en même temps ;
- ordre de grandeur : 1M tokens d'entrée par minute et par endpoint, soit environ 40 questions
  par minute à 25k tokens. Avec le secours, environ 80 par minute avant la première vraie
  attente.

**Traçabilité** : chaque réponse émet un événement `llm` : le modèle qui a répondu, s'il s'agit
du secours, et chaque tentative (modèle, résultat, durée). La variante `rerank` le met dans les
métadonnées du tour (`llm`, `llm_fallback`, `llm_attempts`). Chaque incident est écrit dans les
logs de l'app, préfixe `chat_vsi_llm:`. Un tour sans incident n'y laisse rien.

## 3. Réglages (tous lus à chaque question)

| Variable | Défaut | Rôle |
|---|---|---|
| `CHAT_VSI_LLM_FALLBACK_ENDPOINTS` | vide | Modèles de secours de la réponse, dans l'ordre |
| `CHAT_VSI_REWRITE_FALLBACK_ENDPOINTS` | la chaîne de réponse | Secours de la réécriture |
| `CHAT_VSI_FIRST_TOKEN_TIMEOUT_S` | 90 | Délai maximal avant le premier mot |
| `CHAT_VSI_STALL_TIMEOUT_S` | 60 | Durée maximale de silence en cours de réponse |
| `CHAT_VSI_LLM_DEADLINE_S` | 240 | Plus aucune nouvelle tentative au-delà |
| `CHAT_VSI_LLM_ROUNDS` | 3 | Tours de la chaîne complète |
| `CHAT_VSI_COOLDOWN_S` | 30 | Mise de côté d'un modèle après 429/5xx/timeout |
| `CHAT_VSI_MAX_CONCURRENT_ANSWERS` | 32 | Réponses simultanées par instance (0 = pas de limite) |
| `CHAT_VSI_REASONING_MIN_TOKENS` | 6000 | Plancher de `max_tokens` sur un modèle qui raisonne |
| `CHAT_VSI_REWRITE_TIMEOUT_S` | 45 | Délai d'un appel de réécriture |
| `CHAT_VSI_SEARCH_RETRIES` | 2 | Relances par requête Vector Search (la recherche entière : 1 de plus) |
| `CHAT_TRANSLATE_FALLBACK_ENDPOINTS` | `databricks-gpt-6-luna` | Secours de la traduction (`app.yaml`) |

## 4. Fichiers modifiés

- `server/services/chat_vsi_llm.py` (nouveau) : chaîne de modèles, relances, reprise,
  file d'attente, appels courts avec secours.
- `server/services/chat_vsi.py` : la variante `baseline` répond via `chat_vsi_llm` (prompt
  inchangé) ; modèle effectif dans les métadonnées.
- `server/services/chat_vsi_rerank.py` :
  - réécriture et réponse via `chat_vsi_llm` ;
  - relance par requête Vector Search (`_post_query`) ;
  - recherches partielles acceptées (`_gather_tolerant`, `union` sur un seul côté) ;
  - langue de réponse transmise au prompt ;
  - `settings()` liste les secours et le rappel.
- `server/services/chat_vsi_prompts.py` : rappel qui nomme la langue (`answer_language`).
- `server/services/chat_vsi_variants.py`, `server/routers/chat.py` : le routeur calcule la
  langue de la réponse (`translation_bridge.answer_language`) et la passe au Chat VSI. Le KA
  n'est pas touché.
- `server/services/translation_bridge.py` :
  - nettoyage avant détection, détection « mauvaise langue » plus stricte ;
  - relance sur 429, modèle de secours ;
  - plafond adapté, rejet des traductions tronquées ;
  - noms des langues.
- `app.yaml` : `CHAT_VSI_LLM_FALLBACK_ENDPOINTS` (vide) et `CHAT_TRANSLATE_FALLBACK_ENDPOINTS`.
- Notebooks d'éval (`golden_eval_ka_vs_vsi`, `replay_compare`, `retrieval_eval`,
  `pairwise_answers`) :
  - ils ne reprennent plus de la config de l'app que les index et le modèle de réponse ;
  - une fois l'app passée sur Luna, une config mesurée n'hérite donc ni des options de
    recherche de l'app ni d'un modèle de secours ;
  - `retrieval_eval` a un widget `rewrite_model` (Sonnet 4.6, comme tous les runs passés).
- Tests : `tests/test_chat_vsi_llm.py` (21 tests : secours sur 429, 404 non retenté, réponse
  vide, erreur au milieu du flux, reprise, premier mot trop lent, file d'attente, recherche
  partielle, langue, traduction). 306 tests au total, tous verts.

## 5. Ce qui n'est pas couvert

- **Panne de Vector Search elle-même** (index hors ligne plus de quelques secondes) : la question
  échoue avec un message clair. Répondre sans documents irait contre la règle « seulement les
  documents ».
- **Le KA** (onglet Chat KA) : inchangé, il est appelé à disparaître.
- **Plusieurs instances de l'app** : la mise de côté d'un modèle saturé et la file d'attente
  valent par instance, pas pour toute l'app.
- **Non vérifié sur Databricks** : le format exact des erreurs en cours de flux de GPT-6 Luna.
  Le code reconnaît l'objet `{"error": …}` des endpoints Databricks ; un autre format finirait en
  « flux muet », donc en reprise après 60 s.
