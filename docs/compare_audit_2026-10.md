# Audit de la comparaison de documents — 2026-10-04

Branche : `audit/doc-compare`. Périmètre : `/compare/analyze`, moteur de diff,
impact search, résumé, exports, historique, et `CompareView.tsx`.

## En bref

- **16 problèmes corrigés** dans l'application (partie A), **10 améliorations des
  moteurs PDF/DOCX** (partie E), **16 points identifiés mais laissés** (partie B).
- Les moteurs ont été testés sur des paires de documents PDF et DOCX **générées**
  avec une seule différence connue ; il n'y a aucun document réel sur cette
  machine.
- Le plus grave : **un changement d'un seul mot dans un long paragraphe était
  supprimé avant même d'arriver au LLM** (« shall record » → « shall not
  record », « avant » → « après », « peut » → « doit »). Corrigé (A1).
- Vérifié : 188 tests Python passent (123 avant + 65 nouveaux), `tsc --noEmit`
  et `vite build` passent.
- **Non vérifié** : rien n'a tourné contre Databricks (ni LLM, ni Vector Search,
  ni Lakebase), l'interface n'a pas été ouverte dans un navigateur, et le
  corpus `utils/compare_eval` n'est pas dans ce dépôt — l'effet de A1 sur le
  bruit reste à mesurer dessus. Étapes de test dans `OPERATIONS.md`.

## A. Corrigé sur la branche

Gravité : 🔴 résultat faux ou faille · 🟠 gêne réelle · 🟡 mineur.

### A1 🔴 Les changements d'un seul mot dans un long paragraphe étaient perdus

- **Problème.** `is_substantive_keep_modals` (`_diff_engines.py`) rejette une
  paire de paragraphes si la distance en caractères est sous 5 %, sauf si un
  nombre, une référence normative ou un modal anglais change. La distance est
  mesurée sur tout le paragraphe : un mot décisif dans un paragraphe de 40 mots
  pèse 1 à 2 %. La paire était comptée « filtered » et le LLM ne la voyait
  jamais. Reproduit sur : négation ajoutée, « before » → « after »,
  « operator » → « inspector », « peut » → « doit », « ≤ » → « ≥ », « interne »
  → « externe ».
- **Solutions possibles.** (a) baisser le seuil — rate encore les longs
  paragraphes ; (b) liste de mots sensibles — jamais complète ; (c) comparer mot
  à mot et ne filtrer que ce que le filtre visait vraiment.
- **Fait : (c).** Tout mot ajouté, retiré ou remplacé compte, sauf ponctuation,
  casse, césure, articles, variantes d'orthographe (similarité ≥ 0,85) et
  modaux du même groupe (anglais et français). Désactivable :
  `COMPARE_WORD_LEVEL_CHECK=false`.
- **Risque.** Plus d'entrées arrivent au LLM (qui filtre déjà synonymes et
  inversions d'ordre via son prompt). À mesurer sur `compare_eval`.
- **Tests.** `test_single_word_change_*`, `test_layout_only_differences_stay_filtered`.

### A2 🔴 Le cache de l'impact search ignorait ce qui était recherché

- **Problème.** Clé = `(hash ancien, hash nouveau, APP_VERSION)`. Or le résultat
  dépend aussi du texte des changements (Change Table ou Change Summary) et de
  l'index interrogé. Conséquences : une recherche lancée depuis le résumé était
  rejouée telle quelle après génération de la table ; et **après la bascule de
  `COMPARE_IMPACT_INDEX` vers `chunks_full_index_v1`, les paires déjà cherchées
  auraient continué à renvoyer l'ancien résultat sans les documents d'archive**.
- **Fait.** Empreinte du texte des changements, de l'index, de l'endpoint et des
  limites, stockée dans la colonne `app_version` existante (pas de changement de
  schéma) — `_impact_cache_version` dans `compare.py`.
- **Effet de bord.** Les lignes déjà en cache ne sont plus relues : une
  recherche par paire sera recalculée une fois.
- **Test.** `test_impact_cache_key_depends_on_changes_and_index`.

### A3 🔴 Un rapport incomplet pouvait être mis en cache pour tout le monde

