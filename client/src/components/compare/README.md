# Comparaison de documents

Onglet Comparaison (`/compare`, [`ComparePage.tsx`](../../pages/ComparePage.tsx)).
L'utilisateur dépose deux versions d'un document. Le composant `CompareView`
produit, séparément ou ensemble :

- un **résumé des changements** (Markdown, une puce par changement) ;
- un **tableau des changements** (JSON affiché en tableau) ;
- une **recherche d'impact** : les autres documents de la base que ces
  changements touchent, avec un verdict par document et les passages à mettre à
  jour ;
- un **résumé d'un document** pris seul.

Le serveur fournit les analyses (`server/routers/compare/`), les exports
(`server/routers/exports.py`), l'historique (`server/routers/history.py`), les
votes (`server/routers/feedback.py`) et les aperçus (`server/routers/preview.py`).
Le dossier ne contient que l'interface et l'état du navigateur.

## Fichiers

| Fichier | Rôle |
|---|---|
| `CompareView.tsx` | Page : assemble les panneaux, les deux analyses, la recherche d'impact et l'historique |
| `DocsPanel.tsx` | Deux zones de dépôt et les résumés par document (un seul affiché à la fois) |
| `DocDropZone.tsx` | Une zone de dépôt : contrôle du type et de la taille, aperçu, remplacement |
| `FilePreviewModal.tsx` | Aperçu plein écran d'un fichier (PDF, image, DOCX, autres formats convertis par le serveur) |
| `AnalysisActions.tsx` | Boutons de génération, d'annulation et de relance (« Re-run ») pour chaque analyse, et « Run Both » |
| `AnalysisResults.tsx` | Affiche la carte du résumé ou du tableau ; onglets quand les deux existent |
| `ResultCards.tsx` | Cartes : résumé Markdown, tableau structuré, résumé d'un document |
| `JsonDiffTable.tsx` | Tableau des changements : colonnes selon le type de fichier, masquage des changements peu critiques, images |
| `ImageLightbox.tsx` | Vue avant / après d'un changement visuel |
| `Feedback.tsx` | Vote et commentaire sur une carte de résultat |
| `ImpactSection.tsx` | Section recherche d'impact : boutons, état, carte de résultats |
| `impact/ImpactResultsCard.tsx` | Résultats d'impact, « par document » ou « par changement », filtres par verdict |
| `impact/ImpactParts.tsx` | Sous-composants : passages en conflit, changements, badges, liste des changements |
| `impact/ImpactFeedback.tsx` | Votes sur le verdict d'un document et avis sur la recherche |
| `impact/types.ts` | Types du résultat d'impact (flux NDJSON) |
| `ComparisonHistory.tsx` | Panneau d'historique paginé |
| `hooks/useAnalysisTrack.ts` | Une analyse en flux SSE ; instanciée deux fois (tableau, résumé) |
| `hooks/useImpactSearch.ts` | Recherche d'impact en flux NDJSON, annulation, résultat restauré |
| `hooks/useDocSummaries.ts` | Résumés d'un document seul |
| `hooks/useCompareDocs.ts` | Les deux documents et leurs empreintes, reflétés dans le stockage local |
| `hooks/useCompareConfig.ts` | Configuration serveur : volume, index d'impact, versions de traitement disponibles pour le type du fichier |
| `hooks/useHistoryLoad.ts` | Recharge une entrée d'historique dans la bonne piste et restaure les fichiers |
| `api.ts` | Appels HTTP, lecture du flux SSE, sauvegardes automatiques, historique |
| `files.ts` | Types de fichiers acceptés, taille maximale, empreinte SHA-256, fichiers « placeholder » |
| `storage.ts` | Clés du stockage local, écriture limitée dans le temps pendant un flux |
| `types.ts` | Types partagés : document, paire d'images, changement, résumé |
| `ui.tsx` | Composants communs : boutons, onglets, carte, fenêtre modale, défilement suivant le flux |

## Dépôt des fichiers

