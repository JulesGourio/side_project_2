# Comparaison de documents et recherche d'impact

L'onglet Comparaison (`/compare`) reçoit deux versions d'un même document
(version de base et version modifiée). Il produit, au choix et séparément :

- un **résumé des changements** en Markdown ;
- un **tableau des changements** (JSON, affiché en tableau) ;
- une **recherche d'impact** : les autres documents de la base que ces
  changements touchent, avec pour chacun un verdict et les passages à mettre à
  jour ;
- un **résumé de chaque document** pris seul, sans comparaison.

Le code serveur est dans `server/routers/compare/` (routes) et
`server/services/compare/` (extraction, diff, prompts, exports), avec la
recherche d'impact dans `server/services/infra/vector_search.py` et
`server/services/compare/impact_queries.py`. L'interface est dans ce dossier.

## Analyses et routes

| Analyse | Route | Sortie | Export |
|---|---|---|---|
| Résumé des changements (`processor_version` = `standard`) | `POST /api/compare/analyze` | Markdown, une puce par changement, groupé par section | PDF (`POST /api/export/pdf`) |
| Tableau des changements (`structured`) | `POST /api/compare/analyze` | Tableau : section, type, criticité, valeur initiale, valeur modifiée, justification | Excel (`POST /api/compare/export-excel`) |
| Recherche d'impact | `POST /api/compare/impact` (flux NDJSON) | Documents jugés : impacté, à vérifier, non impacté, échec du jugement | Excel (`POST /api/compare/impact/export-excel`) |
| Résumé d'un document | `POST /api/compare/summarize` | Résumé en Markdown, ou description pour une image | aucun |

Les deux analyses texte passent par la même route ; seule la valeur
`processor_version` change. « Lancer les deux » déclenche les deux en même
temps, chacune avec son état, son annulation et son cache.

Autres routes :

| Route | Rôle |
|---|---|
| `POST /api/compare/save`, `GET /api/compare/load` | Sauvegarde et restauration d'une session dans le volume `COMPARE_VOLUME_PATH` |
| `POST /api/compare/save-result`, `save-excel`, `save-pdf` | Sauvegarde automatique d'un résultat dans le volume, après une analyse |
| `GET /api/history`, `GET /api/history/{id}`, `POST /api/history` | Historique des comparaisons de l'utilisateur, et enregistrement d'une comparaison |
| `PUT /api/history/{id}/impact` | Rattache le résultat d'une recherche d'impact à la comparaison |
| `POST /api/compare/impact/feedback` | Votes sur les verdicts et avis sur la recherche |
| `POST /api/preview`, `POST /api/preview/pdf` | Aperçu d'un fichier Word, PowerPoint ou Excel, en HTML ou en PDF (`preview.py`) |

Les routes d'analyse, d'impact, de résumé, de sauvegarde et d'historique
exigent l'accès à la comparaison (`require_compare`). Les chemins de session
sont vérifiés : un chemin hors du volume configuré est refusé.

## Analyse : résumé et tableau

**Fichiers acceptés** (`processors/factory.py`) : PDF, images (JPG, PNG, GIF,
WEBP, BMP, TIFF), Word (`.docx`, `.doc`), PowerPoint (`.pptx`, `.ppt`), Excel
(`.xlsx`, `.xls`), XML. La taille est plafonnée par `MAX_COMPARE_FILE_MB`
(20 Mo par défaut).

**Processeurs** : un par type de fichier (`processors/`). Chacun extrait le
texte, les images et leur numérotation (pages, diapositives, éléments XML).