- **Problème.** C'est le navigateur qui enregistre l'analyse (`POST /api/history`),
  et cette ligne sert ensuite de cache à tous les utilisateurs. Si la passerelle
  coupait le flux sans le marqueur `[DONE]`, le texte partiel était enregistré
  comme résultat complet. Idem pour un rapport coupé à la limite de tokens ou
  issu d'un document sans texte : le cache rejoue le texte sans l'avertissement.
- **Fait.** Flux terminé sans `[DONE]` → erreur « Connection lost », rien
  n'est enregistré (le texte partiel reste à l'écran). Rapport accompagné d'un
  avertissement → enregistré dans l'historique **sans hashes**, donc jamais
  servi depuis le cache.
- **Limite.** Vérifié par typage et build seulement, pas dans un navigateur.

### A4 🔴 `session_path` permettait de sortir du volume

- **Problème.** `/compare/load`, `/compare/save-result`, `/save-excel`,
  `/save-pdf` validaient `session_path.startswith(volume_path)`. Passaient donc
  `<volume>/../../autre` et `<volume>_autre` : lecture ou écriture dans tout
  volume accessible au service principal de l'app.
- **Fait.** `_is_within_volume` : chemin normalisé, `..` refusé, frontière de
  dossier exigée.
- **Tests.** `test_load_rejects_paths_outside_the_volume`,
  `test_save_result_rejects_paths_outside_the_volume`.

### A5 — retiré

Le contrôle `can_compare` posé sur les routes d'export a été enlevé à ta
demande (2026-10-04) : ces routes restent ouvertes, comme avant. Voir B16.

### A6 🟠 Diff tronqué à 600 000 caractères sans prévenir l'utilisateur

- **Problème.** `truncate_diff` coupe le diff et ajoute une note… lue seulement
  par le LLM. Le rapport s'arrêtait au milieu du document sans aucun signe.
- **Fait.** Avertissement envoyé à l'interface (`diff_truncation_warnings`),
  pour tous les types de fichiers.
- **Test.** `test_truncated_diff_produces_a_user_warning`.

### A7 🟠 Comparer un PDF avec un DOCX donnait une erreur illisible

- **Problème.** Le processeur est choisi d'après l'ancien fichier seul ; le
  second était lu avec le mauvais parseur.
- **Fait.** Message clair avant tout traitement.
- **Test.** `test_analyze_rejects_mixed_file_types`.

### A8 🟠 Des appels bloquants gelaient tous les flux en cours

- **Problème.** `/compare/save`, `/compare/load` et les exports appelaient le
  SDK Databricks et construisaient Excel/PDF directement dans des routes
  `async` : pendant l'envoi de deux fichiers de 20 Mo, **toutes** les analyses
  en streaming des autres utilisateurs étaient figées.
- **Fait.** Passage par `asyncio.to_thread`.

### A9 🟠 « Aucun changement significatif détecté » lançait une recherche

- **Problème.** Les prompts demandent cette phrase dans la langue du document,
  mais seule la version anglaise était reconnue. En français, la phrase partait
  en recherche comme si c'était un changement.
- **Fait.** Français, espagnol, allemand reconnus.
- **Test.** `test_no_changes_phrase_recognised_in_document_language`.

### A10 🟠 Le juge ne voyait pas toujours les changements qui avaient trouvé le document

- **Problème.** La liste des changements était coupée à
  `COMPARE_IMPACT_MAX_QUERY_CHARS` par un simple `[:max]` : fin de table
  perdue, dernier changement coupé en pleine phrase — y compris quand c'était
  justement ce changement qui avait ramené le document.
- **Fait.** Au-delà du budget : changements entiers seulement, ceux qui ont
  trouvé le candidat en premier, et une ligne indique combien sont omis.
- **Tests.** `test_changes_block_*`.

### A11 🟠 Un document pouvait être exclu à tort de l'impact search

- **Problème.** Le document comparé est exclu si sa REF apparaît dans le nom du
  fichier, par sous-chaîne : en comparant `GO-1316`, `GO-131` était exclu aussi.
- **Fait.** La REF ne doit pas être suivie d'un chiffre.
- **Test.** `test_exclusion_does_not_swallow_a_shorter_ref`.