- Un fichier est accepté si son extension figure dans `SUPPORTED_EXTENSIONS`
  (`files.ts`) : PDF, images, Word, PowerPoint, Excel, XML. Il est refusé au-delà
  de `MAX_FILE_SIZE_BYTES` (20 Mo).
- Le contenu est lu dans le navigateur (base64 et octets). Il est gardé dans le
  stockage local, sauf pour les placeholders.
- Changer un des deux fichiers efface les résultats, la recherche d'impact et les
  empreintes en cours.

## Analyse : tableau et résumé

`useAnalysisTrack` fait l'appel. Le navigateur calcule les empreintes SHA-256 des
deux fichiers, puis envoie un `FormData` à `POST /api/compare/analyze` avec
`old_file`, `new_file`, les empreintes, `force_refresh` et `processor_version`
(`structured` pour le tableau, `standard` pour le résumé).

La réponse est un flux SSE (`data: …`). Événements lus par `readAnalysisStream` :

| Événement | Effet |
|---|---|
| `metadata` | Type de fichier, méthode, identifiant de la requête ; si `cached` est vrai, le résultat vient du cache serveur |
| `image_context` | Paires d'images envoyées au modèle |
| `response.output_text.delta` | Texte qui s'ajoute au résultat |
| `warning` | Notification ; le rapport est marqué comme possiblement incomplet |
| `usage` | Consommation, gardée avec l'entrée d'historique |
| `error` | Erreur affichée au-dessus des résultats |
| `[DONE]` | Fin normale du flux |

À la fin, dans l'ordre :

1. Si le flux s'est coupé sans `[DONE]`, le texte partiel reste à l'écran, avec un
   message « réessayez » ; il n'est ni ajouté à l'historique ni mis en cache.
2. Si le serveur a répondu depuis son cache, le résultat est affiché tel quel.
3. Sinon, si le volume de sauvegarde est configuré, le dossier de session reçoit
   les deux documents, le texte (`analysis.md`), puis l'Excel (tableau) ou le PDF
   (résumé).
4. Le résultat est ajouté à l'historique (`POST /api/history`).

Les étapes 3 et 4 ne bloquent jamais l'affichage : leur échec est ignoré.

**Annulation** : le résultat partiel reste à l'écran avec un message d'information,
sans être enregistré.

**Avertissement** : un rapport accompagné d'un `warning` est enregistré sans
empreintes. Le cache ne rejoue que le texte, et l'avertissement serait perdu.

**Re-run** envoie `force_refresh=true` et contourne le cache.

Les deux analyses sont indépendantes : chacune a son état, son annulation et son
cache. « Run Both » les démarre ensemble ; le bouton n'apparaît que si les deux
analyses sont proposées pour le type de fichier.

**Tableau des changements** : les lignes sont numérotées en ordre de document. La
colonne « # » reprend les identifiants `C1`, `C2`… utilisés par la recherche
d'impact. Les changements de criticité faible sont masqués par défaut, sans être
supprimés. Les images sont associées aux lignes par ordre d'apparition, et non par
numéro de page, car le numéro renvoyé par le modèle n'est pas fiable.

**Flux partiel** : le tableau est lu au fil du texte (`parsePartialJsonItems`),
sans attendre la fin.

## Recherche d'impact

`useImpactSearch` appelle `POST /api/compare/impact` en JSON : `changes_text`,
empreintes, noms des fichiers, `force_refresh`. Le texte des changements vient du
tableau s'il existe, sinon du résumé. La recherche ne démarre pas pendant qu'une
analyse est en cours.

Le flux est en NDJSON, une ligne par évènement :

| Évènement | Contenu |
|---|---|
| `plan` | Liste des changements, nombre de candidats, requêtes utilisées ou échouées |
| `document` | Un document jugé, affiché dès qu'il est prêt |
| `done` | Consommation, durée, identifiant de la requête d'impact |
| `error` | Erreur, qui interrompt la recherche |

