# À envoyer à Databricks — bugs Knowledge Assistant (2026-09-04)

Contexte détaillé, preuves, scripts de reproduction : `README.md` (même dossier).

---

**Souci de routage : le choix de la source interrogée n'est pas cohérent.**
Sur un Knowledge Assistant avec plusieurs sources attachées, la même question posée
plusieurs fois de suite peut être routée vers des sources différentes à chaque fois,
sans qu'aucune erreur ne le signale. Sur 32 envois strictement identiques (même
question, même tag), 62 à 75 % partent sur la mauvaise source selon le test.
Contournement actuel : 3 Knowledge Assistants séparés (un par division) plutôt qu'un
seul multi-sources.

**Souci de retrieval : recherches internes parallèles qui se percutent.**
Un Knowledge Assistant avec plusieurs sources attachées peut interroger plusieurs
d'entre elles en même temps pour une seule question. Ces recherches parallèles se
percutent avec une erreur interne ("already running"), faisant échouer l'une des deux
sources sans prévenir. L'utilisateur reçoit alors parfois "non trouvé" alors que le
document existe dans la source qui a échoué.

**Souci de provisioning : une fonctionnalité native reste bloquée sans erreur
visible.** La fonctionnalité "Exemples" dépend d'une ressource interne provisionnée
à la création. Sur notre instance de production, elle n'a jamais été créée : chaque
appel échoue silencieusement, sans erreur côté application. Un moyen de vérifier ou
relancer ce provisioning sans recréer le Knowledge Assistant ?

**Souci de retrieval : les questions multilingues fonctionnent mal.**
Une question posée dans une langue peu représentée dans le corpus retrouve nettement
moins de documents pertinents que la même question en français ou en anglais, alors
que les documents existent. Le modèle d'embeddings n'est pas en cause (vérifié
isolément). Reste le mécanisme de retrieval du Knowledge Assistant, ou un
déséquilibre du corpus. Une question en espagnol obtient 0 source citée contre 2-3 en
français/anglais pour un même besoin. Contournement actuel : traduction automatique
vers l'anglais avant envoi, puis retraduction de la réponse. Ça ajoute un coût et de
la latence, et ne résout rien si le document n'existe pas dans la langue demandée.

**Problème de retrieval : les documents ne sont pas retrouvés par leur code de
référence, pourtant indexé en métadonnées.** Un code de référence mêlé à du texte
dans une question fait régulièrement échouer la recherche, observé systématiquement
et pas comme un cas isolé, alors que le même code demandé seul retrouve le document
sans difficulté. Exemple : "quelles sont les différences entre NF-10845 et NS-1868"
ne retrouve ni l'un ni l'autre, alors que chaque référence redemandée séparément est
trouvée immédiatement.