### A12 🟠 Impact search impossible à annuler, jugements facturés pour rien

- **Problème.** Onglet fermé ou page quittée : les appels au juge restants
  continuaient. Aucun bouton d'annulation.
- **Fait.** Tâches annulées côté serveur quand le client part ; bouton
  « Cancel » et annulation au démontage côté interface.
- **Test.** `test_judge_calls_are_cancelled_when_the_consumer_stops` (serveur).

### A13 🟠 Charger l'historique laissait affichés les résultats d'une autre comparaison

- **Problème.** `handleLoadFromHistory` ne vidait ni l'impact search ni l'autre
  piste : on pouvait voir la Change Table du document X à côté du résumé et des
  documents impactés du document Y, et relancer une impact search sur le
  mauvais texte.
- **Fait.** L'impact est vidé ; l'autre piste aussi, sauf si elle porte sur les
  deux mêmes fichiers. Les hashes de l'entrée sont repris (le cache d'impact
  fonctionne donc aussi depuis l'historique).

### A14 🟠 Un `localStorage` plein faisait échouer une analyse réussie

- **Problème.** Les deux documents sont stockés en base64 dans `localStorage`,
  vite saturé. L'écriture finale du rapport levait alors `QuotaExceededError` :
  « Change Table failed », historique non enregistré. Et le rapport entier était
  réécrit à chaque fragment reçu.
- **Fait.** Écritures protégées (`lsSet`), et au plus une par seconde pendant
  le streaming.

### A15 🟡 Les hashes envoyés par le navigateur n'étaient pas vérifiés

- **Fait.** `/compare/analyze` et `/compare/summarize` calculent le SHA-256 des
  octets reçus (même format que le client) pour la clé de cache.
- **Test.** `test_analyze_cache_lookup_uses_server_side_hashes`.

### A16 🟡 Verdict « Impacted » sans aucun passage vérifiable

- **Fait.** Si le juge dit « impacté » mais ne cite aucun passage valide, le
  statut devient « To check ». Un numéro de passage renvoyé en `2.0` est accepté.
- **Tests.** `test_impacted_without_any_valid_passage_is_to_check`,
  `test_passage_number_given_as_float_is_accepted`.

### A17 🟡 Petites incohérences

- `APP_VERSION` : défaut `'2'` dans `compare.py`, `'1'` dans `history.py` — une
  seule définition désormais.
- `MAX_COMPARE_PDF_MB` (déclarée dans `app.yaml`) n'était lue nulle part : elle
  est maintenant honorée, comme `MAX_COMPARE_FILE_MB`.
- `/compare/save-excel` refusait une table vide `[]` valide.
- `GET /api/history` : `limit` borné à 100.

## B. Identifié, non corrigé

