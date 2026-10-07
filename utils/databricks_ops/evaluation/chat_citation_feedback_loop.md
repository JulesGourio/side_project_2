# Piste "exemples/leçons à partir du feedback" pour le chat — résultats mitigés (2026-07-30)

Investigation démarrée sur une demande : ajouter un mécanisme d'exemples au chat
sans passer par l'infra native Databricks (Vector Search "en plus") pour éviter
un endpoint supplémentaire. Rien n'a été déployé en prod — ni index créé, ni
`ka-7679a56e-endpoint` (`qualibot_ALL_v2`, l'endpoint réel du chat prod/UAT)
modifié. Ce fichier documente ce qui a été essayé et pourquoi le résultat
d'ensemble reste mitigé, pas encore un mécanisme prêt à industrialiser.

Données utilisées : export Lakebase UAT (`chat_messages`/`chat_feedbacks`, via
le job `qualibot-lakebase-export-uat-to-volume-qualibot-uat` déjà planifié) →
volume `/Volumes/uat_landingzone/qualibot/staging/lakebase_export/`. Tests
exécutés en direct contre `ka-7679a56e-endpoint` (host UAT, profil CLI `UAT`).

## 1. Injection brute d'une paire Q/A en few-shot (historique de messages)

Levier : prépendre des tours `user`/`assistant` fictifs devant la vraie
question dans les `messages` envoyés au Knowledge Assistant — même principe
que `_with_today_date` dans `server/routers/chat.py` qui injecte déjà la date
du jour de cette façon.

- **Cas Q0102QP** (feedback session `b921dcf1…`, *"Manque le doc Q0102QP pour
  AS"*) : sans exemple, le fil reproduit le bug (le doc AS `Q0102QP_GB` n'est
  jamais cité, seuls des docs IS le sont). Avec un exemple few-shot montrant la
  bonne citation, le modèle cite bien `Q0102QP_GB` sur les 2 tours suivants.
  **Succès.**
- **Cas CO014 → Q0197QP_FR** (feedback msg `1146`, *"trouve pas info concernant
  qualification"*) : un premier exemple formulé "cherche plus fort avant de
  conclure" a **empiré** le résultat — le modèle a inventé une nouvelle
  citation fausse (`PRLAT103_FR`, sans rapport) plutôt que d'admettre ne pas
  savoir. Reformulé en "ne cite QUE ce que ta recherche de ce tour retourne,
  ne réutilise/n'invente jamais une ref non vérifiée", le résultat s'est
  amélioré : plus de citation inventée, réponse honnête + question de
  clarification. **Correction partielle, pas une réponse complète.**
- **Cas scoping division AS** (feedback msg `44`, *"Ce document ne s'applique
  qu'à la BUS et pas à AS"*) : le bug (citer `MI-13666`, hors-scope) ne s'est
  pas reproduit au baseline du jour. Avec l'exemple, le modèle a cité
  exactement les mêmes refs que l'exemple (`NF-10627`/`NF-10644`/`Q0126QP`) —
  bon résultat ici, mais risque de "calque" difficile à distinguer d'un vrai
  raisonnement indépendant sans un cas où la bonne réponse diffère de
  l'exemple.

**Constat** : la formulation de l'exemple compte énormément. Une consigne
"cherche plus fort" pousse vers l'hallucination ; une consigne "ne cite que du
vérifié, n'invente rien" réduit l'hallucination mais ne garantit pas une
réponse complète.

## 2. Levier découvert : les instructions système du Knowledge Assistant sont éditables par SDK

`databricks.sdk.WorkspaceClient(...).knowledge_assistants` expose
`get_knowledge_assistant` / `update_knowledge_assistant` sur
`knowledge-assistants/7679a56e-4600-49fa-949e-bc7339a7d42b`
(`ka-7679a56e-endpoint`, créé par ce compte). Les instructions actuelles ont
été lues intégralement — elles couvrent déjà langue, scoping AS/IS, format de
citation, anti-fabrication d'URL — mais **aucune règle n'interdit
explicitement de réutiliser/inventer un REF non vérifié d'un tour précédent**,
exactement le trou identifié en §1.

Piste non exploitée pour l'instant : ajouter cette règle une fois dans les
instructions du KA plutôt que de la ré-injecter par requête. Pas fait — ça
change le comportement du chat pour tous les utilisateurs en production
immédiatement, ça nécessite un accord explicite avant d'y toucher.

Deux notes en marge, non résolues :
- L'API native d'exemples Databricks (`create_example`/`list_examples`,
  rattachée au KA lui-même, donc a priori sans endpoint supplémentaire) a
  renvoyé une `InternalError` (`Failed to fetch internal resources`) au moment
  du test — fonctionnalité preview visiblement instable, pas utilisable en
  l'état.
- `get_knowledge_assistant` renvoie `state: "FAILED"` avec
  `error_info: "Vector search endpoint failed to provision (status=OFFLINE)"`
  sur ce même KA, alors qu'il répond normalement à toutes les requêtes de
  test. Possible reliquat d'une mise à jour de config bloquée. À vérifier
  séparément si besoin, sans lien direct avec cette investigation.

## 3. Prétraitement LLM : distiller le feedback en "leçon" avant de l'injecter

Idée : plutôt qu'injecter l'exemple brut, faire distiller
`(question, mauvaise réponse, feedback négatif)` par un LLM
(`databricks-claude-sonnet-4-6` — déjà utilisé ailleurs dans l'app, toujours
aucun endpoint supplémentaire) en une leçon généralisée, injectée en un seul
tour (préfixe sur la question, comme `_with_today_date`).

- **Cas Q0102QP** : leçon distillée = *"inclue toujours Q0102QP comme
  référence AS, aux côtés d'INAQ619_FR/PRLAT538_FR pour IS..."*. Résultat :
  réponse bi-division complète (AS + IS) en **un seul tour**, sans historique
  fictif. Meilleur résultat de toute l'investigation. **Succès net.**
- **Cas IQ19_165** (feedback msg `1918`, *"pourquoi citer l'IQ19 165... ça n'a
  rien à voir... seul le premier doc me parait pertinent"*) : leçon distillée
  = *"évite de citer IQ19_165_FR_EN, sur-récupéré sur des requêtes sans
  rapport..."*. Résultat : le modèle **cite quand même IQ19_165_FR_EN**,
  malgré la consigne explicite, et perd même la seule bonne référence
  (`NS-10477_FR`). **Échec.**

### Conclusion de fond

Les deux échecs/succès ne relèvent pas du même type de problème :

1. **Mauvaise substitution / citation manquante** (Q0102QP) : le bon document
   existe et est récupérable, l'agent choisit juste mal parmi les candidats.
   Une consigne prompt-level corrige efficacement ce cas.
2. **Sur-récupération d'un document non pertinent** (IQ19_165) : le document
   remonte systématiquement, probablement par proximité d'embedding sur du
   texte générique/organisationnel dans son chunk. Une consigne prompt-level
   **ne suffit pas** — dire "n'utilise pas X" n'empêche pas la recherche
   interne du KA de le remonter en tête. Ce type de problème se corrige côté
   index (re-chunking, ou liste d'exclusion par `chunk_id`), pas côté prompt.

**Verdict global** : la piste "leçons distillées + index de similarité +
enrichissement quotidien" n'est validée que pour la classe 1 des deux
problèmes rencontrés, sur un échantillon de 2 cas côté "succès" (Q0102QP en
few-shot brut ET en leçon distillée) contre 1 échec net (IQ19_165) et 1
succès partiel (CO014). Pas assez concluant pour justifier l'implémentation
de l'index + pipeline d'enrichissement quotidien en l'état — la classe 2 des
problèmes (sur-récupération) resterait non résolue par ce mécanisme, et rien
ne garantit que la classe 1 généralise au-delà des 2 cas testés.

## Pistes non tranchées

- Ajouter la règle anti-hallucination directement dans les instructions du KA
  (§2) — plus robuste qu'une injection par requête, mais impact prod immédiat,
  nécessite un accord explicite avant toute application.
- Traiter séparément la sur-récupération (classe 2) : identifier les chunks
  "aimants" (comme IQ19_165_FR_EN) via un signal de fréquence de citation
  hors-sujet, avant d'envisager un re-chunking ou une exclusion ciblée.
- Élargir l'échantillon de test avant de trancher — 2 cas ne suffisent pas à
  distinguer un vrai signal d'un artefact du non-déterminisme du KA (le
  baseline lui-même varie d'une exécution à l'autre sur la même question).