| Type | Extraction | Particularités |
|---|---|---|
| PDF | PyMuPDF, texte trié par position, ligatures développées ; un paragraphe coupé par un saut de page est recollé (`COMPARE_PDF_MERGE_PAGE_SPLITS`) ; les lignes de tableau reçoivent leur en-tête de colonne (`COMPARE_PDF_TABLE_LABELS`) | Images dédoublonnées par empreinte perceptuelle (dhash), redimensionnées |
| Image | Pas d'extraction de texte : les deux images sont envoyées au modèle | Normalisation à 1600 px maximum |
| Word | Lit le texte des paragraphes, des tableaux, des zones de texte, des notes et des contrôles de contenu | Les lignes de tableau sont étiquetées par leur en-tête de colonne ; les cellules vides sont gardées |
| PowerPoint | Parcours récursif des formes, numérotées par diapositive | Images à 1024 px, dédoublonnées |
| Excel | Appariement des lignes par clé ; à défaut, diff unifié `difflib` | Colonnes qui changent dans plus de 50 % des lignes exclues (calculées automatiquement) |
| XML | Parcours des éléments | Pas d'images |

**Moteurs de diff** (`processors/_diff_engines/`) : `paragraph_semantic_diff`
est le moteur principal (méthodes `standard` et `structured`) ;
`section_canonical_diff` sert à la méthode `comparative`. Les passes de
`collapse.py` fusionnent les répétitions : en-têtes courants, blocs redécoupés,
mentions répétées sur plusieurs pages (au moins `COMPARE_BOILERPLATE_MIN_PAGES`
pages, 3 par défaut) et lignes de sommaire.

**Appel du modèle** : `COMPARE_ANALYSIS_ENDPOINT` avec le prompt
`COMPARE_ANALYSIS_SYSTEM_PROMPT`. Le flux renvoie le texte au fur et à mesure.
Au plus `COMPARE_MAX_CONCURRENT` analyses tournent à la fois. Un diff de plus de
`COMPARE_CHUNK_THRESHOLD_CHARS` caractères est coupé aux frontières de sections
en morceaux d'environ `COMPARE_CHUNK_SIZE_CHARS` caractères : un appel par
morceau, et un seul flux pour l'utilisateur (`chunked_analysis.py`). Le modèle
traite donc un diff très long en plusieurs appels.

**Cache** : clé `(old_file_hash, new_file_hash, APP_VERSION, processor_version)`,
dans la table `messages`. Le serveur recalcule les empreintes SHA-256 à partir
des fichiers reçus. Un nouveau calcul se fait avec `force_refresh` (bouton
« Relancer »). Quand une analyse vient d'être faite, le client enregistre la
comparaison dans l'historique (`POST /api/history`) et, si le volume est
configuré, sauvegarde le résultat (`save-result`, `save-excel`, `save-pdf`).
Ces sauvegardes ne bloquent jamais l'affichage.

**Historique** : une entrée rechargée est rangée dans le bon onglet selon sa
méthode (`structured` : tableau ; sinon : résumé), sans toucher à l'autre.

**Exports** :

- PDF (`services/infra/pdf_report.py`, fpdf2) : même mise en page pour le
  résumé de changements, le résumé d'un document et les réponses du chat. La
  police Inter (`server/assets/fonts/`) est fournie avec l'application.
- Excel (`services/compare/export_helpers.py`) : une ligne par changement, colorée
  selon la criticité (puis selon le type). Les miniatures des deux versions sont
  composées côte à côte dans une seule image.

**Lecture du JSON du modèle** : `_parse_json_response` accepte un tableau JSON
nu, un tableau dans un bloc de code Markdown, ou, à défaut, une extraction entre
crochets. Il renvoie `None` si rien ne se lit.

## Recherche d'impact

**Principe** : une requête vectorielle par changement, puis un appel de jugement
par document candidat. Le juge voit la liste complète des changements et les
passages du document, et renvoie son verdict, sa confiance, sa raison et chaque
passage en conflit avec une citation exacte.

1. **Changements numérotés** (`impact_queries.extract_changes`) : chaque ligne du
   tableau (ou chaque puce du résumé) reçoit un identifiant `C1`, `C2`… dans
   l'ordre du document. Les lignes purement éditoriales gardent leur numéro mais
   ne sont pas cherchées.
2. **Une recherche par changement** (`_fetch_chunks_multi`) : hybride, avec
   `COMPARE_IMPACT_PER_QUERY_RESULTS` résultats chacune. Au-delà de
   `COMPARE_IMPACT_MAX_QUERIES` changements, les changements les moins critiques
   partagent la dernière requête (un même groupe reste dans une seule requête, et
   une requête ne dépasse pas 2 000 caractères). Trois requêtes tournent à la fois ;
   deux nouvelles tentatives en cas d'échec.
