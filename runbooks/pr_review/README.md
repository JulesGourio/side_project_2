# Journal de revue — PR Bitbucket 2 (`latec-compare`)

**Statuts** : `Fait` = corrigé ici (avec test si c'est du code) · `Partiel` = une partie corrigée, le reste est dit · `Ouvert` = pas fait · `Décision` = attend ton choix (voir « Points en suspens ») · `Pas de changement` = réponse donnée, code inchangé.

## Synthèse

| Statut | Yann | Logan | Jules | Total |
|---|---|---|---|---|
| Fait | 56 | 19 | 1 | 76 |
| Partiel | 1 | 8 | 0 | 9 |
| Ouvert | 1 | 18 | 0 | 19 |
| Décision | 0 | 1 | 0 | 1 |
| Pas de changement | 4 | 0 | 0 | 4 |
| **Total** | **62** | **46** | **1** | **109** |

## Contexte

Un tableau par thème : une ligne par remarque (relecteur Yann, revue indépendante « Logan », commentaire de Jules). Les commentaires
Bitbucket se récupèrent avec `python debug\fetch_comment_pr.py` (copie de `runbooks/scripts/bitbucket_pr/fetch_pr_comments.py`). La PR est
faite sur le commit `14296499a19baf710dc6c10d74bdd71204176b2d` ; les numéros de ligne cités ne tombent pas toujours pile (les fichiers ont
bougé depuis) : chaque remarque a été rattachée au code par son sens. Relecteurs : Yann Depledt (commentaires inline) et Logan (fichier
`QualiBOT_PR_Review.html`, revue en lecture seule du commit `db1f6d7`, faite le 2026-10-20). Jules Gourio est l'auteur. Les nouveaux
commentaires s'ajoutent en bas du tableau de leur thème.

- **Origine** : `Yann` = commentaire inline Bitbucket (`Y-n`, id Bitbucket en petit après le numéro) ; `Logan` = constat de la revue
  indépendante (`L-1` à `L-29` = sa numérotation ; `L-b1` à `L-b17` = ses constats de gravité basse, dans l'ordre de sa liste) ; `Jules` =
  commentaire de l'auteur (`J-1`). Quand Logan a aussi jugé un commentaire de Yann, son avis est dans la ligne de Yann. Logan a jugé les 48
  premiers commentaires de Yann (lots 1 et 2) ; le lot 3 est arrivé après.
- **Lecture des cellules** : « … » en fin de cellule = la suite est dans la liste `Détails` sous le tableau. Pour les lignes de Yann, la
  colonne de Logan donne son avis en gras puis sa proposition ; pour les lignes de Logan, elle donne sa correction proposée (`—` = rien).
- Là où Logan est en désaccord avec Yann, le code suit Yann (c'est le relecteur), sauf mention. Rien n'a été déployé ni exécuté sur
  Databricks.

## Tableaux des remarques

### A. Sécurité et isolation des utilisateurs

| # | Origine · Fichier | Critique | Avis et proposition de Logan | Ma position · Statut |
|---|---|---|---|---|
| L-1 | Logan · `preview.py:26-40`, `conversion.py:189` | [Critique] `/api/preview` écrit l'upload à `join(tmp, filename)` : nom absolu ou `../` = écriture hors dossier (binaire LibreOffice, `client/out/index.html`). Route sans `require_compare`, taille ni type. | Nom fixe (`input.pptx`) ; `require_compare` + `_validate_file` sur les deux routes d'aperçu. | D'accord. Suffixe vérifié (`_SAFE_SUFFIX`), nom fixe, contrôles sur les deux routes, rendu hors boucle d'événements, erreurs génériques.<br>**Fait** |
| L-2 | Logan · `volume.py:51,89-140`, `exports.py:160-288` | [Critique] `session_path` (query) jamais vérifié : lecture et écrasement des documents des autres (`/compare/load`, `save-*`) ; dossiers nommés à la seconde (~86 000 essais par jour). | L'id d'historique vient du client, le chemin du serveur (`WHERE id AND user_id`) ; dossiers en UUID ; supprimer la copie `.tmp/{id}` inutile. | D'accord. Racine par utilisateur + dossier UUID, `owns_session` sur `load` et `save-*` (403), copie `.tmp` retirée.<br>**Fait** |
| L-4 | Logan · `analyze.py:68-117`, `history.py:63-120` | [Haute] Cache d'analyse alimenté par `POST /history` (corps de requête) : un texte planté est servi aux autres, avec l'id et le chemin du premier utilisateur. | Écrire le cache côté serveur en fin de flux réussi (table à part) ; ne jamais renvoyer l'identifiant d'un autre. | D'accord. Table `analysis_cache` écrite par le serveur seul ; plus d'identifiant d'un autre utilisateur dans le résultat en cache.<br>**Fait** |
| L-5 | Logan · `sessions.py:283`, `ws.py:80`, `persistence.py:15-26` | [Haute] Le lien de partage donne l'id réel de session ; le WebSocket accepte n'importe quel `session_id` et l'`upsert` ne vérifie pas le propriétaire : on écrit dans la conversation d'un autre. | `DO UPDATE … WHERE user_id = EXCLUDED.user_id`, compter les lignes affectées, ne plus renvoyer l'id. | D'accord. Vue partagée avec le jeton comme id, `upsert` conditionné au propriétaire, `session_not_owned` enregistré.<br>**Fait** |
| L-13 | Logan · `sessions.py:43-83,141,264-270`, `feedback.py:23-112` | [Moyenne] Feedback non lié au votant (ids SERIAL énumérables) ; la vue partagée montre les commentaires privés ; `get_session` ne filtre pas `deleted`. | — | Vote accepté seulement sur un message de l'utilisateur ; vue partagée = vote du propriétaire seul ; sessions supprimées exclues.<br>**Fait** |
| L-14 | Logan · `ws.py:63-67,79-81` | [Moyenne] Contrôle d'accès du WebSocket fail-open, pas de contrôle d'Origin, entrée non validée (rôle `system`, taille illimitée, message sans contenu). | — | Échec = refus ; liste d'Origin optionnelle (`ALLOWED_ORIGINS`, vérifiée seulement si renseignée) ; rôles et taille validés (`_clean_messages`).<br>**Fait** |
| L-15 | Logan · `credentials.py:9-30`, `doc_catalog.py:246-314` | [Moyenne] Tout utilisateur du chat atteint tout document indexé (compte de service) ; les puces du catalogue ignorent `in_chat` et la division. | Documenter comme contrôle accepté. | Pas de changement de code ici : à écrire comme contrôle accepté si tu le confirmes. Les puces du catalogue restent à filtrer.<br>**Décision** |
| L-b1 | Logan · exports, historique, config, sessions, volume, feedback, impact, aperçu | [Basse] `str(exc)` renvoyé au navigateur (chemins de volume, erreurs SQL). | Réponse générique comme le gestionnaire global. | Réponse générique `INTERNAL_ERROR` sur ces routes ; le détail va dans les logs.<br>**Fait** |
| L-b2 | Logan · client (Markdown) | [Basse] Exfiltration par injection de prompt : images Markdown vers un hôte quelconque, pas de CSP, liens non limités à https. | N'autoriser que des hôtes connus. | D'accord, à faire côté client (reconstruire `client/out`).<br>**Ouvert** |
| L-b3 | Logan · `export_helpers.py:243` | [Basse] Injection de formule à l'export Excel (texte commençant par `=`). | — | Texte stocké comme texte (`_store_text_as_text`).<br>**Fait** |
| L-b4 | Logan · `/api/history` | [Basse] Coûts falsifiables : `cost_eur` et jetons envoyés par le client. | — | D'accord ; à calculer côté serveur.<br>**Ouvert** |
| L-b10 | Logan · `_caps_cache` | [Basse] Cache des droits sans éviction ; pas de limitation de débit par utilisateur sur les routes LLM. | — | D'accord, faible priorité.<br>**Ouvert** |
| L-b11 | Logan · middleware identité | [Basse] Historique anonyme partagé (`user_id = ''`) sans en-tête d'identité. | — | Non atteignable derrière le proxy ; à refuser quand même si l'en-tête manque.<br>**Ouvert** |
| L-b17 | Logan · tests | [Basse] Chemins de sécurité sans test (propriété `load`/`save-*`, réutilisation de `session_id`, aperçu, tout le client). | — | Tests ajoutés pour la propriété des sessions, la réutilisation de `session_id`, l'aperçu protégé et la reconnexion Lakebase (`test_chat.py`, `test_compare_audit.py`, `test_lakebase_connection.py`). Le client reste sans test.<br>**Partiel** |

### B. Pipeline de parsing

| # | Origine · Fichier | Critique | Avis et proposition de Logan | Ma position · Statut |
|---|---|---|---|---|
| L-3 | Logan · `build_steps.py:128-131,355-358` | [Critique] En mode `full`, le checkpoint n'est pas filtré sur le manifeste : révisions remplacées remises dans `chunks` et dans l'index (UAT tourne en `full`). | Filtrer sur le manifeste dans tous les modes ; supprimer les lignes de checkpoint hors manifeste. | Filtre sur les IDDOC cibles dans tous les modes. Le nettoyage des lignes de checkpoint n'est pas ajouté (simple ajout si tu le veux).<br>**Fait** |
| L-7 | Logan · `manifest_steps.py:107-144`, `output_steps.py:242-266` | [Haute] Une révision remplacée perd ses chunks mais garde `SUCCESS` : `MISSING_CHUNKS`, re-parsing et nouvelle description d'images (payante), un jour sur deux. | Statut terminal pour les révisions élaguées. | Statut `SUPERSEDED_REVISION` posé à l'élagage.<br>**Fait** |
| L-21 | Logan · `describe_steps.py`, `update_proj_vector_indexes.py:323-335`, `parsing_pipeline.job.yml:62` … | [Moyenne] Robustesse et coût : `redescribe_all_images` jamais appliqué aux passages, prune qui échoue en silence, attente d'index qui passe au premier sondage, pas de délai maximal … | — | D'accord sur le fond ; chantier à part, rien changé.<br>**Ouvert** |
| L-22 | Logan · `manifest_steps.py:402-423` | [Moyenne] `dry_run=false` depuis DEV ou UAT supprimerait des dossiers de la zone PROD partagée. | Suppression seulement quand l'environnement d'écriture est `prod`. | Fait ainsi.<br>**Fait** |
| L-b12 | Logan · `document_change_log`, `_pipeline_checkpoint_<ts>` | [Basse] Doublons dans `document_change_log` sur runs réparés ; clones de checkpoint jamais supprimés. | — | D'accord, nettoyage à écrire.<br>**Ouvert** |

**Détails**

- **L-21** — Fichiers (suite) : `llm_describe.py:293` ; Critique (suite) : descriptions d'images non mises en cache, PNG étiqueté jpeg, PROD
  en pause.

### C. Fiabilité et performance du serveur

| # | Origine · Fichier | Critique | Avis et proposition de Logan | Ma position · Statut |
|---|---|---|---|---|
| L-10 | Logan · `connection.py:197-223` | [Haute] Lakebase en panne au démarrage : aucune reconnexion et `/health` reste vert. | Boucle de reconnexion avec attente croissante ; état Lakebase dans `/health`. | Fait ainsi (`tests/test_lakebase_connection.py`).<br>**Fait** |
| L-11 | Logan · `preview.py:26-86`, `credentials.py:15-25`, `doc_catalog.py:142-157`, `analyze.py:233,407` | [Moyenne] Boucle d'événements bloquée (aperçu synchrone, `Config()` à chaque tour, grosse regex des REF), sémaphore compare libéré à la déconnexion, un `httpx.AsyncClient` par appel, cache PDF jamais nettoyé. | `asyncio.to_thread`, un client SDK partagé, un sémaphore LibreOffice, clients HTTP poolés. | Aperçu via `asyncio.to_thread`, configuration SDK en cache, cache PDF borné à 200 fichiers. Reste : regex des REF, sémaphore compare, client HTTP partagé.<br>**Partiel** |
| L-27 | Logan · `persistence.py:83-91`, `users.py:40`, `schema.py:571-578` | [Moyenne] Un `upsert` d'utilisateur en échec perd la sauvegarde du chat ; deux messages d'un tour hors transaction ; index manquants ; pas de verrou de migration. | — | `upsert` non fatal (`user_upsert_failed`) ; tour écrit en une transaction ; index sur `compare_messages(user_id, created_at)`, `chat_sessions(user_id, updated_at)`, `chat_feedbacks(message_id)` ; verrou consultatif ; golden de schéma régénéré.<br>**Fait** |
| L-b8 | Logan · `store_error`, votes, uploads | [Basse] `create_task` sans référence : tâches perdables. | Garder une référence. | `store_error` doit rester synchrone (règle du projet) ; les votes et uploads sont à traiter.<br>**Ouvert** |
| L-b9 | Logan · rafraîchissement du jeton Lakebase | [Basse] `await old_pool.close()` peut être bloqué par une requête bloquée. | — | D'accord (délai maximal à poser).<br>**Ouvert** |
| Y-50 <sub>885860006</sub> | Yann · `llm.py:236` | « C'est normal d'ignorer ? » | **Pas jugé** | Ce n'est pas ignoré : échec journalisé, modèle mis en pause, suivant essayé, `CompareLlmUnavailable` levée si aucun ne répond. Commentaire ajouté.<br>**Fait** |

### D. Comparaison de documents (diff, Excel, conversions)

| # | Origine · Fichier | Critique | Avis et proposition de Logan | Ma position · Statut |
|---|---|---|---|---|
| L-6 | Logan · `excel.py:99-105,155-166`, `text.py:14,180`, `xml.py:25-56` | [Haute] Le diff perd de vrais changements : colonnes Excel « volatiles », feuilles et colonnes ajoutées, en-têtes, signes perdus (−40 → 40), XML malformé = « aucun changement », formules non lues. | Exclure une colonne seulement avec ≥ 20 lignes et la lister ; feuilles nouvelles ; capturer `[-+−±]?` ; erreur visible sur XML. | Tout fait sauf les formules (`data_only=True`, ouvert) : colonne exclue seulement avec ≥ 20 lignes et listée, feuilles et colonnes nouvelles rapportées, changement d'en-tête, signes capturés, XML malformé = erreur visible.<br>**Partiel** |
| L-16 | Logan · `impact_queries.py:81-132,165-172`, `impact.py:179-181,226`, `vector_search.py:456,473-482` | [Moyenne] Recherche d'impact : sections coupées à 2000 caractères, runs partiels mis en cache, citation inventée = quand même « impacté », auto-exclusion par REF seulement. | Garde `queries_failed > 0` sur le cache (voir Y-23). | D'accord, rien fait.<br>**Ouvert** |
| L-17 | Logan · `chunked_analysis.py:29-53,274-277` | [Moyenne] Gros documents découpés : lignes « aucun changement » entre les vrais changements, titre collé en fin de ligne, titres de section perdus, Excel jamais découpé (600 000 caractères en un appel). | — | D'accord, rien fait.<br>**Ouvert** |
| L-18 | Logan · `analyze.py:70-100`, `summarize.py:57`, `impact.py:45-56` | [Moyenne] Clés de cache sans prompt, modèle ni réglages. | Faire comme `_impact_cache_version`. | `_analysis_cache_version` et `_summary_cache_version` ajoutées.<br>**Fait** |
| L-23 | Logan · `llm.py:186-190`, `streaming.py:37` | [Moyenne] Température et « thinking » des modèles Claude morts ou invalides (400 silencieux, repli sur Luna). | — | Pas de température quand le raisonnement est actif ; test corrigé.<br>**Fait** |
| L-29 | Logan · `conversion.py:41-50,93-99,161,175-236` | [Moyenne] Conversions Office (soulevées par Yann) : aperçu PPTX = diapositive 1, `.doc` coupé à 400 fragments, encodage antiword, course sur la détection LibreOffice. | PDF d'abord puis une image par page ; `-m UTF-8.txt`. | Voir Y-44 à Y-48.<br>**Fait** |
| L-b5 | Logan · `.ppt`, images | [Basse] `.ppt` annoncé mais illisible par python-pptx ; grandes images non redimensionnées. | — | D'accord.<br>**Ouvert** |
| L-b6 | Logan · prompt d'images | [Basse] Images au-delà de 95 supprimées en silence, étiquettes restées dans le prompt. | — | D'accord.<br>**Ouvert** |
| L-b7 | Logan · extraction DOCX/PPTX | [Basse] Trous d'extraction : en-têtes et pieds DOCX, notes PPTX ; ligne courte ajoutée identique à une voisine masquée. | — | D'accord.<br>**Ouvert** |
| Y-44 <sub>885835700</sub> | Yann · `conversion.py:44` | « Danger si concurrence non ? » | **D'accord** : le drapeau « vérifié » est posé avant la fin de la recherche (voir L-29). | Vrai. `_libreoffice_cmd` supprimée : conversion par `soffice.py` (`convert_docx_to_pdf`), recherche du binaire une fois sous verrou, conversions simultanées limitées ; vérification d'antiword aussi sous verrou.<br>**Fait** |
| Y-45 <sub>885835950</sub> | Yann · `conversion.py:94` | « Encodage manquant non ? » | **D'accord** : charset dépendant de la locale, toujours décodé en UTF-8.<br>Proposition : `-m UTF-8.txt` | Vrai : accents remplacés par `�`. `-m UTF-8.txt` ajouté ; sans ce fichier, on relance sans lui et on lit en Windows-1252. Deux tests.<br>**Fait** |
| Y-46 <sub>885834566</sub> | Yann · `conversion.py:162` | « Pq 400 ? » | **D'accord** : bug réel, `.doc` long coupé en silence, aussi en comparaison sans antiword. | Plafond nommé `_OLEFILE_MAX_RUNS` et avertissement quand il coupe. Valeur inchangée, aucune mesure ne dit mieux.<br>**Fait** |
| Y-47 <sub>885833326</sub> | Yann · `conversion.py:178` | « Ça a été testé cette fonction ? » | **D'accord** : aucun test, et fonction cassée (diapositive 1 seule). | Non, pas avant. `tests/test_conversion.py` (10 tests : docx, antiword UTF-8 et repli, borne olefile, fichier non OLE, diapositives avec un vrai LibreOffice).<br>**Fait** |
| Y-48 <sub>885833609</sub> | Yann · `conversion.py:200` | « Tu es sûr que ça convertit toutes les pages ? » | **D'accord** : LibreOffice n'écrit que la première (voir L-29).<br>Proposition : PDF puis une image par page | Faux, corrigé : PDF puis une image PNG par page, 100 diapositives au plus (avertissement au-delà). Test avec un diaporama de 3 : 3 images.<br>**Fait** |
| Y-53 <sub>886268712</sub> | Yann · `factory.py:27` | « Pas de .PDF ? » | **Pas jugé** | Déjà géré : `get_extension` met en minuscules. Test ajouté (`test_excel_processor.py`).<br>**Fait** |
| Y-54 <sub>886269539</sub> | Yann · `factory.py:34` | « xlmx en + ? » | **Pas jugé** | `.xlsm` ajouté côté serveur (`EXTENSION_MAP`, aperçu) et client (`files.ts`) ; openpyxl le lit comme un xlsx (test). Reconstruire `client/out` pour que le navigateur l'accepte.<br>**Fait** |
| Y-60 <sub>885862314</sub> | Yann · `relocation.py:58` | « Double boucle. Pas moyen de mieux faire ? » | **Pas jugé** | Non pour l'algorithme (table de programmation dynamique, le glouton se trompe dès qu'un bloc est inséré tôt ; coût borné par `_DUP_ALIGN_MAX_CELLS`). Mieux pour la lecture : `_drift_cost_table` avec docstring. …<br>**Fait** |
| Y-61 <sub>885865732</sub> | Yann · `excel.py:64` | « Split » (`_build_diff_keyed`, 170 lignes) | **Pas jugé** | Coupée en `_parse_sheets`, `_parse_rows`, `_is_auto_increment`, `_column_name`, `_volatile_columns`, `_row_fields`, `_modified_rows`, `_sheet_diff`. Identique sur 400 tirages, sauf un `IndexError` corrigé (feuille sans ligne). Tests ajoutés.<br>**Fait** |

**Détails**

- **Y-60** — Position (suite) : Identique à l'ancien code sur 3000 cas.

### E. Chat (moteur, prompts, WebSocket)

| # | Origine · Fichier | Critique | Avis et proposition de Logan | Ma position · Statut |
|---|---|---|---|---|
| L-19 | Logan · `useChatConversation.ts:90`, `helpers.py:84-92`, `queries.py:25` | [Moyenne] Tours en échec renvoyés au modèle comme historique (ils comptent, et transforment une première question en « suivi » donc coupent la voie traduction seule) ; fenêtre de N−1 messages … | — | Nettoyage côté serveur (`_clean_messages`). Reste : ne plus renvoyer les bulles d'erreur côté navigateur, et l'arrêt serveur.<br>**Partiel** |
| Y-2 <sub>885059913</sub> | Yann · `README.md:84` | « Switch to 6 Luna » | **D'accord** : les cibles surchargent la réécriture (voir L-25).<br>Proposition : Choisir un modèle et aligner les docs. | Pont de traduction, jugement et résumé de la comparaison (texte et images) sur `databricks-gpt-6-luna` ; repli `databricks-gpt-5-6-luna` (chat : `app.yaml` ; comparaison : l'autre modèle, `compare_chain`). Réécriture du chat : voir point 4. …<br>**Fait** |
| Y-5 <sub>885070557</sub> | Yann · `language_reminder_named.md:1` | « Pas compris pourquoi deux » | **Désaccord** : deux cas distincts (langue détectée / non détectée), documenté. | D'accord avec Logan. Un seul fichier changerait la formulation du cas « inconnu », donc le comportement mesuré. Raison écrite dans `server/prompts/README.md`.<br>**Pas de changement** |
| Y-6 <sub>885071187</sub> | Yann · `language_reminder_named.md:1` | « Plutôt un fichier de config .py » | **Désaccord** : les `.md` sont enregistrés dans le registre de prompts et éditables sans code. | Règle du projet : un prompt du chat est un fichier (le hash lie la version enregistrée à la trace). Ce qui varie va en config Python (voir Y-7).<br>**Pas de changement** |
| Y-7 <sub>885138943</sub> | Yann · `as.md:1` | « Variabiliser pour réutiliser le même texte entre IS/AS » | **D'accord** : texte répété, un gabarit `{division}` ; garder en `.md`.<br>Proposition : Un gabarit + bloc d'ordre propre à AS. | `as.md` et `is.md` remplacés par `system/division.md` (gabarit rempli par `division_prompt()` et `DIVISION_NAMES` de `engine/config.py`) ; règle QP puis MI dans `as_reference_ordering.md`. …<br>**Fait** |
| Y-8 <sub>885138148</sub> | Yann · `base.md:2` | « Un petit langdetect en amont ? » | **En partie** : existe déjà (fastText) ; bloc réductible à une ligne de repli.<br>Proposition : Ramener le bloc « langue » à une ligne. | Bloc « langue » de `base.md` ramené à deux lignes (réponse dans la langue de la question sauf demande contraire ; citation dans la langue d'origine avec traduction). Détection fastText et rappel nommé déjà en place. …<br>**Fait** |
| Y-9 <sub>885145601</sub> | Yann · `helpers.py:13` | « Réfléchir en tokens plutôt que conversation turn ? » | **D'accord** : dix messages peuvent être énormes ; bug réel de fenêtre N−1 (voir L-19). | `CHAT_MAX_HISTORY_TOKENS` (8000, `app.yaml`) remplace `CHAT_MAX_HISTORY` : messages récents qui tiennent, dernière question toujours gardée, jamais d'ouverture sur un message assistant, 4 caractères par jeton (pas de tokenizer).<br>**Fait** |
| Y-39 <sub>885271954</sub> | Yann · `translation_bridge.py:36` | « Model hardcodé ! » | **En partie** : lu dans `CHAT_TRANSLATE_ENDPOINT`, le défaut duplique `app.yaml` (comme Y-22). | Défauts regroupés dans `services/chat/config.py` (modèle GPT-6 Luna, voir Y-2), fixés aussi par `app.yaml`.<br>**Fait** |
| Y-43 <sub>885793573</sub> | Yann · `search.py:54` | « Danger ! Aucun avertissement qu'on coupe la query » | **En partie** : silencieux mais impact faible (plafond sous la limite, seule la question brute l'atteint).<br>Proposition : Journaliser un incident à la coupe. | Vrai : > 20 000 caractères coupés sans trace. Avertissement dans les logs, incident `query_truncated` (colonne `warnings`, `app_errors`), longueur d'origine dans `vs_calls[].truncated_from`, code ajouté à `schema.md`. Deux tests.<br>**Fait** |

**Détails**

- **L-19** — Critique (suite) : « Stop » n'arrête pas le serveur.
- **Y-2** — Position (suite) : Non mesuré : le jugement d'impact change de modèle sans comparaison des verdicts, à regarder sur DEV.
- **Y-7** — Position (suite) : Clés de registre changées : relancer `register_chat_prompts`.
- **Y-8** — Position (suite) : À mesurer sur DEV (réponses en mauvaise langue, questions courtes ou mixtes) ; relancer
  `register_chat_prompts`.

### F. Style et structure du code (config, docstrings, types, imports, fonctions longues)

| # | Origine · Fichier | Critique | Avis et proposition de Logan | Ma position · Statut |
|---|---|---|---|---|
| Y-10 <sub>885146344</sub> | Yann · `helpers.py:111` | « Same » (constante en haut) | **D'accord** : style. | `_SOURCE_FIELDS` remonte avec les autres (voir Y-11).<br>**Fait** |
| Y-11 <sub>885146016</sub> | Yann · `helpers.py:36` | « Bouge les constantes au début » | **D'accord** : style. | `_CITE_MARKER_TMPL`, `_PROTECTED_SPAN_RE`, `_SOURCE_FIELDS`, `CHAT_MAX_HISTORY_TOKENS` et motifs de division en tête de module.<br>**Fait** |
| Y-12 <sub>885147969</sub> | Yann · `persistence.py:8` | « Import absolu c'est mieux je pense » | **Désaccord** : les relatifs explicites sont corrects (PEP 8), churn sans gain. | Écart assumé avec Logan : demande de Yann et règle de `CLAUDE.md`. `from server.…` dans 53 fichiers, conversion par script, code compile, tests passent.<br>**Fait** |
| Y-13 <sub>885149621</sub> | Yann · `sessions.py:13` | « Relatif… » | **Désaccord** (comme Y-12). | Même changement que Y-12 (écart assumé).<br>**Fait** |
| Y-14 <sub>885148302</sub> | Yann · `persistence.py:19` | « Doc function! » | **D'accord** : c'est la fonction sans contrôle de propriétaire (voir L-5).<br>Proposition : Corriger le contrôle et documenter. | Docstring de `_upsert_session` avec paramètres ; contrôle de propriétaire ajouté (L-5).<br>**Fait** |
| Y-15 <sub>885148691</sub> | Yann · `persistence.py:47` | « Missing sphynx params ! A check dans toute la base de code » | **D'accord** : standard d'équipe, le code avait des docstrings en prose. | Docstrings Sphinx (`:param`, `:return:`) sur toutes les fonctions de `server/` (et `utils/`) ; `check_docstrings.py` = 0 problème. Passe sur `server/services/` (51 fichiers, ~460 fonctions) : AST identique à celui d'avant. …<br>**Fait** |
| Y-16 <sub>885148761</sub> | Yann · `persistence.py:30` | « Type » | **D'accord**.<br>Proposition : `pool: asyncpg.Pool` | `pool: asyncpg.Pool`, `conn: asyncpg.Connection`, `sources: List[dict] \| None` (`persistence.py`, `sessions._turn_trace_id`).<br>**Fait** |
| Y-17 <sub>885246366</sub> | Yann · `ws.py:159` | « 1 seul if » | **Désaccord** : le `if` interne a son propre `else` (flux vide), fusionner changerait le comportement. | Fait en gardant le comportement : `if not (…)` unique ; fin de flux dans `_finish_turn()` (flux sans texte en sortie anticipée, puis cas normal sans `else`).<br>**Fait** |
| Y-18 <sub>885245561</sub> | Yann · `ws.py:16` | « Changer le nom de ma var de base » | **D'accord** : renommer à la définition.<br>Proposition : `ENABLED` → `TRANSLATE_BRIDGE_ENABLED` | Renommé (défini dans `services/chat/config.py`), alias `as` supprimé de `ws.py`, `routers/chat/__init__.py` et des notebooks d'évaluation.<br>**Fait** |
| Y-19 <sub>885248209</sub> | Yann · `analyze.py:57` | « Please ptit comments pour expliquer » | **D'accord** (et le sémaphore est relâché à la déconnexion, voir L-11). | Commentaires sur le sémaphore, `_SSE_HEADERS`, `_sanitize_messages`, docstrings du module. Le sémaphore lui-même reste ouvert (L-11).<br>**Fait** |
| Y-20 <sub>885252966</sub> | Yann · `common.py:86` | « Docstrings + param descriptions » | **D'accord** : style. | Fait.<br>**Fait** |
| Y-21 <sub>885252414</sub> | Yann · `common.py:59` | « Valeurs par défaut dans un fichier de config perso. À discuter » | **D'accord** (voir Y-22). | Chaque défaut de `get_config()` devient une constante `DEFAULT_*` dans `compare/config.py`.<br>**Fait** |
| Y-22 <sub>885251487</sub> | Yann · `common.py:18` | « Config file ! ou alors ton common = config ? » | **D'accord** : prompt par défaut différent de celui d'`app.yaml`, `APP_VERSION` à deux valeurs ; un seul module de réglages. … | `config.py` (réglages, `APP_VERSION`, taille max, analyses simultanées, prompt par défaut, `get_config()`) et `common.py` (validation, noms sûrs, `_is_within_volume`) ; imports mis à jour. …<br>**Fait** |
| Y-23 <sub>885255107</sub> | Yann · `impact.py:72` | « Fonction à split ? t'en penses quoi ? » | **D'accord** : 160 lignes, helpers testables ; ajouter la garde de cache (voir L-16).<br>Proposition : Helpers au niveau module. | Oui, mais pas en morceaux : `_search`, `_audit`, `_warn`, `_judges` deviennent méthodes de `_ImpactSearch` ; `_impact_events` est un générateur de module ; `find_impacted_documents` ~50 lignes. Garde de cache : ouvert (L-16).<br>**Fait** |
| Y-24 <sub>885254220</sub> | Yann · `common.py:114` | « Boolean complexe à détailler en variables » | **D'accord** : lisibilité seule, logique correcte. | `climbs_out`, `is_root`, `is_below_root`.<br>**Fait** |
| Y-25 <sub>885251206</sub> | Yann · `analyze.py:123` | « Casser la fonction trop longue + ajouter les rtype » | **D'accord** : ~92 lignes avec fermetures, peu de types de retour. | 290 lignes → ~60 : `_AnalysisJob`, `_read_uploads`, `_upload_to_volume`, `_acquire_slot`, `_keepalive_until_done`, `_track_llm_event`, `_analysis_events` ; types de retour sur tout `server/routers/` …<br>**Fait** |
| Y-26 <sub>885259506</sub> | Yann · `exports.py:101` | « Optional[Type] ou Type \| None et applique partout » | **D'accord** : `T \| None`. | `Type \| None` (Python ≥ 3.11) partout dans `server/` (34 fichiers), imports `Optional` retirés ; annotations entre guillemets en `'X \| None'`.<br>**Fait** |
| Y-27 <sub>885261374</sub> | Yann · `feedback.py:18` | « Pq str alors ? Enforcer up or down » | **D'accord**.<br>Proposition : `Literal['up', 'down']` | `vote: Literal['up', 'down']` dans les deux modèles de `feedback.py` et dans celui de `chat/sessions.py` ; valeur invalide = 422.<br>**Fait** |
| Y-28 <sub>885261904</sub> | Yann · `feedback.py:26` | « T'aurais plus besoin de ces checks » | **D'accord** : Pydantic fait déjà le 422 ; le vrai problème est L-13. | Contrôles manuels supprimés ; propriété du message vérifiée (L-13).<br>**Fait** |
| Y-29 <sub>885262081</sub> | Yann · `feedback.py:62` | « Same! » | **D'accord** (comme Y-27). | Même suppression pour le second endpoint et pour le chat.<br>**Fait** |
| Y-30 <sub>885262563</sub> | Yann · `history.py:20` | « Config file » | **En partie** : objet réponse global, mieux une petite fonction qui renvoie une réponse neuve.<br>Proposition : `_unavailable()` | `APP_VERSION` s'importe de `compare/config.py` ; le 2026-10-21 `_UNAVAILABLE` devient la fonction `_unavailable()`.<br>**Fait** |
| Y-31 <sub>885266896</sub> | Yann · `history.py:47` | « Pas dict mais dict[Type, Type]… TypedDict c'est mieux » | **D'accord** : basse priorité. | `_serialize(row: Mapping[str, Any]) -> dict[str, JsonScalar]`. Pas de `TypedDict` : la fonction sert à `SELECT *` et à des listes de colonnes, clés non fixes.<br>**Fait** |
| Y-32 <sub>885267451</sub> | Yann · `history.py:144` | « Class pour 1 var ? » | **Désaccord** : un modèle Pydantic est la façon normale de recevoir un corps JSON. | Écart assumé : `SaveImpactRequest` supprimée, la route lit `impact_json: str = Body(..., embed=True)`. JSON du navigateur inchangé (vérifié).<br>**Fait** |
| Y-33 <sub>885269686</sub> | Yann · `engine/config.py:67` | « En haut » | **D'accord** : style. | `_RAW_ON_ALL` et `_RAW_ON` remontent.<br>**Fait** |
| Y-34 <sub>885269518</sub> | Yann · `engine/config.py:32` | « config == config pas common » | **D'accord** : renommer `common.py` de la comparaison. | Compris comme « un module de réglages s'appelle `config` » : `engine/config.py` l'était, `common.py` de la comparaison est corrigé (Y-22). À confirmer si je me suis trompé.<br>**Fait** |
| Y-35 <sub>885270078</sub> | Yann · `queries.py:49` | « Config » | **En partie** : liste de phrases-bruit, constante nommée en haut suffit. | `_COMPANY` et `_NOISE_PHRASES` passent dans `engine/config.py` (`NOISE_PHRASES`).<br>**Fait** |
| Y-36 <sub>885271146</sub> | Yann · `mlflow_turns.py:119` | « En haut + config » | **En partie** : en haut oui, une regex de parsing n'est pas de la config. | Import de `translation_bridge` en tête ; limites d'export `MLFLOW_*` dans `services/chat/config.py` ; `_fast_lang_guess` → `fast_lang_guess` (publique) ; tests et notebooks mis à jour.<br>**Fait** |
| Y-37 <sub>885271431</sub> | Yann · `mlflow_turns.py:287` | « Pas cool à lire » | **D'accord** : conditionnel dans une f-string.<br>Proposition : Construire le libellé avant. | Span `rewrite` avec variables nommées ; libellé des appels Vector Search par `_vs_call_label()`.<br>**Fait** |
| Y-38 <sub>885272147</sub> | Yann · `translation_bridge.py:67` | « Config! » | **Désaccord** : regex de nettoyage, pas des réglages. | Écart assumé : motifs, seuils de détection et noms de langues déplacés dans `services/chat/config.py`.<br>**Fait** |
| Y-40 <sub>885272670</sub> | Yann · `turn_log.py:33` | « Config » | **Désaccord** : regex de parsing / vocabulaire. | D'accord avec Logan. `INFO`, `WARNING`, `ERROR` sont le vocabulaire du journal de tour (importé par `ws.py`, le moteur, `lakebase`), pas des réglages par cible ; en config, le journal dépendrait d'un module de réglages. …<br>**Pas de changement** |
| Y-41 <sub>885789675</sub> | Yann · `retrieval.py:52` | « Pas super lisible mais bon à voir » | **D'accord** : `setdefault` dans une compréhension.<br>Proposition : Écrire en boucle. | `refs_named_in`, `named` dans `retrieve_for_turn` et `one_language_per_document` simplifiés (liste dédupliquée par `dict.fromkeys`, boucle explicite). Comportement identique, deux tests.<br>**Fait** |
| Y-42 <sub>885790057</sub> | Yann · `retrieval.py:57` | « Any c'est pas incroyable. Si possible des TypedDict » | **D'accord** : basse priorité. | Nouveau `engine/types.py` : `SearchRow`, `Hit`, `PassageRecord`, `DocumentPassages`. Filtres et corps de requêtes gardent `Dict[str, Any]`. Pas de typage statique outillé : ces types documentent sans être vérifiés.<br>**Fait** |
| Y-49 <sub>886269905</sub> | Yann · `factory.py:89` | « Param sous cette forme :param: » | **Pas jugé** | Bloc `Args:` de `get_processor` supprimé, contenu repris dans le `:param`. Pas de `:rtype:` (annotations de retour seulement, décidé).<br>**Fait** |
| Y-51 <sub>885864584</sub> | Yann · `extract.py:27` | « Fonction trop longue à split » | **Pas jugé** | `_extract_docx` (200 lignes, 6 fonctions imbriquées) → classe `_DocxWalk` + `_oriented_image` + deux fonctions de module. Même résultat sur 3 documents de test.<br>**Fait** |
| Y-55 <sub>886274477</sub> | Yann · `soffice.py:126` | « Comments » | **Pas jugé** | Commentaires ajoutés (marqueur de fin, dossier remis à zéro, téléchargement par blocs, confiance dans l'archive, bit exécutable).<br>**Fait** |
| Y-56 <sub>886275266</sub> | Yann · `summarize.py:18` | « Mérite un fichier de config » | **Pas jugé** | Nombres fait : `SUMMARY_LLM_TIMEOUT_S` dans `services/compare/config.py`. Prompts de résumé en fichiers (`server/prompts/compare/summary.md`, `summary_image.md`, via `compare/prompts.py`).<br>**Fait** |
| Y-57 <sub>886276049</sub> | Yann · `markdown.py:58` | « Détailler avec un comment ce que ça fait » | **Pas jugé** | Commentaire sur la regex du Markdown en ligne : ordre des alternatives et raison.<br>**Fait** |
| Y-58 <sub>886277833</sub> | Yann · `vector_search.py:49` | « Pareil config » | **Pas jugé** | Nombres fait : délais, parallélisme, reprises, plafonds de passages et jetons dans `services/infra/config.py` (`IMPACT_*`, `VECTOR_SEARCH_QUERY_TIMEOUT_S`), importés par la recherche d'impact et le moteur du chat. …<br>**Fait** |
| Y-59 <sub>885862606</sub> | Yann · `relocation.py:196` | « Mettre en haut » | **Pas jugé** | Trois constantes (`_RELOCATION_COVERAGE_THRESHOLD`, `_RELOCATION_MIN_RUN_WORDS`, `_STRIKETHROUGH_RE`) en tête de module.<br>**Fait** |
| Y-62 <sub>885860992</sub> | Yann · `engines.py` (`section_canonical_diff`) | Bloc `Returns:` de style Google (même remarque que Y-49) | **Pas jugé** | Bloc supprimé, le `:return:` le disait déjà.<br>**Fait** |

**Détails**

- **Y-15** — Position (suite) : À relire en priorité : `engine/answer.py` `revision_note`, `export_helpers.py` `_build_excel_bytes`,
  `relocation.py` (`_drop_present_verbatim`, `_align_duplicates`), `error_origin.py` `caller`, `images.py` `_compute_image_diff_pairs`,
  `engines.py` `_order_key`, `vector_search.py` `run_impact_search`.
- **Y-22** — Avis et proposition (suite) : Proposition : Un module de réglages ; prompts de la comparaison dans `server/prompts/`. ;
  Position (suite) : Les six prompts de la comparaison sont des fichiers (`server/prompts/compare/`, chargés par `compare/prompts.py`,
  `load_prompt`), texte identique, surcharge `COMPARE_ANALYSIS_SYSTEM_PROMPT` conservée.
- **Y-25** — Position (suite) : `test_compare_analyze_stream.py` passe aussi sur l'ancien code. `:rtype:` non ajouté (annotations seulement,
  décidé suite à Logan).
- **Y-40** — Position (suite) : À discuter si Yann y tient.
- **Y-58** — Position (suite) : Prompt du juge en fichier (`server/prompts/compare/impact_judge.md`).

### G. Dépôt, CI/CD, déploiement, dépendances

| # | Origine · Fichier | Critique | Avis et proposition de Logan | Ma position · Statut |
|---|---|---|---|---|
| Y-1 <sub>885061633</sub> | Yann · `settings.json:1` | « Exclure le .vscode non ? » | **En partie** : `settings.json` = réglages personnels ; `tasks.json` sert à l'équipe.<br>Proposition : Exclure `settings.json`, garder `tasks.json`. | `settings.json` sort du suivi et entre dans `.gitignore` ; `tasks.json` reste (tâche « Pull + Deploy » du README).<br>**Partiel** |
| Y-3 <sub>885062343</sub> | Yann · `index.html:1` | « Pourquoi en double avec client/index.html » | **En partie** : pas un doublon (source Vite / build) ; le vrai problème est de versionner le build.<br>Proposition : Construire au déploiement, ignorer `out/`. | Pas un doublon : `client/index.html` est la source, `out/` le build (balises plus scripts au nom haché). …<br>**Fait** |
| Y-52 <sub>885869279</sub> | Yann · `8859-1.txt:69` | « C'est quoi ces trucs ? À garder dans la PR ? » | **Pas jugé** | Garder : tables de caractères et exécutable d'antiword, lus par le job de parsing (`ANTIWORDHOME`) que le cluster ne peut pas installer. L'app installe le `.deb` (`start.sh`). …<br>**Pas de changement** |
| L-9 | Logan · `bitbucket-pipelines.yml:312-354`, `bitbucket_deploy.sh:49-62`, `databricks.yml:499` | [Haute] PROD déployable depuis toute branche, aucun test en CI, CLI installée par script non épinglé (`main`), jeton longue durée. | Tests d'abord, UAT sur `main`, PROD manuel ; environnement de déploiement PROD limité à `main` ; épingler la CLI ; OAuth/OIDC. | Tests avant les déploiements UAT et PROD (DEV sans, pour itérer), après installation de `requirements.txt` épinglé ; garde de branche PROD déjà là. …<br>**Partiel** |
| L-20 | Logan · `alerts.yml:82-126`, `03_chat_failure_rate.sql`, `lakebase_sync.yml:124` … | [Moyenne] Alertes qui ne peuvent pas se déclencher (copie Delta rafraîchie 2 fois par jour, 22 runs sur 24 voient 0 ligne), pas de job d'export en PROD, tableaux DEV figés … | — | Ressources Databricks à traiter à part.<br>**Ouvert** |
| L-25 | Logan · `target_env.json`, `app.yaml`, `databricks.yml`, `test_deploy_config.py:22`, READMEs | [Moyenne] Config et docs incohérentes : réécriture Gemini vs « Luna seulement », `CHAT_TRANSLATE_ONLY` documenté « off », fichiers cités absents, README CI faux, README du moteur vs `base.md` … | — | Réécriture tranchée (point 4) ; README CI corrigé. Le reste est à traiter.<br>**Partiel** |
| L-26 | Logan · `leap_utils/` | [Moyenne] 12 272 lignes copiées, 78 % jamais atteint (le pipeline utilise 7 modules, 2 628 lignes), risque de dérive. | Dépendre d'un paquet épinglé ou ne copier que les modules utilisés. | Réduit aux 11 modules que le pipeline de parsing atteint (3 195 lignes gardées, 23 fichiers / 9 077 lignes supprimés), voir `leap_utils/README.md`. Un paquet épinglé reste possible plus tard.<br>**Fait** |
| L-28 | Logan · `requirements.txt`, `pyproject.toml`, `start.sh:31-45`, `bitbucket_deploy.sh:49`, `.mcp.json` | [Moyenne] Dépendances non reproductibles : `>=` ≠ `uv.lock`, bibliothèques de jobs non épinglées, `.deb` antiword en `http://` sans somme de contrôle, `@playwright/mcp@latest`. | — | `requirements.txt` : toutes les dépendances directes épinglées aux versions des tests (pymupdf 1.28.0, Pillow 11.1.0) ; `start.sh` : `.deb` antiword en https avec SHA-256, paquet amd64 seul (arm64 : paquet fourni ou apt). …<br>**Partiel** |
| L-b13 | Logan · `client/out`, `.gitleaksignore` | [Basse] `client/out` versionné, jamais reconstruit par la CI, exclu du scan de secrets. | Construire au déploiement. | Fait : voir Y-3 (construit au déploiement, plus versionné).<br>**Fait** |
| L-b16 | Logan · `databricks.yml`, YAML des jobs | [Basse] Identités individuelles (dont un compte externe) nommées en dur. | Groupes et variables. | D'accord.<br>**Ouvert** |

**Détails**

- **Y-3** — Position (suite) : `client/out` n'est plus versionné (`.gitignore`, retiré de git) : chaque déploiement le construit avec bun
  (`bitbucket_deploy.sh`, version 1.4.2 épinglée ; `deploy_qualibot.ps1` le faisait déjà) et les règles de sync (`include client/out/**`,
  `--include`) l'envoient.
- **Y-52** — Position (suite) : Seule `UTF-8.txt` est demandée par le code ; les autres peuvent partir si tu veux une PR plus courte.
- **L-9** — Position (suite) : Version de la CLI lue dans `utils/deploy/databricks_cli_version.txt` (vide pour l'instant = dernière, avec
  avertissement, en attente de `databricks version`). OAuth/OIDC : ouvert. Restreindre l'environnement Bitbucket `Production` à `main` : à
  faire par toi (Repository settings > Deployments).
- **L-20** — Fichiers (suite) : `import_lakebase_from_json.py:149-153` ; Critique (suite) : import UAT→PROD qui échoue sur table manquante.
- **L-25** — Critique (suite) : repli « rewrite failed » mort, volumes `uat_landingzone`, pas de `prevent_destroy`, test `uat-test`.
- **L-28** — Position (suite) : `.mcp.json` absent de ce dépôt. Dépendances transitives non verrouillées (pas de `uv.lock`) : ouvert.

### H. Client et interface

| # | Origine · Fichier | Critique | Avis et proposition de Logan | Ma position · Statut |
|---|---|---|---|---|
| L-24 | Logan · `useHistoryLoad.ts:46-51`, `useChatConversation.ts:34-56`, `useAnalysisTrack.ts:80`, `config.ts:68-70` … | [Moyenne] Courses (historique A puis B, « Nouveau chat », résumé du fichier précédent), `running` bloqué si le hachage échoue, pas de watchdog du chat, suppression sans erreur, `/api/me` en échec = accès complet. | — | Besoin de reconstruire `client/out`.<br>**Ouvert** |
| Y-4 <sub>885062844</sub> | Yann · `databricks-symbol-color.svg` | « Pareil logos en double ? » | **En partie** : même cause que Y-3 ; réduire le favicon de 1 Mo.<br>Proposition : Réduire `LOGO_Qualibot.png`. | Ce fichier n'existe pas dans mon dépôt (il reste `LOGO_LATECOERE.png` et `LOGO_Qualibot.png`) ; `out/` : voir Y-3 (plus versionné). Favicon : à réduire. Si le fichier existe chez toi, envoie son chemin exact.<br>**Ouvert** |
| L-b14 | Logan · `client/` | [Basse] Bundle JS de 1,14 Mo en un morceau, `pdfjs-dist` inutilisé, favicon de 1 Mo, polices Google chargées à l'exécution. | — | D'accord.<br>**Ouvert** |

**Détails**

- **L-24** — Fichiers (suite) : `useChatSessions.ts:27-31`

### I. Données, RGPD, conformité

| # | Origine · Fichier | Critique | Avis et proposition de Logan | Ma position · Statut |
|---|---|---|---|---|
| L-8 | Logan · `bitbucket-pipelines.yml:262-286`, `ops_config.py:39` | [Haute] Données de chat et d'utilisateurs hors plateforme : artefacts Bitbucket du pipeline d'export, jetons de partage copiés en Delta, DEV chargé depuis des instantanés UAT. | Passer par un volume UC ou un job base à base ; jeton créé au clic sur Partager ; exclure `share_token` des exports et les faire tourner ; décider si l'historique va en environnement bas. | Le pipeline `sync-prod-data` ne copie plus que le volume des modèles docling ; `export_lakebase_to_json.py`, `import_lakebase_from_json.py` et leurs constantes (`ops_config.py`) supprimés. …<br>**Fait** |
| J-1 <sub>885301989</sub> | Jules · `export_lakebase_uat_to_volume_job.py:1` | « Les jobs là sont refaits… » (plus de `uat` dans les noms, un job par cible qui copie Lakebase{env} → UC{env}) | Confirmé sur `origin/DEV` (la partie export Bitbucket restait, voir L-8). | Déjà fait avant la revue (commit `97f8853` : `lakebase_export`, `export_lakebase_to_volume`, `import_lakebase_tables_to_delta`). Plus aucune référence `export_lakebase_uat*`.<br>**Fait** |
| L-12 | Logan · `analyze.py:288`, `schema.py:391-394`, `ws.py:90-92`, `storage.ts:63-74` | [Moyenne] Pas de rétention ni d'effacement par utilisateur : textes complets dans `llm_requests`, passages dans `chat_retrieved_chunks`, questions dans les logs et MLflow, suppressions « douces » … | — | `user_id` ajouté à `llm_requests` (colonne, index, insertion d'audit, route d'analyse). Pas de job de purge (décidé). …<br>**Partiel** |
| L-b15 | Logan · antiword, modèle fastText | [Basse] Notices de licence manquantes (antiword GPL-2, modèle fastText CC-BY-SA). | Ajouter les notices. | D'accord.<br>**Ouvert** |

**Détails**

- **L-8** — Position (suite) : Jetons de partage inchangés ; chats DEV = vraies données DEV (accepté).
- **L-12** — Critique (suite) : `llm_requests` sans `user_id`, `localStorage`. ; Position (suite) : `localStorage` effacé 7 jours après la
  dernière écriture (`clearExpiredStorage`, `storage.ts`) ; l'historique en base recharge toujours l'entrée (analyse, impact, fichiers du
  volume). Reconstruire le client (fait au déploiement).

## Points en suspens

### 1. Données hors plateforme (L-8)

- **Le problème** : des données de conversations d'utilisateurs sortaient de leur environnement : fichiers conservés par Bitbucket après le
  pipeline d'export, jetons de partage (les liens qui ouvrent une conversation) copiés dans des tables, environnement de développement (DEV)
  rempli avec des copies de l'UAT.
- **Options et conséquences** : décidé le 2026-10-21. Lakebase (la base de l'application) ne quitte plus son environnement : le pipeline
  `sync-prod-data` ne copie plus que le volume des modèles docling, et les scripts d'export/import JSON et leurs constantes sont supprimés.
  Les jetons de partage restent inchangés (chaque environnement garde les siens). Les conversations de DEV sont de vraies conversations de
  DEV.
- **Avis de Logan** : retirer `staging/**` des artefacts ; créer le jeton au clic sur Partager et l'exclure des exports ; dire si
  l'historique peut aller dans un environnement bas.
- **Décision / état** : scripts supprimés ; jetons et conversations de DEV laissés tels quels. **Fait**

### 2. Rétention et RGPD (L-12)

- **Le problème** : les textes complets des documents et des passages retrouvés restent en base sans limite de durée, et le navigateur en
  garde des copies.
- **Options et conséquences** : `user_id` ajouté à `llm_requests` (colonne, index, insertion d'audit), ce qui permet de retrouver les
  données d'une personne. Pas de job de purge. Le stockage du navigateur (`localStorage`) est effacé 7 jours après la dernière écriture
  (`clearExpiredStorage`) ; l'historique en base recharge l'entrée avec analyse, impact et fichiers du volume.
- **Avis de Logan** : fixer une durée de conservation pour `llm_requests.messages_json` et `chat_retrieved_chunks` ; traiter `localStorage`.
- **Décision / état** : `user_id` ajouté ; pas de purge ; expiration à 7 jours du stockage du navigateur (client à reconstruire, fait au
  déploiement). **Fait**

### 3. `client/out` versionné ou construit au déploiement

- **Le problème** : `client/out` (le site compilé) était dans git : il pouvait dériver du code source, n'était pas reconstruit par la CI et
  échappait au scan de secrets.
- **Options et conséquences** : option B retenue, construire au déploiement avec bun 1.4.2 épinglé (`bitbucket_deploy.sh` ;
  `deploy_qualibot.ps1` le faisait déjà). `client/out` est ignoré et retiré de git ; les règles de synchronisation (`include client/out/**`,
  `--include`) ne changent pas.
- **Avis de Logan** : construire au déploiement et ignorer `out/`.
- **Décision / état** : construit au déploiement (option B). **Fait**

### 4. Modèle de réécriture du chat

- **Le problème** : les cibles remplaçaient le modèle de réécriture par un autre, alors que la documentation annonçait « Luna seulement ».
- **Options et conséquences** : GPT-6 Luna sur toutes les cibles ; surcharges Gemini retirées de `utils/deploy/target_env.json` et README
  corrigé.
- **Avis de Logan** : choisir un modèle et aligner la documentation.
- **Décision / état** : GPT-6 Luna partout. **Fait**

### 5. Bloc « langue » de `base.md`

- **Le problème** : Yann demande une détection de langue en amont. Elle existe déjà (fastText détecte la langue et ajoute un rappel nommé),
  mais `base.md` gardait en plus un long bloc de règles de langue.
- **Options et conséquences** : bloc ramené à deux lignes (réponse dans la langue de la question sauf demande contraire ; citation dans la
  langue d'origine avec traduction). Risque : réponse dans la mauvaise langue sur des questions courtes ou mixtes.
- **Avis de Logan** : couper à une ligne de repli.
- **Décision / état** : bloc réduit ; mesure sur DEV à faire, puis `register_chat_prompts`. **Fait**

### 6. CI (intégration continue)

- **Le problème** : la CLI Databricks est installée sans version fixée, le jeton de déploiement est longue durée, et les tests ne bloquent
  pas les déploiements.
- **Options et conséquences** : tests avant les déploiements UAT et PROD (pas DEV), avec `requirements.txt` épinglé ; 5 échecs ont disparu
  (pymupdf épinglé à 1.26.7, fausse connexion corrigée) : 597 réussis. La version de la CLI est lue dans
  `utils/deploy/databricks_cli_version.txt` par `bitbucket_deploy.sh` ; vide pour l'instant, ce qui prend la dernière version avec un
  avertissement. OAuth/OIDC (authentification sans jeton longue durée) demande une configuration Databricks et Bitbucket hors dépôt.
- **Avis de Logan** : épingler la CLI, passer à OAuth/OIDC, lancer les tests avant les déploiements.
- **Décision / état** : tests et lecture de la version faits ; attente de la sortie de `databricks version` ; OIDC ouvert ; restriction de
  l'environnement `Production` à `main` à faire par toi (Repository settings > Deployments). **Partiel**

### 7. Dépendances et `leap_utils`

- **Le problème** : `requirements.txt` accepte n'importe quelle version plus récente (`>=`), le `.deb` d'antiword est téléchargé en
  `http://` sans somme de contrôle, et `leap_utils` est une copie de 12 272 lignes dont environ 78 % ne servent jamais.
- **Options et conséquences** : option A pour les deux. `requirements.txt` épinglé aux versions testées ; `.deb` en https avec SHA-256
  (amd64 seul) ; `leap_utils` réduit aux 11 modules atteints (3 195 lignes). `.mcp.json` est absent de ce dépôt. Les dépendances transitives
  ne sont pas verrouillées.
- **Avis de Logan** : dépendre d'un paquet épinglé ou copier module par module ; épingler les dépendances et vérifier le `.deb`.
- **Décision / état** : épinglage direct et `leap_utils` réduit (option A). **Fait**

### 8. Prompts de la comparaison et `:rtype:`

- **Le problème** : les prompts de la comparaison étaient des chaînes dans le code Python, contrairement à ceux du chat qui sont des
  fichiers. Yann demande aussi la mention `:rtype:` dans les docstrings.
- **Options et conséquences** : six prompts mis en fichiers (`server/prompts/compare/`, chargés par `compare/prompts.py`, `load_prompt`),
  texte identique, surcharge `COMPARE_ANALYSIS_SYSTEM_PROMPT` gardée. `:rtype:` non ajouté : les annotations de retour suffisent.
- **Avis de Logan** : prompts de la comparaison en fichiers.
- **Décision / état** : prompts en fichiers ; annotations seulement (suite à Logan). **Fait**

## Vérifications

- **Lot 1** : `pytest tests` : 527 réussis, 5 échecs, 12 ignorés ; les 5 échecs (`test_compare_documents` ×3, `test_lakebase_carry_over` ×2)
  existaient avant mes changements, sur ce poste (module PDF, base locale). Essai des routes avec le client de test : `PUT
  /history/{id}/impact` (corps `{"impact_json"}` accepté, corps vide refusé), `POST /feedback` et `/chat/feedback` avec un vote invalide
  (422), `/compare/analyze` et `/compare/impact` sans réglages (erreurs claires).
- **Lot 2** : tests ajoutés pour `refs_named_in` et `one_language_per_document`, la coupe de requête (`query_truncated`), antiword (UTF-8,
  repli), la borne olefile et le diaporama de 3 diapositives avec un vrai LibreOffice (`tests/test_conversion.py`, 10 tests).
- **Lot 3** : `tests/test_excel_processor.py` est nouveau ; les trois découpages (docx, excel, relocation) ont été comparés à l'ancien code : mêmes sorties.
- **Ensemble (état actuel)** : 597 réussis, 12 ignorés, 0 échec avec `requirements.txt` épinglé (pymupdf 1.28.0, Pillow 11.1.0) ; avec
  pymupdf 2.0, les 3 tests PDF échouent ; `check_docstrings.py` : 0 problème. Rien n'a été déployé ni exécuté sur Databricks ; étapes dans
  `runbooks/retest.md`, section « revue de la PR Bitbucket 2 ».

## Reste ouvert (hors décisions)

Constats de Logan marqués Ouvert (ou partiels), sans décision à prendre de ta part :

- L-3 : suppression des lignes de checkpoint hors manifeste (simple ajout).
- L-6 : formules Excel (`data_only=True`).
- L-11 : regex des REF sur la boucle d'événements, sémaphore compare libéré à la déconnexion, client HTTP partagé.
- L-15 : écrire le contrôle accepté (accès au service principal) ; filtrer les puces du catalogue sur `in_chat` et la division.
- L-16 : recherche d'impact (sections coupées, garde de cache `queries_failed > 0`, citation inventée, auto-exclusion).
- L-17 : gros documents découpés ; Excel jamais découpé.
- L-19 : ne plus renvoyer les bulles d'erreur depuis le navigateur ; arrêt serveur sur « Stop ».
- L-20 : alertes, job d'export PROD, tableaux DEV, import UAT→PROD.
- L-21 : robustesse et coût du pipeline (`redescribe_all_images`, prune silencieux, attente d'index, délais maximaux, cache des
  descriptions, PNG/jpeg, planification PROD).
- L-24 : courses côté client (nécessite de reconstruire `client/out`).
- L-25 : reste de la config et des docs incohérentes (fichiers cités absents, README du moteur, repli mort, volumes `uat_landingzone`,
  `prevent_destroy`, test `uat-test`).
- Basses : L-b2, L-b4, L-b5, L-b6, L-b7, L-b8, L-b9, L-b10, L-b11, L-b12, L-b14, L-b15, L-b16 ; L-b17 pour le code client.
- Y-4 : réduire le favicon de 1 Mo (avec L-b14).
- Y-40 : `turn_log.py` garde ses niveaux de gravité dans le module du journal, à discuter si Yann maintient sa demande.
- Décisions et réglages hors dépôt ou à venir : version de la CLI fixée à 1.20.0 dans `utils/deploy/databricks_cli_version.txt` (à vérifier
  au premier déploiement) ; OAuth/OIDC à la place du jeton longue durée ; restriction de l'environnement Bitbucket `Production` à `main`
  (action à faire une fois tout validé) ; dépendances transitives non verrouillées (pas de `uv.lock`) ; L-b2 (CSP) ; L-24 (courses client) ;
  L-16 et L-17 (recherche d'impact, rapports découpés) ; L-20 et L-21 (alertes, coût du pipeline) ; mesure DEV de la langue (Y-8).