| # | Problème | Solution proposée | Pourquoi pas fait |
|---|---|---|---|
| B1 | Le cache d'analyse est écrit par le navigateur (`POST /api/history`) avec des hashes qu'il fournit : tout utilisateur de Compare peut y inscrire un faux rapport pour une paire de fichiers. | Enregistrer côté serveur à la fin du flux dans `/compare/analyze` ; `POST /history` ne fait plus que rattacher session et métriques. | Change le contrat client/serveur et le schéma d'écriture ; à décider. |
| B2 | Impact search depuis le Change Summary : un changement par section `##`, donc une requête qui mélange toutes les puces de la section. | Un changement par puce. | Choix posé le 2026-10-02 et couvert par un test ; à trancher par toi. |
| B3 | Chaque analyse envoie les deux fichiers dans `<volume>/.tmp/<id>` (jamais nettoyé), puis le navigateur les renvoie dans le dossier de session. | Supprimer l'envoi `.tmp`, ou job de purge. | Je ne sais pas si `.tmp` sert à un audit ; suppression = décision. |
| B4 | « Run Both » crée deux dossiers de session et les deux pistes écrasent le même `sessionPath`. | Une session partagée créée avant de lancer les deux pistes. | Lié à B5. |
| B5 | `CompareView.tsx` : 2 600 lignes, `handleAnalyzeStructured` et `handleAnalyzeStandard` identiques à 95 % (chaque correctif est à faire deux fois, comme ici). | Hook `useAnalysisStream(method)`. | Refactor large, sans test front pour le sécuriser. |
| B6 | Documents (jusqu'à 20 Mo) stockés en base64 dans `localStorage` (quota ~5 Mo) : la restauration après rechargement échoue en silence pour les gros fichiers. | IndexedDB. | Refactor front. |
| B7 | `parsePartialJsonItems` re-parse tout le texte à chaque fragment (coût quadratique sur les grosses tables). | Parser incrémental ou limiter à ~5/s. | Gain non mesuré. |
| B8 | `COMPARE_ANALYSIS_SYSTEM_PROMPT` dans `app.yaml` n'a aucun effet en `standard`/`structured` (prompts codés en dur ; seul `comparative` le lit). | Retirer la variable ou la brancher. | Décision de configuration. |
| B9 | Le moteur `comparative` (`is_substantive`) a le même défaut que A1 ; `MODAL_RE` ne connaît que l'anglais ; `canonicalize` supprime les lettres accentuées. | Appliquer le contrôle mot à mot à `is_substantive`, ajouter les modaux français. | Moteur non utilisé par défaut ; à faire avec une mesure sur `compare_eval`. |
| B10 | Aucune mesure automatique de la qualité de détection : `utils/compare_eval` n'est pas dans ce dépôt et n'est pas en CI. | Versionner le corpus (ou un sous-ensemble) et faire échouer la CI sous un seuil de rappel. | Corpus absent ici. |
| B11 | Analyse découpée : si une partie échoue après que d'autres ont été émises, le client garde un JSON incomplet. | Émettre les lignes valides puis fermer le tableau avant l'erreur. | Cas rare, non reproduit. |
| B12 | Table `messages` : pas d'index sur les hashes, lignes jamais purgées ; les trois caches n'écrivent qu'en ajout. | Index `(old_file_hash, new_file_hash, app_version, processor_version)` + purge. | Étape Lakebase manuelle. |
| B13 | Le cache ne rejoue pas les avertissements (contourné par A3 : ces rapports ne sont plus mis en cache, donc recalculés à chaque fois). | Stocker les avertissements avec le rapport. | Demande une colonne. |
| B14 | `preview.py` et `feedback.py` n'ont pas de contrôle `can_compare`. | Poser la dépendance. | Je n'ai pas vérifié s'ils servent aussi au chat. |
| B16 | Les routes d'export (`exports.py`) n'exigent pas `can_compare`. | `Depends(require_compare)` sur le routeur. | Fait puis retiré à ta demande. |
| B15 | Exports Excel/PDF et jugements d'impact : pas de test HTTP de bout en bout sur `/compare/impact` ni `/compare/summarize`. | Tests `TestClient` avec LLM et Vector Search simulés. | Hors temps de cette passe. |

## E. Moteurs PDF et DOCX

Méthode : deux révisions d'une procédure qualité générée (titres numérotés,
paragraphes, tableau, en-tête et pied de page « Page i/n »), **une seule
différence connue**, et on regarde ce qui arrive au LLM. Colonne « avant » =
résultat mesuré sur le code avant correction.