3. **Un document, une entrée** (`vector_search._aggregate_docs`) : un passage
   trouvé par plusieurs changements n'apparaît qu'une fois, avec la liste des
   changements qui l'ont trouvé. Les variantes linguistiques d'un même document
   sont fusionnées, et la meilleure est jugée. Le document comparé lui-même est
   écarté (son REF est reconnu dans le nom des fichiers). Le rang est le nombre de
   changements qui le trouvent, puis le meilleur score.
4. **Jugement** : les `COMPARE_IMPACT_MAX_CANDIDATES` premiers documents sont
   jugés, 6 à la fois, par `COMPARE_IMPACT_ENDPOINT` (température 0). Chaque appel
   reçoit au plus 10 passages par document, de 3 000 caractères chacun, et la
   liste des changements (`COMPARE_IMPACT_MAX_QUERY_CHARS` caractères). Un appel
   en échec est repris une fois ; s'il échoue encore, seul ce document passe en
   « Jugement échoué ».
5. **Flux** : `plan` (changements, documents retenus, documents non jugés) ;
   puis un évènement `document` par document jugé, dès qu'il est prêt ; puis
   `done` (consommation). Une erreur de recherche est remontée sans évènement `plan`.

**Statuts** : `impacted` (impacté avec une confiance haute ou moyenne et au moins
un passage), `check` (confiance basse, ou impacté sans passage exploitable),
`not_impacted`, `error` (jugement échoué). Les documents hors du plafond de
jugement sont listés à part (`not_judged`) et ne s'affichent pas.

**Affichage** : vue par document (verdict, raison, passages à mettre à jour, le
passage en conflit surligné, et le changement concerné écrit en clair) ou vue par
changement (uniquement les changements qui touchent un document). Un filtre par
verdict. Les coûts, durées et scores ne s'affichent pas : ils sont dans les
tables de journal.

**Documents antérieurs à 2018** : affichés avec un badge « Archive » (seuil
`COMPARE_IMPACT_ARCHIVE_BEFORE`, 1er janvier 2018). Pour qu'ils soient trouvés,
`COMPARE_IMPACT_INDEX` doit pointer sur l'index qui les contient (voir
[`utils/parsing_pipeline/README.md`](../../../../utils/parsing_pipeline/README.md)).
Le réglage pointe sur `chunks_index`, qui ne contient que les documents publiés à
partir de 2018.

**Lien avec le tableau** : la colonne `#` du tableau affiche les identifiants
`C1`… Dans la recherche d'impact, cliquer sur un identifiant fait défiler le
tableau des changements jusqu'à la ligne correspondante, quand ce tableau est
affiché.

**Limites d'API** : une requête vectorielle est limitée à environ 29 000
caractères, et `num_results` à 200.

**Cache** : table `impact_cache`, clé = les deux empreintes + la version de
l'application + une empreinte du texte des changements, de l'index, du modèle et
des limites. Un résultat n'est gardé que si aucun jugement n'a échoué. « Relancer »
envoie `force_refresh`. Un résultat en cache est rejoué dans le même flux, avec
`cached: true`.

**Retours** (`impact_feedbacks`) : pouce haut ou bas sur le verdict d'un
document (avec le verdict affiché), et un avis global sur la recherche. Chaque
retour renvoie à la recherche par `impact_request_id`.

**Export Excel** : feuille `Passages` (une ligne par passage en conflit),
`Documents` (chaque document jugé), `Changes` (la liste des changements).

## Résumé d'un document

`POST /api/compare/summarize` : un fichier seul, sans comparaison, avec
`COMPARE_SUMMARY_ENDPOINT`. Un fichier image va à `COMPARE_SUMMARY_IMAGE_ENDPOINT`
(modèle de vision) et reçoit une description. Le texte est tronqué à
`COMPARE_SUMMARY_MAX_CHARS` caractères, et la réponse est limitée à
`COMPARE_SUMMARY_MAX_TOKENS`. Un document sans texte extractible (page scannée,
par exemple) reçoit une réponse « sans contenu », et non une erreur.