**Verdicts** (`status`) : `impacted` (impacté), `check` (à vérifier),
`not_impacted` (non impacté), `error` (jugement échoué). Le filtre de la carte
porte sur ces quatre valeurs.

**Vues** : « par document » (verdict, raison, passages à mettre à jour, passage
en conflit surligné) ou « par changement » (seuls les changements qui touchent un
document). Les changements « C12 » cliqués dans les passages font défiler le
tableau jusqu'à la ligne correspondante, quand ce tableau est affiché.

**Badge « Archive »** : affiché pour un document publié avant 2018 (champ
`archive` du résultat serveur).

**Sauvegarde** : le résultat est gardé dans le stockage local et, si la comparaison
a une entrée d'historique, rattaché à celle-ci (`PUT /api/history/{id}/impact`).
Une recherche sans changement n'est pas rattachée.

**Votes** : un vote par verdict de document, et un avis global, envoyés à
`POST /api/compare/impact/feedback` avec l'identifiant de la requête.


## Historique

Le panneau lit `GET /api/history` par pages, puis `GET /api/history/{id}` pour une
entrée. `useHistoryLoad` range l'entrée dans la piste de son traitement : `structured`
va dans le tableau, les autres dans le résumé. L'autre piste n'est conservée que si
elle correspond aux deux mêmes fichiers (mêmes empreintes) ; sinon elle est
réinitialisée. Le résultat d'impact enregistré est restauré dans la même opération.

Les fichiers sont rechargés depuis le dossier de session du volume
(`GET /api/compare/load`). Sans fichier disponible, la zone de dépôt affiche le nom
du fichier comme placeholder, avec le message « File not available — drop to replace ».

## Résumé d'un document

`useDocSummaries` envoie le fichier et son empreinte à `POST /api/compare/summarize`.
Un document sans texte extractible reçoit un résumé « sans contenu », et non une
erreur. Chaque carte peut être exportée en PDF.

## Retours

- `Feedback.tsx` : pouce haut ou bas et commentaire facultatif, envoyés à
  `POST /api/feedback`. Un seul envoi par comparaison, mémorisé dans le stockage
  local.
- `impact/ImpactFeedback.tsx` : votes sur les verdicts et avis global (voir plus haut).

## Exports et aperçus

- PDF : `downloadPdf` dans [`lib/exportPdf.ts`](../../lib/exportPdf.ts), rendu par le serveur (`POST /api/export/pdf`).
- Excel : `downloadExport` dans `api.ts`, pour le tableau (`POST /api/compare/export-excel`) et pour la recherche d'impact (`POST /api/compare/impact/export-excel`).
- Aperçu : PDF et images sont lus localement ; un DOCX passe par
  [`ExactDocxPreview`](../shared/ExactDocxPreview.tsx) (`POST /api/preview/pdf`) ;
  les autres formats sont convertis en HTML par `POST /api/preview`.

## Stockage local

[`storage.ts`](storage.ts) garde les documents, les empreintes, les textes et les
résultats pour que la page survive au rechargement. Les écritures sont limitées à
une par seconde pendant un flux. Un échec d'écriture (quota dépassé) est ignoré :
le résultat en mémoire reste juste.

## Configuration côté interface

`useCompareConfig` lit `/api/config/app` et `/api/config/processors` :

- la sauvegarde automatique n'est active que si le chemin du volume est renseigné
  et n'est pas un marqueur de configuration non renseignée (`TODO…`) ;
- la recherche d'impact n'est proposée que si le nom de l'index est valide ;
- les versions de traitement proposées (`standard`, `structured`) dépendent du type
  du fichier. Les types et leurs versions sont définis dans
  `server/services/compare/processors/versions.yml`.

## Accès

`ComparePage` affiche le panneau d'accès refusé si `can_compare` est faux. Côté serveur,
les routes d'analyse, d'impact, de résumé, de sauvegarde et d'historique exigent
l'accès à la comparaison (`require_compare`).