| # | Cas | Avant | Après |
|---|---|---|---|
| E1 | **PDF** — un paragraphe ajouté en page 1 d'un document de 6 pages (tous les sauts de page bougent) | 1 ajout + **6 faux « MODIFIED »** : un paragraphe coupé par un saut de page devenait deux blocs | 1 ajout. Les paragraphes coupés par un saut de page sont recollés (en-têtes et pieds de page ignorés, lignes de tableau jamais fusionnées) |
| E2 | **PDF** — ligatures (« ﬁ », « ﬂ ») | conservées : deux exports avec des polices différentes divergent sur chaque mot en « fi » | développées |
| E3 | **PDF** — tableau à bordures, une croix change de colonne (matrice de responsabilités) | **changement invisible** : les cellules vides étaient supprimées, les deux lignes se lisaient pareil | `Activity: Release the part \| ~~Inspector:~~ **Quality manager:** X` |
| E4 | **PDF** — tableau : valeur modifiée | `M10 bolt \| ~~45~~ **48** Nm \| Wrench C` (min ou max ?) | `Fastener: M10 bolt \| Max torque: ~~45~~ **48** Nm \| Tool: Wrench C` |
| E5 | **PDF** — tableau qui continue page suivante sans répéter l'en-tête | — | les lignes gardent les libellés de colonne |
| E6 | **DOCX** — tableau avec cellule vide ou deux cellules de même valeur | **libellés décalés d'une colonne** : `Max torque: Wrench A` ; un changement de Max était annoncé sur Min | chaque valeur porte sa propre colonne |
| E7 | **DOCX** — texte ajouté en suivi des modifications (`w:ins`) | **non lu** : aucun changement détecté | lu ; le texte supprimé en suivi (`w:del`, origine d'un déplacement) est ignoré |
| E8 | **DOCX** — paragraphe dans un contrôle de contenu (`w:sdt`), texte dans un champ (`w:fldSimple`), tableau imbriqué dans une cellule, notes de bas de page | **non lus** | lus (notes : `Footnote N: …`) ; les lignes de sommaire (styles `TOC n` / `TM n`) sont ignorées |
| E9 | **Les deux** — un paragraphe coupé en deux (ou deux fusionnés), texte identique | 1 faux ajout + 1 faux « MODIFIED » | rien |
| E10 | **Les deux** — section insérée, titres suivants renumérotés | 2 entrées par titre (« REMOVED 3. INSPECTION » + « ADDED 4. INSPECTION »), chacune sous son propre `##` | 1 entrée par titre : `~~3.~~ **4.** INSPECTION`. Le numéro reste visible : `2.5 mm max` → `3.5 mm max` a la même forme et ne doit jamais être masqué |

Déjà corrects avant, vérifiés et maintenant couverts par un test : documents
identiques (0 entrée), valeur modifiée, négation, paragraphe supprimé, section
déplacée, pied de page dont la révision change (1 entrée regroupée), cinq
modifications dont un paragraphe déplacé **et** modifié, document en français,
figure redessinée (1 image « MODIFIED »), figure supprimée, PDF sans couche
texte (avertissement).

**Limites.**
- Documents générés, pas des documents Latécoère : la mise en page réelle
  (cartouches, schémas de câblage, tableaux sans bordures, deux colonnes) n'est
  pas représentée. E1, E3-E5 changent l'extraction PDF : à rejouer sur
  `compare_eval`.
- Retour arrière sans toucher au code : `COMPARE_PDF_MERGE_PAGE_SPLITS=false`
  (E1), `COMPARE_PDF_TABLE_LABELS=false` (E3-E5), `COMPARE_WORD_LEVEL_CHECK=false` (A1).
- E3-E5 : un tableau n'est étiqueté que si sa première ligne ressemble à un
  en-tête (3 colonnes ou plus, toutes remplies, sans chiffres). Les
  formulaires clé/valeur et les cartouches restent en positions
  (`Revision | A`). Un tableau dont la première ligne de données est
  entièrement du texte sera pris pour un en-tête.
- E6 : même règle « première ligne = en-tête » qu'avant côté DOCX, sans
  condition : un tableau DOCX sans en-tête reste mal étiqueté.
- En-têtes et pieds de page DOCX : toujours non lus.

Tests : `tests/test_compare_documents.py` (31 tests ; 20 échouent sur les
processeurs d'avant).

## C. Non audité en détail

Extraction PPTX / Excel / XML / image,
`export_helpers.py`, `ImpactResults.tsx`, `ComparisonHistory.tsx`,
`FeedbackPanel.tsx`, `streaming.py` (lu seulement pour la fin de flux).

## D. Comment tester

```powershell
# Tests Python (depuis la racine)
python -m pytest -q
python -m pytest -q tests/test_compare_audit.py tests/test_compare_documents.py   # les 65 tests de cet audit

# Front
cd client; bun run build
```

Tests manuels sur `qualibot-uat-test` : voir `OPERATIONS.md`, section
« Audit comparaison ».

Revenir en arrière sur A1 sans redéployer le code : variable d'environnement
`COMPARE_WORD_LEVEL_CHECK=false`.