Cache : table `summary_cache`, clé = l'empreinte du fichier + la version de
l'application. Le résumé ne dépend pas de l'autre document.

## Journal

| Table | Contenu |
|---|---|
| `llm_requests` | Une ligne par analyse : statut (`ok`, `error`, `aborted`), durées (attente, construction, premier texte, génération, total), jetons, coût, nombre de parties si découpée |
| `impact_requests` | Une ligne par recherche d'impact : statut, nombre de requêtes et de passages, documents jugés, durées, jetons, coût |
| `impact_document_results` | Une ligne par document jugé : verdict, appel du juge, tentatives, durée |
| `impact_feedbacks` | Votes et avis sur les recherches d'impact |
| `messages` | Historique des comparaisons (textes d'analyse, résultat d'impact, empreintes, fichiers) |
| `summary_cache`, `impact_cache` | Caches décrits plus haut |
| `errors` | Erreurs des routes d'analyse, d'impact, de résumé et d'export, avec leur origine dans le code |

## Configuration

Valeurs de `app.yaml` (les valeurs par défaut du code sont indiquées quand
elles diffèrent ; `app.yaml` est ce qui est déployé). Surcharges par cible dans
`utils/deploy/target_env.json`.

| Variable | Valeur | Rôle |
|---|---|---|
| `COMPARE_ENABLED` | `true` | Active la comparaison |
| `COMPARE_ANALYSIS_ENDPOINT` | valeur dans `app.yaml` | Modèle des analyses (résumé et tableau) |
| `COMPARE_ANALYSIS_SYSTEM_PROMPT` | dans `app.yaml` | Consigne de l'analyse |
| `COMPARE_ANALYSIS_TIMEOUT_S` / `_CONNECT_TIMEOUT_S` / `_RETRIES` | `300` / `30` / `1` | Délais et reprises de l'appel |
| `COMPARE_MAX_TOKENS` / `COMPARE_THINKING_BUDGET` / `COMPARE_TEMPERATURE` | `16000` / `4000` / `0.0` | Paramètres de l'appel (défaut du code : `8192` pour les jetons) |
| `COMPARE_MAX_CONCURRENT` | `10` | Analyses simultanées (défaut du code : 5) |
| `COMPARE_CHUNK_THRESHOLD_CHARS` / `COMPARE_CHUNK_SIZE_CHARS` | `60000` / `45000` | Découpage des diffs longs |
| `COMPARE_METHOD_PDF`, `_IMAGE`, `_DOCX`, `_PPTX`, `_EXCEL` | `standard` | Méthode par défaut de chaque type de fichier |
| `COMPARE_PDF_MERGE_PAGE_SPLITS`, `COMPARE_PDF_TABLE_LABELS`, `COMPARE_WORD_LEVEL_CHECK` | `true` | Réglages d'extraction et de diff (pour les comparaisons A/B) |
| `COMPARE_BOILERPLATE_MIN_PAGES` | `3` | Nombre de pages à partir duquel une mention répétée est fusionnée |
| `MAX_COMPARE_FILE_MB` (ou `MAX_COMPARE_PDF_MB`) | `20` | Taille maximale d'un fichier. `MAX_COMPARE_FILE_MB` prime si les deux sont définis |
| `COMPARE_IMPACT_INDEX` | `dev_proj.qualibot.chunks_index` (par cible) | Index interrogé par la recherche d'impact |
| `COMPARE_IMPACT_ENDPOINT` | `databricks-gpt-5-6-luna` | Modèle du juge |
| `COMPARE_IMPACT_MAX_QUERIES` / `_PER_QUERY_RESULTS` | `30` / `40` | Requêtes par changement, résultats par requête |
| `COMPARE_IMPACT_MAX_CANDIDATES` | `15` | Documents jugés |
| `COMPARE_IMPACT_MAX_QUERY_CHARS` / `COMPARE_IMPACT_MAX_TOKENS` | `20000` / `1500` | Taille de la liste de changements par appel, jetons de sortie par appel |
| `COMPARE_IMPACT_ARCHIVE_BEFORE` | 1er janvier 2018 (défaut du code) | Seuil du badge « Archive » |
| `COMPARE_SUMMARY_ENDPOINT` / `COMPARE_SUMMARY_IMAGE_ENDPOINT` | `databricks-gpt-5-6-luna` | Modèles des résumés (texte, image) |
| `COMPARE_SUMMARY_MAX_CHARS` / `COMPARE_SUMMARY_MAX_TOKENS` | `300000` / `800` | Troncature du texte, jetons de sortie |
| `COMPARE_VOLUME_PATH` | par cible | Volume de sauvegarde des sessions ; vide : désactivé |
| `SOFFICE_ARCHIVE_VOLUME_PATH`, `SOFFICE_PATH` | par cible / vide | Aperçu PDF fidèle : archive LibreOffice dans un volume, ou binaire local (poste de développement) |
| `APP_VERSION` | `1.4.1` | Version de l'application : l'incrémenter invalide tous les caches |
| `LAKEBASE_PROJECT_ID`, `LAKEBASE_DATABASE` | `qualibot`, `doccompare` | Projet et base Lakebase de l'historique |

## Frontend (`client/src/components/compare/`)

| Fichier | Rôle |
|---|---|
| `CompareView.tsx` | Page : compose les sections et les hooks |
| `hooks/useAnalysisTrack.ts` | Une analyse (résumé ou tableau) : flux, état, sauvegarde, entrée d'historique |
| `hooks/useImpactSearch.ts` | Recherche d'impact : flux NDJSON, annulation |
| `hooks/useDocSummaries.ts` | Résumés de documents |
| `hooks/useCompareDocs.ts`, `hooks/useCompareConfig.ts`, `hooks/useHistoryLoad.ts` | Les deux documents et leurs empreintes ; configuration serveur ; rechargement d'une entrée |
| `api.ts`, `files.ts`, `storage.ts`, `types.ts` | Appels HTTP et lecture du flux ; fichiers ; clés du stockage local ; types partagés |
| `DocsPanel.tsx`, `DocDropZone.tsx`, `FilePreviewModal.tsx` | Dépôt des fichiers, aperçu, résumés |
| `AnalysisActions.tsx`, `AnalysisResults.tsx`, `ResultCards.tsx` | Boutons, cartes du résumé et du tableau |
| `JsonDiffTable.tsx`, `ImageLightbox.tsx`, `Feedback.tsx` | Tableau des changements, images, avis |
| `ImpactSection.tsx`, `impact/` | Recherche d'impact : résultats par document et par changement, votes |
| `ComparisonHistory.tsx` | Liste de l'historique |
| `ui.tsx` | Boutons, cartes et fenêtres communes |

Le navigateur calcule l'empreinte SHA-256 de chaque fichier (Web Crypto) pour son
état local et pour les appels d'impact.

## Tests et outils

Tests (`tests/`) : `test_compare_pipeline.py` et `test_compare_documents.py`
(moteur de diff), `test_compare_round3.py` et `test_compare_audit.py` (routes
avec `TestClient`), `test_impact_queries.py` et `test_impact_judge_prompt.py`
(requêtes et jugement), `test_diff_golden.py`, `test_processors_golden.py`,
`test_pdf_golden.py` (résultats de référence), `test_pdf_report.py`,
`test_visual_diff.py`, `test_structured_prompt.py`, `test_standard_prompt.py`,
`test_summarize_prompt.py`, `test_vs_gate.py`.

```
python -m pytest tests/test_compare_*.py tests/test_impact_*.py tests/test_diff_golden.py tests/test_processors_golden.py
```

Hors des tests, `runbooks/scripts/compare/compare_preview.py` lance l'extraction, le diff et la
préparation des images sur une paire de fichiers locaux, sans appel au modèle, et
affiche ce qui serait envoyé :

```
python runbooks/scripts/compare/compare_preview.py version_a.docx version_b.docx
```
