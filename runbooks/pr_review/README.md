# Journal de revue — PR Bitbucket 2 (`latec-compare`)

**Statuts** (une remarque n'est traitée que si le changement demandé est fait ou si Jules a tranché) :

- `Fait` : le changement demandé est fait tel quel, ou Jules a pris une décision explicite (section « Décisions de Jules »).
- `Partiel` : une partie est faite, le reste est nommé dans la ligne.
- `Ouvert` : pas fait.
- `En attente de Yann` : réponse, explication ou autre solution que celle demandée, sans décision de Jules ; Yann ne l'a pas acceptée.
- `Décision de Jules` : attend un choix de Jules.

## Synthèse

| Statut | Yann | Logan | Logan v3 | Jules | Total |
|---|---|---|---|---|---|
| Fait | 52 | 39 | 19 | 1 | 111 |
| Partiel | 0 | 7 | 2 | 0 | 9 |
| Ouvert | 0 | 0 | 6 | 0 | 6 |
| En attente de Yann | 10 | 0 | 0 | 0 | 10 |
| Décision de Jules | 0 | 0 | 3 | 0 | 3 |
| **Total** | **62** | **46** | **30** | **1** | **139** |

## Décisions de Jules (2026-10-21 et 2026-10-22) et leurs raisons

| Sujet | Décision | Raison |
|---|---|---|
| Lakebase entre environnements | Jamais copié d'un environnement à l'autre ; `sync-prod-data` ne copie plus que les modèles docling | « Il n'y a besoin d'aucun job du Lakebase UAT vers DEV : le job DEV copie le Lakebase DEV, le job UAT copie le Lakebase UAT, c'est tout ce qu'il faut. » |
| Jetons de partage | Inchangés | « Chaque environnement garde ses share_tokens, cela aide à lire les chats plus facilement. » |
| Chats DEV | Vraies données DEV | « Ce n'est pas grave si on peut faire un select dessus. » |
| RGPD | `user_id` oui, pas de purge, `localStorage` 7 jours | « L'historique garde ceci dans Databricks. » |
| `client/out` | Construit au déploiement (option B) | Le build ne doit plus dériver du code source ni échapper à la CI. |
| Modèle de réécriture du chat | GPT-6 Luna sur toutes les cibles | Un seul modèle, documentation alignée. |
| Bloc « langue » de `base.md` | Ramené à deux lignes | La détection fastText existe déjà ; le bloc long était redondant. |
| Tableau de sources en fin de réponse | GARDÉ ; `CLAUDE.md` corrigé | « Je veux bien les réponses plus longues. » |
| Échec de la réécriture | Tour dégradé qui cherche avec la question brute, autant de candidats qu'un tour normal (48) | Meilleure expérience pour l'utilisateur. |
| CI | CLI épinglée ; tests bloquent UAT/PROD ; `Production` limité à `main` après validation ; OIDC plus tard | Déploiements reproductibles ; la restriction attend que tout soit validé ; OIDC demande l'administrateur de compte Databricks. |
| Dépendances et `leap_utils` | Option A pour les deux : épinglage ; réduction à 11 modules | Reproductibilité ; 78 % de la copie ne servait jamais. |
| Prompts de la comparaison | En fichiers ; annotations seulement (pas de `:rtype:`) | Suit Logan ; même règle que les prompts du chat. |
| Rôle Lakebase de PROD | `prevent_destroy` préparé | Protéger le rôle contre une suppression accidentelle. |
| Export Lakebase de PROD | Dé-pausé (à déployer avec PROD) | Les alertes de PROD lisent la copie Delta. |
| Adresse d'alerte DEV/UAT | Conservée (`jules.gourio.external`) | Décision de Jules. |
| Utilisateurs nommés de l'app UAT | `christophe.pelissier` et `yosif.karapendov` retirés | Vérifié : tous deux ont can_chat par les groupes. |
| Documentation API interactive | Désactivée | « On s'en fout. » |
| Polices web | Livrées avec le build du client (Fontsource) | L'apparence reste identique, plus d'appel à Google. |
| Tests | Aucune donnée stockée | « Je ne veux pas ces données dans les tests. » |
| Descriptions d'images (R1) | Réutilisées pour la même image et la même requête (option c) | La description dépend du contexte : on réutilise seulement si l'image et son texte de contexte sont identiques. |
| Re-description des images (R2) | Les passages sont reconstruits (option a) | Sinon on paie les appels sans effet sur le chat. |
| Auto-exclusion de la recherche d'impact (R4) | Option a : on accepte | « On s'en fout. » |
| Planning du parsing en PROD (R3) | Reste en pause (option b, « on verra plus tard ») | Après quelques runs manuels validés. |
| Origine des WebSockets (R5) | Adresses renseignées pour DEV et UAT ; PROD à faire | Le contrôle ne s'applique que si la liste est renseignée. |
| Checkpoint du parsing (R6) | Lignes supprimées pour les documents hors périmètre parsés il y a plus de 180 jours | Les plus récentes restent : un document qui revient n'est pas reparsé. |
| Coloration du code (R7) | Chargée à la demande | Première page ~510 Ko au lieu de 1,14 Mo. |
| Tableau de bord DEV (R8) | Plus tard, non fait | « Je le ferai plus tard, garde en non fait. » |
| OIDC et environnement `Production` (R9) | Plus tard | Demande un administrateur de compte Databricks ; restriction après validation. |

## Contexte

Un tableau par thème : une ligne par remarque (relecteur Yann, revue indépendante « Logan », commentaire de Jules). Les commentaires
Bitbucket se récupèrent avec `python debug\fetch_comment_pr.py` (copie de `runbooks/scripts/bitbucket_pr/fetch_pr_comments.py`). La PR est
faite sur le commit `14296499a19baf710dc6c10d74bdd71204176b2d` ; les numéros de ligne cités ne tombent pas toujours pile (les fichiers ont
bougé depuis) : chaque remarque a été rattachée au code par son sens. Relecteurs : Yann Depledt (commentaires inline) et Logan (fichier
`QualiBOT_PR_Review.html`, revue en lecture seule du commit `db1f6d7`, faite le 2026-10-20, puis revue « v3 » du commit `f4fbe99`, voir plus bas). Jules Gourio est l'auteur. Les nouveaux
commentaires s'ajoutent en bas du tableau de leur thème.

- **Origine** : `Yann` = commentaire inline Bitbucket (`Y-n`, id Bitbucket en petit après le numéro) ; `Logan` = constat de la revue
  indépendante (`L-1` à `L-29` = sa numérotation ; `L-b1` à `L-b17` = ses constats de gravité basse, dans l'ordre de sa liste) ; `Jules` =
  commentaire de l'auteur (`J-1`) ; `Logan v3` = constat de la troisième revue de Logan (`N-1` à `N-16` = ses nouveaux constats, `N-b1` à `N-b10` = ses constats de gravité basse, dans l'ordre de sa liste ; ses réévaluations des constats précédents sont une phrase « Logan v3 : … » à la fin de la ligne concernée). Quand Logan a aussi jugé un commentaire de Yann, son avis est dans la ligne de Yann. Logan a jugé les 48
  premiers commentaires de Yann (lots 1 et 2) ; le lot 3 est arrivé après.
- **Lecture des cellules** : « … » en fin de cellule = la suite est dans la liste `Détails` sous le tableau. Pour les lignes de Yann, la
  colonne de Logan donne son avis en gras puis sa proposition ; pour les lignes de Logan, elle donne sa correction proposée (`—` = rien).
- **Troisième revue de Logan (« v3 », 2026-10-23)** : relecture du commit `f4fbe99` (copie de `origin/DEV` à un réglage de parsing près) ;
  passe partielle, arrêtée à la demande (la comparaison des processeurs récrits, le parsing de format, le moteur de chat, `utils/app_mgmt` ne sont pas couverts).
  Logan n'a pas lancé la suite de tests (`fasttext-wheel` ne se construit pas sur son poste) ; chez nous, 767 tests réussissent.
  Les corrections du 2026-10-23 sont sur la branche `claude/sharp-mayer-uqwx8x`, rien n'a été déployé ni exécuté sur Databricks.
- Là où Logan est en désaccord avec Yann, le code suit Yann (c'est le relecteur), sauf mention. Rien n'a été déployé ni exécuté sur
  Databricks.

## Tableaux des remarques

### A. Sécurité et isolation des utilisateurs

| # | Origine · Fichier | Critique | Avis et proposition de Logan | Ma position · Statut |
|---|---|---|---|---|
| L-1 | Logan · `preview.py:26-40`, `conversion.py:189` | [Critique] `/api/preview` écrit l'upload à `join(tmp, filename)` : nom absolu ou `../` = écriture hors dossier (binaire LibreOffice, `client/out/index.html`). Route sans `require_compare`, taille ni type. | Nom fixe (`input.pptx`) ; `require_compare` + `_validate_file` sur les deux routes d'aperçu. | D'accord. Suffixe vérifié (`_SAFE_SUFFIX`), nom fixe, contrôles sur les deux routes, rendu hors boucle d'événements, erreurs génériques. Logan v3 : corrigé (nom fixe, contrôles sur les deux routes).<br>**Fait** |
| L-2 | Logan · `volume.py:51,89-140`, `exports.py:160-288` | [Critique] `session_path` (query) jamais vérifié : lecture et écrasement des documents des autres (`/compare/load`, `save-*`) ; dossiers nommés à la seconde (~86 000 essais par jour). | L'id d'historique vient du client, le chemin du serveur (`WHERE id AND user_id`) ; dossiers en UUID ; supprimer la copie `.tmp/{id}` inutile. | D'accord. Racine par utilisateur + dossier UUID, `owns_session` sur `load` et `save-*` (403), copie `.tmp` retirée. Logan v3 : le dossier d'un collègue restait réclamable par `POST /history` (N-1) ; corrigé depuis.<br>**Fait** |
| L-4 | Logan · `analyze.py:68-117`, `history.py:63-120` | [Haute] Cache d'analyse alimenté par `POST /history` (corps de requête) : un texte planté est servi aux autres, avec l'id et le chemin du premier utilisateur. | Écrire le cache côté serveur en fin de flux réussi (table à part) ; ne jamais renvoyer l'identifiant d'un autre. | D'accord. Table `analysis_cache` écrite par le serveur seul ; plus d'identifiant d'un autre utilisateur dans le résultat en cache. Logan v3 : corrigé.<br>**Fait** |
| L-5 | Logan · `sessions.py:283`, `ws.py:80`, `persistence.py:15-26` | [Haute] Le lien de partage donne l'id réel de session ; le WebSocket accepte n'importe quel `session_id` et l'`upsert` ne vérifie pas le propriétaire : on écrit dans la conversation d'un autre. | `DO UPDATE … WHERE user_id = EXCLUDED.user_id`, compter les lignes affectées, ne plus renvoyer l'id. | D'accord. Vue partagée avec le jeton comme id, `upsert` conditionné au propriétaire, `session_not_owned` enregistré. Logan v3 : corrigé.<br>**Fait** |
| L-13 | Logan · `sessions.py:43-83,141,264-270`, `feedback.py:23-112` | [Moyenne] Feedback non lié au votant (ids SERIAL énumérables) ; la vue partagée montre les commentaires privés ; `get_session` ne filtre pas `deleted`. | — | Vote accepté seulement sur un message de l'utilisateur ; vue partagée = vote du propriétaire seul ; sessions supprimées exclues. Logan v3 : corrigé.<br>**Fait** |
| L-14 | Logan · `ws.py:63-67,79-81` | [Moyenne] Contrôle d'accès du WebSocket fail-open, pas de contrôle d'Origin, entrée non validée (rôle `system`, taille illimitée, message sans contenu). | — | Échec = refus ; rôles et taille validés (`_clean_messages`) ; contrôle d'origine configuré pour DEV et UAT (`CHAT_ALLOWED_ORIGINS` de `target_env.json`, adresses exactes des apps). Reste : PROD, à renseigner quand l'app PROD existe (le profil `PROD` de Jules répond aujourd'hui avec l'app DEV, à vérifier). Logan v3 : voir N-5 (reste : origine PROD).<br>**Partiel** |
| L-15 | Logan · `credentials.py:9-30`, `doc_catalog.py:246-314` | [Moyenne] Tout utilisateur du chat atteint tout document indexé (compte de service) ; les puces du catalogue ignorent `in_chat` et la division. | Documenter comme contrôle accepté. | Voulu par Jules : le contrôle d'accès par l'index est accepté et écrit (`server/services/infra/README.md`) ; les liens REF du catalogue couvrent tout le périmètre de parsing exprès. Logan v3 : le trouve encore non documenté ; N-5 élargit le public concerné (contrôle accepté et écrit).<br>**Fait** |
| L-b1 | Logan · exports, historique, config, sessions, volume, feedback, impact, aperçu | [Basse] `str(exc)` renvoyé au navigateur (chemins de volume, erreurs SQL). | Réponse générique comme le gestionnaire global. | Réponse générique `INTERNAL_ERROR` sur ces routes ; le détail va dans les logs.<br>**Fait** |
| L-b2 | Logan · client (Markdown) | [Basse] Exfiltration par injection de prompt : images Markdown vers un hôte quelconque, pas de CSP, liens non limités à https. | N'autoriser que des hôtes connus. | D'accord, à faire côté client (reconstruire `client/out`). (Liens https seulement, images de l'origine ou `data:` seulement, en-tête CSP + nosniff + referrer-policy (`server/app.py`).)<br>**Fait** |
| L-b3 | Logan · `export_helpers.py:243` | [Basse] Injection de formule à l'export Excel (texte commençant par `=`). | — | Texte stocké comme texte (`_store_text_as_text`).<br>**Fait** |
| L-b4 | Logan · `/api/history` | [Basse] Coûts falsifiables : `cost_eur` et jetons envoyés par le client. | — | D'accord ; à calculer côté serveur. (`/api/history` ignore coût et jetons du client (relus depuis `llm_requests` du demandeur).)<br>**Fait** |
| L-b10 | Logan · `_caps_cache` | [Basse] Cache des droits sans éviction ; pas de limitation de débit par utilisateur sur les routes LLM. | — | D'accord, faible priorité. (Cache des droits plafonné à 5000 entrées ; limite de 30 requêtes/minute/utilisateur (`RATE_LIMIT_PER_MINUTE`, 0 = coupée) sur analyse, résumé, impact.)<br>**Fait** |
| L-b11 | Logan · middleware identité | [Basse] Historique anonyme partagé (`user_id = ''`) sans en-tête d'identité. | — | Non atteignable derrière le proxy ; à refuser quand même si l'en-tête manque. (Sans identité, les routes d'historique, de sessions et de feedback répondent 401 (sauf en dev).)<br>**Fait** |
| L-b17 | Logan · tests | [Basse] Chemins de sécurité sans test (propriété `load`/`save-*`, réutilisation de `session_id`, aperçu, tout le client). | — | Tests ajoutés pour la propriété des sessions, la réutilisation de `session_id`, l'aperçu protégé et la reconnexion Lakebase (`test_chat.py`, `test_compare_audit.py`, `test_lakebase_connection.py`). Reste : le code client n'a aucun test. (Aussi testés : en-têtes de sécurité, limite de débit.)<br>**Partiel** |
| N-1 | Logan v3 · `history.py:56,150`, `compare/common.py:80-105` | [Haute] `POST /history` enregistre `volume_session_path` tel que le client l'envoie : on nomme le dossier ancien d'un collègue, puis `load` et `save-*` le lisent et l'écrasent. | Accepter le chemin seulement sous la racine de l'utilisateur. | Chemin enregistré seulement sous la racine de l'appelant, ou si une ligne de l'appelant nomme déjà ce chemin exact (`..` refusé) ; sinon NULL et avertissement.<br>**Fait** |
| N-5 | Logan v3 · `user.py:126-129,275-289`, `ws.py:74-78,95` | [Haute] Accès au chat passé de « refus par défaut » à « ouvert » (`_DEFAULT_CAPS`) ; WebSocket ouvert si la recherche des droits échoue, sans identité, et sans contrôle d'origine en PROD. | Décider si le chat est ouvert à tous et l'écrire avec L-15 ; WebSocket fermé par défaut ; origine PROD ; refuser l'anonyme. | WebSocket refusé si la recherche des droits échoue, appelant sans identité refusé hors dev, origine vide = tout refusé hors dev ; reste : origine PROD et confirmation du chat ouvert à tous (voir Détails).<br>**Partiel** |
| N-7 | Logan v3 · limiteur, `ws.py`, routes d'aperçu | [Moyenne] La limite de débit ne couvre pas les routes coûteuses : WebSocket du chat et les deux routes d'aperçu. | — | La limite couvre aussi le WebSocket du chat et les deux routes d'aperçu.<br>**Fait** |
| N-b7 | Logan v3 · `server/app.py` | [Basse] En-têtes de sécurité absents des réponses 500 ; `frame-ancestors 'self'` bloque toute intégration dans Teams. | — | Le gestionnaire d'erreur pose les en-têtes de sécurité.<br>**Fait** |

**Détails**

- **N-5** — Logan v3 : WebSocket refusé si la recherche des droits échoue ; appelant sans identité refusé hors dev ; `CHAT_ALLOWED_ORIGINS` vide refuse toute origine hors dev. Reste : origine PROD à renseigner quand l'app PROD existe (PROD refuse toute origine d'ici là) ; chat ouvert à tous = choix documenté du projet, à confirmer avec L-15 comme contrôle accepté.

### B. Pipeline de parsing

| # | Origine · Fichier | Critique | Avis et proposition de Logan | Ma position · Statut |
|---|---|---|---|---|
| L-3 | Logan · `build_steps.py:128-131,355-358` | [Critique] En mode `full`, le checkpoint n'est pas filtré sur le manifeste : révisions remplacées remises dans `chunks` et dans l'index (UAT tourne en `full`). | Filtrer sur le manifeste dans tous les modes ; supprimer les lignes de checkpoint hors manifeste. | Filtre sur le manifeste dans tous les modes ; les résultats de parsing (`parsing_checkpoint`) des documents hors périmètre et parsés il y a plus de 180 jours sont supprimés (les plus récents restent : un document qui revient n'est pas reparsé). Logan v3 : filtre confirmé ; reste le cas des révisions remplacées en mode complet (N-9), corrigé.<br>**Fait** |
| L-7 | Logan · `manifest_steps.py:107-144`, `output_steps.py:242-266` | [Haute] Une révision remplacée perd ses chunks mais garde `SUCCESS` : `MISSING_CHUNKS`, re-parsing et nouvelle description d'images (payante), un jour sur deux. | Statut terminal pour les révisions élaguées. | Statut `SUPERSEDED_REVISION` posé à l'élagage. Logan v3 : voir N-9 (corrigé).<br>**Fait** |
| L-21 | Logan · `describe_steps.py`, `update_proj_vector_indexes.py:323-335`, `parsing_pipeline.job.yml:62` … | [Moyenne] Robustesse et coût : `redescribe_all_images` jamais appliqué aux passages, prune qui échoue en silence, attente d'index qui passe au premier sondage, pas de délai maximal … | — | Fait : nouvel essai du prune, attente d'index, délais, type de média ; descriptions réutilisées (même image et même requête : table `image_description_cache`, clé SHA-256, ignorée si `parsing_redescribe_all_images`) ; passages reconstruits dans ce cas. Reste : planning PROD en pause (Jules : « b, on verra plus tard » ; liste de contrôle PROD prête). Logan v3 : voir Détails.<br>**Partiel** |
| L-22 | Logan · `manifest_steps.py:402-423` | [Moyenne] `dry_run=false` depuis DEV ou UAT supprimerait des dossiers de la zone PROD partagée. | Suppression seulement quand l'environnement d'écriture est `prod`. | Fait ainsi. Logan v3 : corrigé.<br>**Fait** |
| L-b12 | Logan · `document_change_log`, `_pipeline_checkpoint_<ts>` | [Basse] Doublons dans `document_change_log` sur runs réparés ; clones de checkpoint jamais supprimés. | — | Un run réparé n'ajoute plus de lignes en double au journal des changements ; les clones `_pipeline_checkpoint_*` de plus de 30 jours sont supprimés (le plus récent reste).<br>**Fait** |
| N-2 | Logan v3 · `parse_steps.py:269-279` | [Haute] Un petit fichier dont le parseur a planté (ou un PDF scanné en attente d'OCR) devient `SKIPPED_EMPTY_SMALL_FILE`, statut terminal : le document quitte le chat sans bruit. | Tester `parser_error` d'abord ; exclure `pending_llm_ocr` du saut des petits fichiers. | Le statut teste `parser_error` en premier ; un résultat de stratégie `pending_llm_ocr` n'est jamais le saut des petits fichiers (`decisions.py`).<br>**Fait** |
| N-3 | Logan v3 · `parse_steps.py:342-356`, `build_steps.py:48-81` | [Haute] Un nouveau parse en échec ramène l'ancien texte `SUCCESS`, estampillé de la révision courante : le chat cite l'ancien contenu. | Dédoublonner sur la dernière ligne du sha sélectionné ; préférer SUCCESS dans le même sha seulement. | Variante plus étroite : la déduplication classe par sha égal à celui de la dernière tentative, puis SUCCESS, puis horodatage ; le sha exact sélectionné n'est pas connu de la tâche de chunking.<br>**Fait** |
| N-4 | Logan v3 · `manifest_steps.py:102-166`, `create_proj_parse_manifest.py` | [Haute] Une lecture de périmètre partielle ou vide supprime chunks, métadonnées d'images et documents parsés avant tout contrôle. | Arrêter la tâche si le périmètre baisse trop par rapport au manifeste précédent. | Garde `PARSING_MAX_SCOPE_DROP_PCT` (30 par défaut, 0 = coupée, seulement si le manifeste précédent a au moins 100 documents) : la tâche s'arrête avant toute suppression.<br>**Fait** |
| N-8 | Logan v3 · `output_steps.py:210-213` | [Moyenne] Les modes `full` et `rechunk` écrasent `parsed_documents` avec les seules lignes des cibles : les lignes SKIPPED disparaissent et le tableau de bord est faux. | Fusionner au lieu d'écraser. | Choix de Jules. (a) fusion sur IDDOC : les lignes SUCCESS hors périmètre ne sont plus nettoyées par effet de bord, les lignes des non-cibles survivent, évolution de schéma. (b) garder l'écrasement et rajouter les lignes SKIPPED existantes des IDDOC absents du nouveau tableau : sémantique actuelle gardée, tableau de bord corrigé. Recommandation : b.<br>**Décision de Jules** |
| N-9 | Logan v3 · `parsing_pipeline.job.yml:409-411`, `build_steps.py` | [Moyenne] En mode full, une révision remplacée encore dans le manifeste est reconstruite en SUCCESS avec chunks, puis élaguée le lendemain. | Exclure ce statut des cibles dans tous les modes. | Les documents `SKIPPED_SUPERSEDED_REVISION` sont exclus des cibles dans tous les modes (le manifeste les marque de nouveau ; N-8 décide si un run complet garde ces lignes).<br>**Fait** |
| N-10 | Logan v3 · `manifest_steps.py:44-51` | [Moyenne] `SKIPPED_EMPTY_FOLDER` est effacé à chaque run : nouveau scan chaque jour et faux NEW dans `document_change_log`. | — | Choix de Jules. A : effacer seulement si le dossier a changé (nouvelle colonne, date de dossier fiable). B : garder la ligne et lister le dossier à chaque run pour l'effacer. C : garder l'effacement quotidien et ne plus journaliser NEW pour un document déjà SKIPPED_EMPTY_FOLDER et de nouveau vide. Recommandation : C maintenant, A plus tard.<br>**Décision de Jules** |
| N-b5 | Logan v3 · parsing (essai de rang 2, métadonnées d'images) | [Basse] Chaque run complet reparse un fichier en échec et relance l'essai de rang 2 ; la fusion des métadonnées d'images ne supprime jamais les images disparues. | — | À voir à l'exécution, avec L-21.<br>**Ouvert** |
| N-b6 | Logan v3 · tâche GPU du parsing | [Basse] opencv 4.12 épinglé à côté d'une bibliothèque `numpy<2` : échec possible au démarrage (plausible). | — | À vérifier à l'exécution sur le cluster GPU.<br>**Ouvert** |

**Détails**

- **L-21** — Fichiers (suite) : `llm_describe.py:293` ; Critique (suite) : descriptions d'images non mises en cache, PNG étiqueté jpeg, PROD
  en pause.
- **L-21** — Logan v3 : reste : cluster GPU sans politique, planning PROD en pause, la fusion d'une nouvelle description ne supprime jamais les anciens passages d'images.

### C. Fiabilité et performance du serveur

| # | Origine · Fichier | Critique | Avis et proposition de Logan | Ma position · Statut |
|---|---|---|---|---|
| L-10 | Logan · `connection.py:197-223` | [Haute] Lakebase en panne au démarrage : aucune reconnexion et `/health` reste vert. | Boucle de reconnexion avec attente croissante ; état Lakebase dans `/health`. | Fait ainsi (`tests/test_lakebase_connection.py`). Logan v3 : corrigé.<br>**Fait** |
| L-11 | Logan · `preview.py:26-86`, `credentials.py:15-25`, `doc_catalog.py:142-157`, `analyze.py:233,407` | [Moyenne] Boucle d'événements bloquée (aperçu synchrone, `Config()` à chaque tour, grosse regex des REF), sémaphore compare libéré à la déconnexion, un `httpx.AsyncClient` par appel, cache PDF jamais nettoyé. | `asyncio.to_thread`, un client SDK partagé, un sémaphore LibreOffice, clients HTTP poolés. | Aperçu via `asyncio.to_thread`, configuration SDK en cache, cache PDF borné à 200 fichiers, recherche des références en thread, sémaphore libéré à la fin du travail réel, clients HTTP partagés fermés à l'arrêt. Logan v3 : voir Détails.<br>**Fait** |
| L-27 | Logan · `persistence.py:83-91`, `users.py:40`, `schema.py:571-578` | [Moyenne] Un `upsert` d'utilisateur en échec perd la sauvegarde du chat ; deux messages d'un tour hors transaction ; index manquants ; pas de verrou de migration. | — | `upsert` non fatal (`user_upsert_failed`) ; tour écrit en une transaction ; index sur `compare_messages(user_id, created_at)`, `chat_sessions(user_id, updated_at)`, `chat_feedbacks(message_id)` ; verrou consultatif ; golden de schéma régénéré. Logan v3 : corrigé (les quatre points).<br>**Fait** |
| L-b8 | Logan · `store_error`, votes, uploads | [Basse] `create_task` sans référence : tâches perdables. | Garder une référence. | `spawn()` garde une référence aux tâches ; `store_error` reste synchrone (règle du projet).<br>**Fait** |
| L-b9 | Logan · rafraîchissement du jeton Lakebase | [Basse] `await old_pool.close()` peut être bloqué par une requête bloquée. | — | D'accord (délai maximal à poser). (Fermeture du pool bornée à 30 s puis `terminate()`.)<br>**Fait** |
| Y-50 <sub>885860006</sub> | Yann · `llm.py:236` | « C'est normal d'ignorer ? » | **Pas jugé** | Ce n'est pas ignoré : échec journalisé, modèle mis en pause, suivant essayé, `CompareLlmUnavailable` levée si aucun ne répond. Commentaire ajouté. Attend Yann : réponse et commentaire, pas de changement de logique.<br>**En attente de Yann** |
| N-12 | Logan v3 · `lakebase_sync.yml:63-74` | [Moyenne] `sync_user_capabilities` tourne dans le job d'export deux fois par jour : jusqu'à 12 h plus le cache pour appliquer le retrait d'un groupe (job DEV en pause). | Un job à part, plus fréquent. | Choix de Jules : un job à part avec un planning plus fréquent (par exemple toutes les heures) ; fréquence à choisir.<br>**Décision de Jules** |
| N-b9 | Logan v3 · sessions, feedback de la comparaison | [Basse] Suppression et duplication de session hors transaction ; commentaire de feedback sans limite de taille. | — | Suppression et duplication dans une transaction ; commentaire plafonné à 2000 caractères (422 au-delà).<br>**Fait** |

**Détails**

- **L-11** — Logan v3 : le rafraîchissement des identifiants tourne maintenant hors de la boucle d'événements (chat, résumé, impact, analyse).

### D. Comparaison de documents (diff, Excel, conversions)

| # | Origine · Fichier | Critique | Avis et proposition de Logan | Ma position · Statut |
|---|---|---|---|---|
| L-6 | Logan · `excel.py:99-105,155-166`, `text.py:14,180`, `xml.py:25-56` | [Haute] Le diff perd de vrais changements : colonnes Excel « volatiles », feuilles et colonnes ajoutées, en-têtes, signes perdus (−40 → 40), XML malformé = « aucun changement », formules non lues. | Exclure une colonne seulement avec ≥ 20 lignes et la lister ; feuilles nouvelles ; capturer `[-+−±]?` ; erreur visible sur XML. | Formules lues : chaque cellule s'affiche `<valeur en cache> {=formule}` (`excel.py`), une formule modifiée à valeur égale est rapportée ; test ajouté. Colonne exclue seulement avec ≥ 20 lignes et listée, feuilles et colonnes nouvelles, en-têtes, signes, XML malformé = erreur visible. Logan v3 : voir Détails.<br>**Partiel** |
| L-16 | Logan · `impact_queries.py:81-132,165-172`, `impact.py:179-181,226`, `vector_search.py:456,473-482` | [Moyenne] Recherche d'impact : sections coupées à 2000 caractères, runs partiels mis en cache, citation inventée = quand même « impacté », auto-exclusion par REF seulement. | Garde `queries_failed > 0` sur le cache (voir Y-23). | Décidé par Jules (R4 a) : on accepte. L'ancienne version peut ressortir en tête si le nom du fichier ne contient pas sa REF ; elle se reconnaît à sa référence. Sections longues découpées, citation inventée classée « à vérifier ». Logan v3 : voir N-13 (fait) ; auto-exclusion acceptée (R4).<br>**Fait** |
| L-17 | Logan · `chunked_analysis.py:29-53,274-277` | [Moyenne] Gros documents découpés : lignes « aucun changement » entre les vrais changements, titre collé en fin de ligne, titres de section perdus, Excel jamais découpé (600 000 caractères en un appel). | — | Lignes « aucun changement » retirées à la fusion, titres séparés, titre repris dans les parties suivantes, diffs Excel découpés. Logan v3 : corrigé pour le Markdown ; les parties Excel suivantes perdent encore le contexte de feuille (basse).<br>**Fait** |
| L-18 | Logan · `analyze.py:70-100`, `summarize.py:57`, `impact.py:45-56` | [Moyenne] Clés de cache sans prompt, modèle ni réglages. | Faire comme `_impact_cache_version`. | `_analysis_cache_version` et `_summary_cache_version` ajoutées. Logan v3 : voir N-14 (fait).<br>**Fait** |
| L-23 | Logan · `llm.py:186-190`, `streaming.py:37` | [Moyenne] Température et « thinking » des modèles Claude morts ou invalides (400 silencieux, repli sur Luna). | — | Pas de température quand le raisonnement est actif ; test corrigé. Logan v3 : documenté : la température n'est pas envoyée aux endpoints Sonnet 5.5 et GPT-6.<br>**Fait** |
| L-29 | Logan · `conversion.py:41-50,93-99,161,175-236` | [Moyenne] Conversions Office (soulevées par Yann) : aperçu PPTX = diapositive 1, `.doc` coupé à 400 fragments, encodage antiword, course sur la détection LibreOffice. | PDF d'abord puis une image par page ; `-m UTF-8.txt`. | Voir Y-44 à Y-48. Logan v3 : le repli `.doc` avertit maintenant quand il ne garde que 400 fragments.<br>**Fait** |
| L-b5 | Logan · `.ppt`, images | [Basse] `.ppt` annoncé mais illisible par python-pptx ; grandes images non redimensionnées. | — | D'accord. (`.ppt` converti via LibreOffice, sinon message clair ; images réduites à 2000 px.)<br>**Fait** |
| L-b6 | Logan · prompt d'images | [Basse] Images au-delà de 95 supprimées en silence, étiquettes restées dans le prompt. | — | D'accord. (Étiquettes des images au-delà de 95 retirées avec un avertissement visible.)<br>**Fait** |
| L-b7 | Logan · extraction DOCX/PPTX | [Basse] Trous d'extraction : en-têtes et pieds DOCX, notes PPTX ; ligne courte ajoutée identique à une voisine masquée. | — | D'accord. (En-têtes/pieds DOCX et notes PPTX extraits ; ligne répétée = en-tête seulement à partir de 3 pages.)<br>**Fait** |
| Y-44 <sub>885835700</sub> | Yann · `conversion.py:44` | « Danger si concurrence non ? » | **D'accord** : le drapeau « vérifié » est posé avant la fin de la recherche (voir L-29). | Vrai. `_libreoffice_cmd` supprimée : conversion par `soffice.py` (`convert_docx_to_pdf`), recherche du binaire une fois sous verrou, conversions simultanées limitées ; vérification d'antiword aussi sous verrou.<br>**Fait** |
| Y-45 <sub>885835950</sub> | Yann · `conversion.py:94` | « Encodage manquant non ? » | **D'accord** : charset dépendant de la locale, toujours décodé en UTF-8.<br>Proposition : `-m UTF-8.txt` | Vrai : accents remplacés par `�`. `-m UTF-8.txt` ajouté ; sans ce fichier, on relance sans lui et on lit en Windows-1252. Deux tests.<br>**Fait** |
| Y-46 <sub>885834566</sub> | Yann · `conversion.py:162` | « Pq 400 ? » | **D'accord** : bug réel, `.doc` long coupé en silence, aussi en comparaison sans antiword. | Plafond nommé `_OLEFILE_MAX_RUNS` et avertissement quand il coupe. Valeur inchangée, aucune mesure ne dit mieux.<br>**Fait** |
| Y-47 <sub>885833326</sub> | Yann · `conversion.py:178` | « Ça a été testé cette fonction ? » | **D'accord** : aucun test, et fonction cassée (diapositive 1 seule). | Non, pas avant. `tests/test_conversion.py` (10 tests : docx, antiword UTF-8 et repli, borne olefile, fichier non OLE, diapositives avec un vrai LibreOffice).<br>**Fait** |
| Y-48 <sub>885833609</sub> | Yann · `conversion.py:200` | « Tu es sûr que ça convertit toutes les pages ? » | **D'accord** : LibreOffice n'écrit que la première (voir L-29).<br>Proposition : PDF puis une image par page | Faux, corrigé : PDF puis une image PNG par page, 100 diapositives au plus (avertissement au-delà). Test avec un diaporama de 3 : 3 images.<br>**Fait** |
| Y-53 <sub>886268712</sub> | Yann · `factory.py:27` | « Pas de .PDF ? » | **Pas jugé** | Déjà géré : `get_extension` met en minuscules. Test ajouté (`test_excel_processor.py`). Attend Yann : déjà géré, réponse seule (plus un test).<br>**En attente de Yann** |
| Y-54 <sub>886269539</sub> | Yann · `factory.py:34` | « xlmx en + ? » | **Pas jugé** | `.xlsm` ajouté côté serveur (`EXTENSION_MAP`, aperçu) et client (`files.ts`) ; openpyxl le lit comme un xlsx (test). Reconstruire `client/out` pour que le navigateur l'accepte.<br>**Fait** |
| Y-60 <sub>885862314</sub> | Yann · `relocation.py:58` | « Double boucle. Pas moyen de mieux faire ? » | **Pas jugé** | Non pour l'algorithme (table de programmation dynamique, le glouton se trompe dès qu'un bloc est inséré tôt ; coût borné par `_DUP_ALIGN_MAX_CELLS`). Mieux pour la lecture : `_drift_cost_table` avec docstring. … Attend Yann : algorithme gardé, seulement documenté.<br>**En attente de Yann** |
| Y-61 <sub>885865732</sub> | Yann · `excel.py:64` | « Split » (`_build_diff_keyed`, 170 lignes) | **Pas jugé** | Coupée en `_parse_sheets`, `_parse_rows`, `_is_auto_increment`, `_column_name`, `_volatile_columns`, `_row_fields`, `_modified_rows`, `_sheet_diff`. Identique sur 400 tirages, sauf un `IndexError` corrigé (feuille sans ligne). Tests ajoutés.<br>**Fait** |
| N-6 | Logan v3 · `xml.py:36-55` | [Haute] Les attributs XML ne sont jamais comparés (`<limit min="-40" max="85"/>` : « aucun changement »). | Une ligne `tag@attr=valeur` par attribut, ordre stable. | Une ligne `tag@attr=valeur` par attribut, triée.<br>**Fait** |
| N-13 | Logan v3 · `impact_queries.py:231-257` | [Moyenne] Passé `max_queries`, les groupes fusionnés sont coupés : un changement listé peut manquer du texte cherché ; le budget de 20 000 caractères vaut aussi pour un seul changement. | — | Groupes fusionnés découpés par taille ; ceux qui ne tiennent toujours pas sont listés (`left_out`, événement `changes_not_searched`, avertissement `app_errors`) ; requête d'un changement plafonnée à 2000 caractères (texte complet gardé). Question : afficher un avis dans l'interface (le client n'affiche pas `changes_not_searched`).<br>**Fait** |
| N-14 | Logan v3 · `analyze.py:85-99` | [Moyenne] Clé de cache d'analyse sans le prompt de la méthode utilisée, clé d'impact sans le prompt du juge ; réponse d'un modèle de repli mise en cache comme normale. | Hacher le prompt réellement envoyé ; ne pas cacher le repli. | Clé d'analyse = hachage du prompt réellement envoyé pour la méthode ; clé d'impact avec le prompt du juge ; réponses d'un endpoint de repli non mises en cache.<br>**Fait** |
| N-16 | Logan v3 · `excel.py:183-201` | [Moyenne] Colonnes Excel comparées par position : une colonne insérée exclut toutes celles de droite sur les feuilles de 20 lignes ou plus. | Aligner par nom d'en-tête quand ils diffèrent. | Quand les en-têtes diffèrent, colonnes appariées par nom d'en-tête (colonnes ajoutées et retirées listées) ; comparaison par position gardée si identiques.<br>**Fait** |
| N-b8 | Logan v3 · `soffice.py` | [Basse] Une découverte de LibreOffice en échec n'est pas mémorisée : chaque requête relance la sonde (jusqu'à 120 s). | — | Une découverte en échec est mémorisée 5 minutes.<br>**Fait** |

**Détails**

- **Y-60** — Position (suite) : Identique à l'ancien code sur 3000 cas.
- **L-6** — Logan v3 : presque corrigé (signes, formules, colonnes de fin, en-têtes, XML mal formé). Reste : une colonne modifiée dans plus de la moitié des lignes, sur les feuilles de 20 lignes ou plus, est encore écartée (son nom seul est listé) ; une feuille ajoutée n'est rapportée que par « sheet added (N rows) ». Demande une décision. Voir aussi N-6 et N-16 (faits).

### E. Chat (moteur, prompts, WebSocket)

| # | Origine · Fichier | Critique | Avis et proposition de Logan | Ma position · Statut |
|---|---|---|---|---|
| L-19 | Logan · `useChatConversation.ts:90`, `helpers.py:84-92`, `queries.py:25` | [Moyenne] Tours en échec renvoyés au modèle comme historique (ils comptent, et transforment une première question en « suivi » donc coupent la voie traduction seule) ; fenêtre de N−1 messages … | — | Un « Stop » ou un onglet fermé met fin au tour tout de suite, à n'importe quelle étape (écoute de la fermeture du socket, tâche annulée, tour enregistré `aborted`), y compris quand la traduction retient la réponse ; le serveur ne continue plus à produire ni à payer. Le client n'envoie plus les bulles d'erreur au modèle. Logan v3 : voir N-15 (fait).<br>**Fait** |
| Y-2 <sub>885059913</sub> | Yann · `README.md:84` | « Switch to 6 Luna » | **D'accord** : les cibles surchargent la réécriture (voir L-25).<br>Proposition : Choisir un modèle et aligner les docs. | Pont de traduction, jugement et résumé de la comparaison (texte et images) sur `databricks-gpt-6-luna` ; repli `databricks-gpt-5-6-luna` (chat : `app.yaml` ; comparaison : l'autre modèle, `compare_chain`). Réécriture du chat : voir point 4. …<br>**Fait** |
| Y-5 <sub>885070557</sub> | Yann · `language_reminder_named.md:1` | « Pas compris pourquoi deux » | **Désaccord** : deux cas distincts (langue détectée / non détectée), documenté. | D'accord avec Logan. Un seul fichier changerait la formulation du cas « inconnu », donc le comportement mesuré. Raison écrite dans `server/prompts/README.md`. Attend Yann : pas de changement, réponse non acceptée.<br>**En attente de Yann** |
| Y-6 <sub>885071187</sub> | Yann · `language_reminder_named.md:1` | « Plutôt un fichier de config .py » | **Désaccord** : les `.md` sont enregistrés dans le registre de prompts et éditables sans code. | Règle du projet : un prompt du chat est un fichier (le hash lie la version enregistrée à la trace). Ce qui varie va en config Python (voir Y-7). Attend Yann : pas de changement, réponse non acceptée.<br>**En attente de Yann** |
| Y-7 <sub>885138943</sub> | Yann · `as.md:1` | « Variabiliser pour réutiliser le même texte entre IS/AS » | **D'accord** : texte répété, un gabarit `{division}` ; garder en `.md`.<br>Proposition : Un gabarit + bloc d'ordre propre à AS. | `as.md` et `is.md` remplacés par `system/division.md` (gabarit rempli par `division_prompt()` et `DIVISION_NAMES` de `engine/config.py`) ; règle QP puis MI dans `as_reference_ordering.md`. …<br>**Fait** |
| Y-8 <sub>885138148</sub> | Yann · `base.md:2` | « Un petit langdetect en amont ? » | **En partie** : existe déjà (fastText) ; bloc réductible à une ligne de repli.<br>Proposition : Ramener le bloc « langue » à une ligne. | Bloc « langue » de `base.md` ramené à deux lignes (réponse dans la langue de la question sauf demande contraire ; citation dans la langue d'origine avec traduction). Détection fastText et rappel nommé déjà en place. …<br>**Fait** |
| Y-9 <sub>885145601</sub> | Yann · `helpers.py:13` | « Réfléchir en tokens plutôt que conversation turn ? » | **D'accord** : dix messages peuvent être énormes ; bug réel de fenêtre N−1 (voir L-19). | `CHAT_MAX_HISTORY_TOKENS` (8000, `app.yaml`) remplace `CHAT_MAX_HISTORY` : messages récents qui tiennent, dernière question toujours gardée, jamais d'ouverture sur un message assistant, 4 caractères par jeton (pas de tokenizer).<br>**Fait** |
| Y-39 <sub>885271954</sub> | Yann · `translation_bridge.py:36` | « Model hardcodé ! » | **En partie** : lu dans `CHAT_TRANSLATE_ENDPOINT`, le défaut duplique `app.yaml` (comme Y-22). | Défauts regroupés dans `services/chat/config.py` (modèle GPT-6 Luna, voir Y-2), fixés aussi par `app.yaml`.<br>**Fait** |
| Y-43 <sub>885793573</sub> | Yann · `search.py:54` | « Danger ! Aucun avertissement qu'on coupe la query » | **En partie** : silencieux mais impact faible (plafond sous la limite, seule la question brute l'atteint).<br>Proposition : Journaliser un incident à la coupe. | Vrai : > 20 000 caractères coupés sans trace. Avertissement dans les logs, incident `query_truncated` (colonne `warnings`, `app_errors`), longueur d'origine dans `vs_calls[].truncated_from`, code ajouté à `schema.md`. Deux tests.<br>**Fait** |
| N-15 | Logan v3 · `useChatConversation.ts:92-95`, `history.ts` | [Moyenne] Le client retire la réponse en échec mais garde sa question : le serveur voit deux questions de suite et garde l'orpheline ; les réponses coupées par Stop sont envoyées. | Filtrer par paires côté client. | Le client construit l'historique par paires (`history.ts` : une question part avec la réponse qui a échoué, été arrêtée ou est vide) ; les réponses partielles arrêtées ne sont plus envoyées.<br>**Fait** |

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
| Y-23 <sub>885255107</sub> | Yann · `impact.py:72` | « Fonction à split ? t'en penses quoi ? » | **D'accord** : 160 lignes, helpers testables ; ajouter la garde de cache (voir L-16).<br>Proposition : Helpers au niveau module. | Oui, mais pas en morceaux : `_search`, `_audit`, `_warn`, `_judges` deviennent méthodes de `_ImpactSearch` ; `_impact_events` est un générateur de module ; `find_impacted_documents` ~50 lignes. Garde de cache faite (L-16).<br>**Fait** |
| Y-24 <sub>885254220</sub> | Yann · `common.py:114` | « Boolean complexe à détailler en variables » | **D'accord** : lisibilité seule, logique correcte. | `climbs_out`, `is_root`, `is_below_root`.<br>**Fait** |
| Y-25 <sub>885251206</sub> | Yann · `analyze.py:123` | « Casser la fonction trop longue + ajouter les rtype » | **D'accord** : ~92 lignes avec fermetures, peu de types de retour. | 290 lignes → ~60 : `_AnalysisJob`, `_read_uploads`, `_upload_to_volume`, `_acquire_slot`, `_keepalive_until_done`, `_track_llm_event`, `_analysis_events` ; types de retour sur tout `server/routers/` …<br>**Fait** |
| Y-26 <sub>885259506</sub> | Yann · `exports.py:101` | « Optional[Type] ou Type \| None et applique partout » | **D'accord** : `T \| None`. | `Type \| None` (Python ≥ 3.11) partout dans `server/` (34 fichiers), imports `Optional` retirés ; annotations entre guillemets en `'X \| None'`.<br>**Fait** |
| Y-27 <sub>885261374</sub> | Yann · `feedback.py:18` | « Pq str alors ? Enforcer up or down » | **D'accord**.<br>Proposition : `Literal['up', 'down']` | `vote: Literal['up', 'down']` dans les deux modèles de `feedback.py` et dans celui de `chat/sessions.py` ; valeur invalide = 422.<br>**Fait** |
| Y-28 <sub>885261904</sub> | Yann · `feedback.py:26` | « T'aurais plus besoin de ces checks » | **D'accord** : Pydantic fait déjà le 422 ; le vrai problème est L-13. | Contrôles manuels supprimés ; propriété du message vérifiée (L-13).<br>**Fait** |
| Y-29 <sub>885262081</sub> | Yann · `feedback.py:62` | « Same! » | **D'accord** (comme Y-27). | Même suppression pour le second endpoint et pour le chat.<br>**Fait** |
| Y-30 <sub>885262563</sub> | Yann · `history.py:20` | « Config file » | **En partie** : objet réponse global, mieux une petite fonction qui renvoie une réponse neuve.<br>Proposition : `_unavailable()` | `APP_VERSION` s'importe de `compare/config.py` ; le 2026-10-21 `_UNAVAILABLE` devient la fonction `_unavailable()`.<br>**Fait** |
| Y-31 <sub>885266896</sub> | Yann · `history.py:47` | « Pas dict mais dict[Type, Type]… TypedDict c'est mieux » | **D'accord** : basse priorité. | `_serialize(row: Mapping[str, Any]) -> dict[str, JsonScalar]`. Pas de `TypedDict` : la fonction sert à `SELECT *` et à des listes de colonnes, clés non fixes. Attend Yann : dict typé à la place d'un `TypedDict`.<br>**En attente de Yann** |
| Y-32 <sub>885267451</sub> | Yann · `history.py:144` | « Class pour 1 var ? » | **Désaccord** : un modèle Pydantic est la façon normale de recevoir un corps JSON. | Écart assumé : `SaveImpactRequest` supprimée, la route lit `impact_json: str = Body(..., embed=True)`. JSON du navigateur inchangé (vérifié).<br>**Fait** |
| Y-33 <sub>885269686</sub> | Yann · `engine/config.py:67` | « En haut » | **D'accord** : style. | `_RAW_ON_ALL` et `_RAW_ON` remontent.<br>**Fait** |
| Y-34 <sub>885269518</sub> | Yann · `engine/config.py:32` | « config == config pas common » | **D'accord** : renommer `common.py` de la comparaison. | Compris comme « un module de réglages s'appelle `config` » : `engine/config.py` l'était, `common.py` de la comparaison est corrigé (Y-22). À confirmer si je me suis trompé. Attend Yann : remarque comprise autrement, à confirmer.<br>**En attente de Yann** |
| Y-35 <sub>885270078</sub> | Yann · `queries.py:49` | « Config » | **En partie** : liste de phrases-bruit, constante nommée en haut suffit. | `_COMPANY` et `_NOISE_PHRASES` passent dans `engine/config.py` (`NOISE_PHRASES`).<br>**Fait** |
| Y-36 <sub>885271146</sub> | Yann · `mlflow_turns.py:119` | « En haut + config » | **En partie** : en haut oui, une regex de parsing n'est pas de la config. | Import de `translation_bridge` en tête ; limites d'export `MLFLOW_*` dans `services/chat/config.py` ; `_fast_lang_guess` → `fast_lang_guess` (publique) ; tests et notebooks mis à jour.<br>**Fait** |
| Y-37 <sub>885271431</sub> | Yann · `mlflow_turns.py:287` | « Pas cool à lire » | **D'accord** : conditionnel dans une f-string.<br>Proposition : Construire le libellé avant. | Span `rewrite` avec variables nommées ; libellé des appels Vector Search par `_vs_call_label()`.<br>**Fait** |
| Y-38 <sub>885272147</sub> | Yann · `translation_bridge.py:67` | « Config! » | **Désaccord** : regex de nettoyage, pas des réglages. | Écart assumé : motifs, seuils de détection et noms de langues déplacés dans `services/chat/config.py`.<br>**Fait** |
| Y-40 <sub>885272670</sub> | Yann · `turn_log.py:33` | « Config » | **Désaccord** : regex de parsing / vocabulaire. | D'accord avec Logan. `INFO`, `WARNING`, `ERROR` sont le vocabulaire du journal de tour (importé par `ws.py`, le moteur, `lakebase`), pas des réglages par cible ; en config, le journal dépendrait d'un module de réglages. … Attend Yann : pas de changement, réponse non acceptée.<br>**En attente de Yann** |
| Y-41 <sub>885789675</sub> | Yann · `retrieval.py:52` | « Pas super lisible mais bon à voir » | **D'accord** : `setdefault` dans une compréhension.<br>Proposition : Écrire en boucle. | `refs_named_in`, `named` dans `retrieve_for_turn` et `one_language_per_document` simplifiés (liste dédupliquée par `dict.fromkeys`, boucle explicite). Comportement identique, deux tests.<br>**Fait** |
| Y-42 <sub>885790057</sub> | Yann · `retrieval.py:57` | « Any c'est pas incroyable. Si possible des TypedDict » | **D'accord** : basse priorité. | Les filtres Vector Search, le corps des requêtes, le résultat de la récupération et les faits de l'appel de réponse ont maintenant leurs `TypedDict` (`engine/types.py`) ; ne restent en dict libre que les réglages enregistrés avec le tour et les puces de sources.<br>**Fait** |
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
| Y-1 <sub>885061633</sub> | Yann · `settings.json:1` | « Exclure le .vscode non ? » | **En partie** : `settings.json` = réglages personnels ; `tasks.json` sert à l'équipe.<br>Proposition : Exclure `settings.json`, garder `tasks.json`. | `settings.json` sort de git et entre dans `.gitignore` ; `tasks.json` reste volontairement (il porte la tâche de déploiement). Attend Yann : `tasks.json` gardé alors qu'il demandait d'exclure le dossier.<br>**En attente de Yann** |
| Y-3 <sub>885062343</sub> | Yann · `index.html:1` | « Pourquoi en double avec client/index.html » | **En partie** : pas un doublon (source Vite / build) ; le vrai problème est de versionner le build.<br>Proposition : Construire au déploiement, ignorer `out/`. | Pas un doublon : `client/index.html` est la source, `out/` le build (balises plus scripts au nom haché). …<br>**Fait** |
| Y-52 <sub>885869279</sub> | Yann · `8859-1.txt:69` | « C'est quoi ces trucs ? À garder dans la PR ? » | **Pas jugé** | Garder : tables de caractères et exécutable d'antiword, lus par le job de parsing (`ANTIWORDHOME`) que le cluster ne peut pas installer. L'app installe le `.deb` (`start.sh`). … Attend Yann : fichiers gardés, réponse non acceptée.<br>**En attente de Yann** |
| L-9 | Logan · `bitbucket-pipelines.yml:312-354`, `bitbucket_deploy.sh:49-62`, `databricks.yml:499` | [Haute] PROD déployable depuis toute branche, aucun test en CI, CLI installée par script non épinglé (`main`), jeton longue durée. | Tests d'abord, UAT sur `main`, PROD manuel ; environnement de déploiement PROD limité à `main` ; épingler la CLI ; OAuth/OIDC. | Tests avant les déploiements UAT et PROD (DEV sans) ; CLI épinglée à 1.20.0 (`utils/deploy/databricks_cli_version.txt`). Reste : OAuth/OIDC à la place du jeton longue durée ; `Production` limité à `main` (Jules : quand tout est validé). … Logan v3 : voir Détails.<br>**Partiel** |
| L-20 | Logan · `alerts.yml:82-126`, `03_chat_failure_rate.sql`, `lakebase_sync.yml:124` … | [Moyenne] Alertes qui ne peuvent pas se déclencher (copie Delta rafraîchie 2 fois par jour, 22 runs sur 24 voient 0 ligne), pas de job d'export en PROD, tableaux DEV figés … | — | Alerte d'échec du chat à 03 h et 15 h (fenêtre 13 h) après la copie ; export Lakebase de PROD dé-pausé dans `databricks.yml` (tournera au déploiement de PROD, il faut d'abord les droits du principal de service de PROD). Reste : le tableau de bord DEV qui lit d'anciennes tables figées n'est pas corrigé (Jules : « je le ferai plus tard »). Logan v3 : voir Détails.<br>**Partiel** |
| L-25 | Logan · `target_env.json`, `app.yaml`, `databricks.yml`, `test_deploy_config.py:22`, READMEs | [Moyenne] Config et docs incohérentes : réécriture Gemini vs « Luna seulement », `CHAT_TRANSLATE_ONLY` documenté « off », fichiers cités absents, README CI faux, README du moteur vs `base.md` … | — | Réécriture tranchée (GPT-6 Luna) ; README CI et docs corrigés (sources, `CHAT_TRANSLATE_ONLY`, références absentes) ; tableau de sources de `base.md` gardé, `CLAUDE.md` corrigé ; repli « réécriture échouée » réel : le tour continue avec la question brute (statut dégradé) et demande autant de candidats qu'un tour normal (48) ; `prevent_destroy` sur le rôle Lakebase de PROD. Logan v3 : voir Détails.<br>**Fait** |
| L-26 | Logan · `leap_utils/` | [Moyenne] 12 272 lignes copiées, 78 % jamais atteint (le pipeline utilise 7 modules, 2 628 lignes), risque de dérive. | Dépendre d'un paquet épinglé ou ne copier que les modules utilisés. | Réduit aux 11 modules que le pipeline de parsing atteint (3 195 lignes gardées, 23 fichiers / 9 077 lignes supprimés), voir `leap_utils/README.md`. Un paquet épinglé reste possible plus tard. Logan v3 : corrigé.<br>**Fait** |
| L-28 | Logan · `requirements.txt`, `pyproject.toml`, `start.sh:31-45`, `bitbucket_deploy.sh:49`, `.mcp.json` | [Moyenne] Dépendances non reproductibles : `>=` ≠ `uv.lock`, bibliothèques de jobs non épinglées, `.deb` antiword en `http://` sans somme de contrôle, `@playwright/mcp@latest`. | — | Dépendances directes figées sur les versions de `uv.lock`, et `tests/test_requirements_pinned.py` échoue si une ligne de `requirements.txt` n'est pas une version exacte ou si elle s'écarte de `uv.lock`. Les dépendances indirectes sont résolues à l'installation de l'app (Databricks Apps préinstalle des paquets) ; `.deb` antiword en https avec SHA-256. Logan v3 : voir Détails.<br>**Partiel** |
| L-b13 | Logan · `client/out`, `.gitleaksignore` | [Basse] `client/out` versionné, jamais reconstruit par la CI, exclu du scan de secrets. | Construire au déploiement. | Fait : voir Y-3 (construit au déploiement, plus versionné).<br>**Fait** |
| L-b16 | Logan · `databricks.yml`, YAML des jobs | [Basse] Identités individuelles (dont un compte externe) nommées en dur. | Groupes et variables. | Adresse d'alerte des jobs DEV/UAT conservée sur `jules.gourio.external` (décision de Jules) ; `christophe.pelissier` et `yosif.karapendov` retirés des droits de l'app UAT (vérifié : tous deux ont can_chat par les groupes) ; compte de déploiement de Jules et système de droits Lakebase conservés.<br>**Fait** |
| N-b1 | Logan v3 · `runbooks/pr_review/comments/*` | [Basse] Vidages des commentaires de la PR commités (~46 Ko), avec le nom du relecteur et des liens internes. | Les retirer et ignorer le dossier. | Dossier ajouté à `.gitignore` ; les fichiers déjà commités demandent `git rm --cached` dans le dépôt de Jules.<br>**Partiel** |
| N-b2 | Logan v3 · `databricks.yml:500-508` | [Basse] Rôle Lakebase de PROD : identifiant du SP de l'app interpolé avec `prevent_destroy` ; une recréation forcée serait bloquée et ferait échouer le déploiement (plausible). | — | Mettre l'identifiant du SP de l'app en dur une fois l'app PROD créée ; inscrit dans la liste PROD.<br>**Ouvert** |
| N-b3 | Logan v3 · `lakebase_sync.yml`, `databricks.yml` | [Basse] Volume de transit non déclaré pour UAT et PROD : l'export le crée à l'exécution (droit CREATE VOLUME), hors du bundle. | — | Le déclarer pourrait entrer en conflit avec le volume UAT existant dans l'état du bundle ; à décider.<br>**Ouvert** |
| N-b4 | Logan v3 · `alerts.yml` | [Basse] L'alerte d'échec de job lit `system.lakeflow` sans filtre `workspace_id` : sur un metastore partagé, chaque environnement alerte sur tous (plausible). | — | Il faut les identifiants d'espace de travail par cible : DEV 2865348338307293, UAT 3155144190025826, PROD inconnu.<br>**Ouvert** |
| N-b10 | Logan v3 · `bitbucket-pipelines.yml` | [Basse] Deux messages d'erreur du pipeline encore en français. | — | Messages en anglais.<br>**Fait** |
| N-b11 | Logan v3 · `audit.py` `store_error` | [Basse] L'écriture d'erreur part dans une tâche sans référence forte. | — | La tâche passe par `spawn` (référence conservée jusqu'à la fin).<br>**Fait** |
| N-b12 | Logan v3 · `analyze.py` | [Basse] `str(e)` renvoyé à l'utilisateur. | — | Message générique ; la cause va dans `app_errors`.<br>**Fait** |
| N-b13 | Logan v3 · tableaux DOCX | [Basse] Les tableaux dans les en-têtes et pieds de page DOCX sont toujours ignorés. | Les lire. | Pas traité.<br>**Ouvert** |
| N-b14 | Logan v3 · droits de l'app | [Basse] Compte externe avec CAN_MANAGE. | Le retirer. | Jules a décidé de le garder.<br>**Fait** |

**Détails**

- **Y-3** — Position (suite) : `client/out` n'est plus versionné (`.gitignore`, retiré de git) : chaque déploiement le construit avec bun
  (`bitbucket_deploy.sh`, version 1.4.2 épinglée ; `deploy_qualibot.ps1` le faisait déjà) et les règles de sync (`include client/out/**`,
  `--include`) l'envoient.
- **Y-52** — Position (suite) : Seule `UTF-8.txt` est demandée par le code ; les autres peuvent partir si tu veux une PR plus courte.
- **L-15** — Position (suite) : un document absent de l'index du chat reste ouvrable dans le portail Intraqual, d'où les liens sur tout le périmètre.
- **L-9** — Position (suite) : OAuth/OIDC demande un administrateur de compte Databricks et une configuration Bitbucket (plus tard). Version de la CLI lue par `bitbucket_deploy.sh` dans `utils/deploy/databricks_cli_version.txt` (1.20.0, à
  vérifier au premier déploiement). Restreindre `Production` à `main` : Repository settings > Deployments, une fois tout validé.
- **L-20** — Fichiers (suite) : `import_lakebase_from_json.py:149-153` ; Critique (suite) : import UAT→PROD qui échoue sur table manquante.
- **L-25** — Critique (suite) : repli « rewrite failed » mort, volumes `uat_landingzone`, pas de `prevent_destroy`, test `uat-test`.
- **L-28** — Position (suite) : `.mcp.json` absent de ce dépôt. Pas de `uv.lock` dans ce dépôt : dépendances indirectes non verrouillées.
- **L-9** — Logan v3 : fait : CLI épinglée dans chaque étape Bitbucket via `utils/deploy/install_databricks_cli.sh`, installateur de Bun lancé depuis un fichier, étape de tests qui installe les dépendances épinglées en dernier, `deploy_qualibot.ps1 -AppEnv prod` refuse de tourner hors `main`, erreurs traduites. Reste : la vérification de branche est dans le yml que la branche peut modifier (restriction de l'environnement `Production` après validation), OIDC.
- **L-20** — Logan v3 : les six alertes sont en pause sur toutes les cibles ; celles d'UAT ont été dé-pausées depuis par un autre changement.
- **L-25** — Logan v3 : fait pour la documentation et `APP_VERSION` (tous les défauts à 1.4.1). Question : le prompt inline d'`app.yaml` ne s'applique que si la méthode est `comparative` ; toutes les cibles tournent en `standard`, donc le modèle reçoit `analysis_standard.md` / `analysis_structured.md` : le supprimer ou le garder ?
- **L-28** — Logan v3 : reste : bibliothèques de jobs non épinglées ; le `uv.lock` périmé est celui du dépôt de Jules.

### H. Client et interface

| # | Origine · Fichier | Critique | Avis et proposition de Logan | Ma position · Statut |
|---|---|---|---|---|
| L-24 | Logan · `useHistoryLoad.ts:46-51`, `useChatConversation.ts:34-56`, `useAnalysisTrack.ts:80`, `config.ts:68-70` … | [Moyenne] Courses (historique A puis B, « Nouveau chat », résumé du fichier précédent), `running` bloqué si le hachage échoue, pas de watchdog du chat, suppression sans erreur, `/api/me` en échec = accès complet. | — | Besoin de reconstruire `client/out`. (Compteurs de requêtes (historique, nouveau chat, résumés), `try/finally` du hachage, garde-fou de 120 s sur le chat, suppression avec erreur, `/api/me` en échec = écran avec « Réessayer ».) Logan v3 : corrigé (les sept points).<br>**Fait** |
| Y-4 <sub>885062844</sub> | Yann · `databricks-symbol-color.svg` | « Pareil logos en double ? » | **En partie** : même cause que Y-3 ; réduire le favicon de 1 Mo.<br>Proposition : Réduire `LOGO_Qualibot.png`. | Le svg n'existe plus dans le dépôt ; favicon de 1 Mo remplacé par des icônes 64 px et 180 px, logo 66 Ko ; `out/` : voir Y-3 (plus versionné).<br>**Fait** |
| L-b14 | Logan · `client/` | [Basse] Bundle JS de 1,14 Mo en un morceau, `pdfjs-dist` inutilisé, favicon de 1 Mo, polices Google chargées à l'exécution. | — | D'accord. (`pdfjs-dist` retiré, favicon 64 px et icône 180 px, logo 66 Ko, code vendor en morceaux ; polices Google retirées (polices système).) (Polices Montserrat, Open Sans et JetBrains Mono livrées avec le build du client (Fontsource), plus aucun appel à Google.) (Coloration du code chargée à la demande : première page ~510 Ko au lieu de 1,14 Mo.)<br>**Fait** |

**Détails**

- **L-24** — Fichiers (suite) : `useChatSessions.ts:27-31`

### I. Données, RGPD, conformité

| # | Origine · Fichier | Critique | Avis et proposition de Logan | Ma position · Statut |
|---|---|---|---|---|
| L-8 | Logan · `bitbucket-pipelines.yml:262-286`, `ops_config.py:39` | [Haute] Données de chat et d'utilisateurs hors plateforme : artefacts Bitbucket du pipeline d'export, jetons de partage copiés en Delta, DEV chargé depuis des instantanés UAT. | Passer par un volume UC ou un job base à base ; jeton créé au clic sur Partager ; exclure `share_token` des exports et les faire tourner ; décider si l'historique va en environnement bas. | Le pipeline `sync-prod-data` ne copie plus que le volume des modèles docling ; `export_lakebase_to_json.py`, `import_lakebase_from_json.py` et leurs constantes (`ops_config.py`) supprimés. … Logan v3 : voir Détails.<br>**Fait** |
| J-1 <sub>885301989</sub> | Jules · `export_lakebase_uat_to_volume_job.py:1` | « Les jobs là sont refaits… » (plus de `uat` dans les noms, un job par cible qui copie Lakebase{env} → UC{env}) | Confirmé sur `origin/DEV` (la partie export Bitbucket restait, voir L-8). | Déjà fait avant la revue (commit `97f8853` : `lakebase_export`, `export_lakebase_to_volume`, `import_lakebase_tables_to_delta`). Plus aucune référence `export_lakebase_uat*`.<br>**Fait** |
| L-12 | Logan · `analyze.py:288`, `schema.py:391-394`, `ws.py:90-92`, `storage.ts:63-74` | [Moyenne] Pas de rétention ni d'effacement par utilisateur : textes complets dans `llm_requests`, passages dans `chat_retrieved_chunks`, questions dans les logs et MLflow, suppressions « douces » … | — | `llm_requests.user_id` (colonne, index, rempli à chaque analyse). Stockage du navigateur (`localStorage`) effacé 7 jours après la dernière écriture (`clearExpiredStorage`) ; l'historique en base recharge toujours l'entrée (analyse, impact, fichiers). Job de purge REFUSÉ par Jules (« pas besoin de faire la suppression »). Logan v3 : voir Détails.<br>**Fait** |
| L-b15 | Logan · antiword, modèle fastText | [Basse] Notices de licence manquantes (antiword GPL-2, modèle fastText CC-BY-SA). | Ajouter les notices. | Décidé : antiword sous GPL-2.0 (le texte amont est la GPL version 2), modèle fastText sous CC-BY-SA 3.0 (version publiée par fasttext.cc), `fasttext-wheel` sous MIT ; inscrit dans `third_party/README.md`.<br>**Fait** |
| N-11 | Logan v3 · `import_lakebase_tables_to_delta_job.py:113-116` | [Moyenne] La copie Delta supprime et recrée chaque table deux fois par jour (droits perdus, trou bref) ; les vieux JSON du volume de transit ne sont jamais effacés, donc la liste d'exclusion n'est pas fiable. | `CREATE OR REPLACE` ou échange ; vider le dossier d'export avant. | `CREATE OR REPLACE TABLE … AS SELECT` (table, droits et historique conservés) ; après un export complet, les JSON qu'aucune table n'a écrits sont supprimés. Non testé sur Databricks.<br>**Fait** |

**Détails**

- **L-8** — Position (suite) : Jetons de partage inchangés ; chats DEV = vraies données DEV (accepté).
- **L-12** — Critique (suite) : `llm_requests` sans `user_id`, `localStorage`. ; Position (suite) : `clearExpiredStorage` est dans
  `storage.ts` ; reconstruire le client (fait au déploiement).
- **L-8** — Logan v3 : statuts inchangés ; l'export `SELECT *` copie `share_token` et le texte des chats dans `<env>_proj.qualibot` deux fois par jour, accepté par Jules (jetons inchangés).
- **L-12** — Logan v3 : toujours partiel de son côté (pas de purge, suppressions douces, 7 jours non liés à un utilisateur) ; décision de Jules inchangée.

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
- **Décision / état** : Décidé le 2026-10-21 : Lakebase jamais copié entre environnements (« il n'y a besoin d'aucun job du Lakebase UAT vers
  DEV : le job DEV copie le Lakebase DEV, le job UAT copie le Lakebase UAT, c'est tout ce qu'il faut ») ; jetons de partage inchangés (« chaque
  environnement garde ses share_tokens, cela aide à lire les chats plus facilement ») ; chats DEV = vraies données DEV (« ce n'est pas grave si
  on peut faire un select dessus »). **Fait**

### 2. Rétention et RGPD (L-12)

- **Le problème** : les textes complets des documents et des passages retrouvés restent en base sans limite de durée, et le navigateur en
  garde des copies.
- **Options et conséquences** : `user_id` ajouté à `llm_requests` (colonne, index, insertion d'audit), ce qui permet de retrouver les
  données d'une personne. Pas de job de purge. Le stockage du navigateur (`localStorage`) est effacé 7 jours après la dernière écriture
  (`clearExpiredStorage`) ; l'historique en base recharge l'entrée avec analyse, impact et fichiers du volume.
- **Avis de Logan** : fixer une durée de conservation pour `llm_requests.messages_json` et `chat_retrieved_chunks` ; traiter `localStorage`.
- **Décision / état** : Décidé le 2026-10-21 : `user_id` oui, pas de purge (« pas besoin de faire la suppression » ; « l'historique garde
  ceci dans Databricks »), `localStorage` effacé à 7 jours (client reconstruit au déploiement). **Fait**

### 3. `client/out` versionné ou construit au déploiement

- **Le problème** : `client/out` (le site compilé) était dans git : il pouvait dériver du code source, n'était pas reconstruit par la CI et
  échappait au scan de secrets.
- **Options et conséquences** : option B retenue, construire au déploiement avec bun 1.4.2 épinglé (`bitbucket_deploy.sh` ;
  `deploy_qualibot.ps1` le faisait déjà). `client/out` est ignoré et retiré de git ; les règles de synchronisation (`include client/out/**`,
  `--include`) ne changent pas.
- **Avis de Logan** : construire au déploiement et ignorer `out/`.
- **Décision / état** : Décidé le 2026-10-21 : construit au déploiement (option B). **Fait**

### 4. Modèle de réécriture du chat

- **Le problème** : les cibles remplaçaient le modèle de réécriture par un autre, alors que la documentation annonçait « Luna seulement ».
- **Options et conséquences** : GPT-6 Luna sur toutes les cibles ; surcharges Gemini retirées de `utils/deploy/target_env.json` et README
  corrigé.
- **Avis de Logan** : choisir un modèle et aligner la documentation.
- **Décision / état** : Décidé le 2026-10-21 : GPT-6 Luna sur toutes les cibles. **Fait**

### 5. Bloc « langue » de `base.md`

- **Le problème** : Yann demande une détection de langue en amont. Elle existe déjà (fastText détecte la langue et ajoute un rappel nommé),
  mais `base.md` gardait en plus un long bloc de règles de langue.
- **Options et conséquences** : bloc ramené à deux lignes (réponse dans la langue de la question sauf demande contraire ; citation dans la
  langue d'origine avec traduction). Risque : réponse dans la mauvaise langue sur des questions courtes ou mixtes.
- **Avis de Logan** : couper à une ligne de repli.
- **Décision / état** : Décidé le 2026-10-21 : bloc réduit à deux lignes ; mesure sur DEV à faire, puis `register_chat_prompts`. **Fait**

### 6. CI (intégration continue)

- **Le problème** : la CLI Databricks est installée sans version fixée, le jeton de déploiement est longue durée, et les tests ne bloquent
  pas les déploiements.
- **Options et conséquences** : tests avant les déploiements UAT et PROD (pas DEV), avec `requirements.txt` épinglé : 597 réussis. La
  version de la CLI (1.20.0) est lue dans `utils/deploy/databricks_cli_version.txt` par `bitbucket_deploy.sh`. OAuth/OIDC
  (authentification sans jeton longue durée) demande un administrateur de compte Databricks et une configuration Bitbucket.
- **Avis de Logan** : épingler la CLI, passer à OAuth/OIDC, lancer les tests avant les déploiements.
- **Décision / état** : Décidé le 2026-10-21 : CLI épinglée, tests bloquants pour UAT et PROD ; `Production` limité à `main` une fois tout
  validé (Repository settings > Deployments) ; OIDC plus tard. **Partiel** (voir L-9)

### 7. Dépendances et `leap_utils`

- **Le problème** : `requirements.txt` accepte n'importe quelle version plus récente (`>=`), le `.deb` d'antiword est téléchargé en
  `http://` sans somme de contrôle, et `leap_utils` est une copie de 12 272 lignes dont environ 78 % ne servent jamais.
- **Options et conséquences** : option A pour les deux. `requirements.txt` épinglé aux versions testées ; `.deb` en https avec SHA-256
  (amd64 seul) ; `leap_utils` réduit aux 11 modules atteints (3 195 lignes). `.mcp.json` est absent de ce dépôt. Les dépendances transitives
  ne sont pas verrouillées.
- **Avis de Logan** : dépendre d'un paquet épinglé ou copier module par module ; épingler les dépendances et vérifier le `.deb`.
- **Décision / état** : Décidé le 2026-10-21 : option A pour les deux (épinglage direct, `leap_utils` réduit à 11 modules). **Fait** ; dépendances indirectes : voir L-28.

### 8. Prompts de la comparaison et `:rtype:`

- **Le problème** : les prompts de la comparaison étaient des chaînes dans le code Python, contrairement à ceux du chat qui sont des
  fichiers. Yann demande aussi la mention `:rtype:` dans les docstrings.
- **Options et conséquences** : six prompts mis en fichiers (`server/prompts/compare/`, chargés par `compare/prompts.py`, `load_prompt`),
  texte identique, surcharge `COMPARE_ANALYSIS_SYSTEM_PROMPT` gardée. `:rtype:` non ajouté : les annotations de retour suffisent.
- **Avis de Logan** : prompts de la comparaison en fichiers.
- **Décision / état** : Décidé le 2026-10-21 : prompts en fichiers ; annotations seulement, sans `:rtype:` (suite à Logan). **Fait**

### 9. Accès aux documents indexés (L-15)

- **Le problème** : le chat et la recherche d'impact interrogent Vector Search avec le service principal de l'app, donc les droits Unity
  Catalog ne sont pas appliqués par utilisateur ; les liens REF du catalogue couvrent tout le périmètre de parsing.
- **Options et conséquences** : accepter ce contrôle (déjà écrit dans `server/services/infra/README.md`, section « Access to indexed
  documents ») ou filtrer les liens du catalogue par `in_chat` et division. Les liens couvrent volontairement tout le périmètre : un
  document absent de l'index du chat s'ouvre dans le portail Intraqual.
- **Avis de Logan** : documenter comme contrôle accepté.
- **Décision / état** : décidé le 2026-10-21 par Jules : contrôle accepté et liens du catalogue voulus tels quels. **Fait**

## Vérifications

- **Lot 1** : `pytest tests` : 527 réussis, 5 échecs, 12 ignorés ; les 5 échecs (`test_compare_documents` ×3, `test_lakebase_carry_over` ×2)
  existaient avant mes changements, sur ce poste (module PDF, base locale). Essai des routes avec le client de test : `PUT
  /history/{id}/impact` (corps `{"impact_json"}` accepté, corps vide refusé), `POST /feedback` et `/chat/feedback` avec un vote invalide
  (422), `/compare/analyze` et `/compare/impact` sans réglages (erreurs claires).
- **Lot 2** : tests ajoutés pour `refs_named_in` et `one_language_per_document`, la coupe de requête (`query_truncated`), antiword (UTF-8,
  repli), la borne olefile et le diaporama de 3 diapositives avec un vrai LibreOffice (`tests/test_conversion.py`, 10 tests).
- **Lot 3** : `tests/test_excel_processor.py` est nouveau ; les trois découpages (docx, excel, relocation) ont été comparés à l'ancien code : mêmes sorties.
- **Après la revue v3 (2026-10-23)** : 767 tests réussissent (suite complète, local). Les nouvelles règles de parsing (N-2, N-3, N-4, N-9) n'ont jamais tourné sur Spark.
- **Ensemble (état actuel)** : 597 réussis, 12 ignorés, 0 échec avec `requirements.txt` épinglé (pymupdf 1.28.0, Pillow 11.1.0) ; avec
  pymupdf 2.0, les 3 tests PDF échouent ; `check_docstrings.py` : 0 problème. Rien n'a été déployé ni exécuté sur Databricks ; étapes dans
  `runbooks/retest.md`, section « revue de la PR Bitbucket 2 ».

## Reste à faire et points à vérifier

Établi le 2026-10-23. Le code est testé en local mais rien n'a tourné sur Databricks. Une remarque n'est `Fait` que si elle est traitée ou tranchée par Jules ; voici toutes les autres (28 lignes : 10 En attente de Yann, 3 Décision de Jules, 9 Partiel, 6 Ouvert), plus les questions ci-dessous.

### Remarques de Yann sans accord (En attente de Yann)

| # | Problème | Ce qu'il faut |
|---|---|---|
| Y-1 | Yann veut exclure le dossier `.vscode`, j'ai gardé `tasks.json`. | Yann accepte, ou `tasks.json` sort aussi. |
| Y-5 | Il ne voit pas pourquoi il y a deux fichiers de rappel de langue. | Yann accepte la raison, ou on fusionne et on mesure. |
| Y-6 | Il veut un fichier de config `.py`, j'ai gardé les `.md`. | Yann accepte la règle « un prompt = un fichier ». |
| Y-31 | Il veut un `TypedDict`, j'ai mis un dict typé. | Yann accepte, ou un `TypedDict` par forme de ligne. |
| Y-34 | J'ai compris « un module de réglages s'appelle `config` ». | Yann confirme ce qu'il voulait. |
| Y-40 | Il veut ces constantes en config, je les ai gardées dans `turn_log.py`. | Yann accepte la raison. |
| Y-50 | Il demande si ignorer l'échec est normal ; ce ne l'est pas, c'est déjà géré. | Yann accepte la réponse. |
| Y-52 | Il demande si les fichiers antiword restent dans la PR ; je les garde. | Yann accepte, ou on retire les tables inutilisées. |
| Y-53 | Il demande si `.PDF` est géré ; oui, c'était déjà le cas. | Yann accepte la réponse. |
| Y-60 | Il veut mieux que la double boucle ; l'algorithme reste, seulement documenté. | Yann accepte, ou propose une autre méthode. |

### Décision de Jules

| # | Problème | Options |
|---|---|---|
| N-8 | Un run `full` ou `rechunk` écrase `parsed_documents` et perd les lignes SKIPPED. | (a) fusion sur IDDOC (risques : les lignes SUCCESS hors périmètre ne sont plus nettoyées par effet de bord, les lignes des non-cibles survivent, évolution de schéma) ; (b) garder l'écrasement et rajouter les lignes SKIPPED des IDDOC absents du nouveau tableau (sémantique actuelle gardée, tableau de bord corrigé). Recommandation : b. |
| N-10 | `SKIPPED_EMPTY_FOLDER` est effacé à chaque run : nouveau scan quotidien et faux NEW dans `document_change_log`. | A : effacer seulement si le dossier a changé (nouvelle colonne, date de dossier fiable) ; B : garder la ligne et lister le dossier à chaque run pour l'effacer ; C : garder l'effacement quotidien et ne plus journaliser NEW pour un document déjà vide au run précédent. Recommandation : C maintenant, A plus tard. |
| N-12 | La synchro des droits utilisateurs tourne dans le job d'export, deux fois par jour (jusqu'à 12 h plus le cache pour retirer un groupe ; job DEV en pause). | Un job à part avec un planning plus fréquent (par exemple toutes les heures) ; choisir la fréquence. |

Questions ouvertes (pas des lignes du tableau, mais une réponse de Jules est attendue) :

- **N-13** : afficher un avis dans l'interface quand des changements ne sont pas cherchés (le client n'affiche pas l'événement `changes_not_searched`) ?
- **N-13** : le plafond de 2000 caractères par requête d'un changement convient-il ?
- **L-25** : le prompt inline d'`app.yaml` ne s'applique qu'à la méthode `comparative` ; toutes les cibles tournent en `standard`. Le supprimer ou le garder ?
- **N-5 / L-14** : adresse de l'app PROD pour `CHAT_ALLOWED_ORIGINS`, et confirmation que le chat ouvert à tous les utilisateurs de l'app (`_DEFAULT_CAPS`) est un contrôle accepté, comme L-15.
- **L-6** : règle pour les colonnes modifiées dans plus de la moitié des lignes (aujourd'hui écartées, nom seul listé) et pour une feuille ajoutée (aujourd'hui « sheet added (N rows) »).
- **N-b3** : déclarer le volume de transit pour UAT et PROD dans le bundle (risque de conflit avec le volume UAT existant dans l'état du bundle), ou le laisser créé à l'exécution.

### Partiel

| # | Problème | Ce qu'il faut |
|---|---|---|
| L-6 | Colonne modifiée dans plus de la moitié des lignes (feuilles de 20 lignes ou plus) encore écartée ; feuille ajoutée peu détaillée. | Décision de Jules sur la règle. |
| L-9 | Jeton longue durée ; la vérification de branche est dans le yml que la branche peut modifier ; `Production` pas encore limité à `main`. | OIDC (administrateur de compte), puis restriction après validation (Jules : plus tard). |
| L-14 | Le contrôle d'origine du WebSocket n'est pas configuré en PROD. | Voir N-5. |
| L-20 | Le tableau de bord DEV lit d'anciennes tables figées ; l'export PROD attend les droits de son principal de service. | Changer ses sources (Jules : plus tard) ; donner les droits avant le déploiement PROD. |
| L-21 | Cluster GPU sans politique, planning PROD en pause, la fusion d'une nouvelle description ne supprime jamais les anciens passages d'images. | Quelques runs manuels validés, puis dé-pauser (Jules : plus tard) ; le reste à regarder à l'exécution. |
| L-28 | Bibliothèques des jobs non épinglées ; `uv.lock` périmé. | Épingler les bibliothèques des jobs ; le `uv.lock` est celui du dépôt de Jules. |
| L-b17 | Le code client n'a aucun test automatique. | Ajouter des tests client, ou accepter. |
| N-5 | Origine WebSocket de PROD non renseignée ; chat ouvert à tous = choix à confirmer. | Renseigner `CHAT_ALLOWED_ORIGINS` quand l'app PROD existe ; confirmer avec L-15. |
| N-b1 | Les vidages de commentaires déjà commités restent dans le dépôt. | `git rm --cached` dans le dépôt de Jules. |

### Ouvert

| # | Problème | Ce qu'il faut |
|---|---|---|
| N-b2 | Rôle Lakebase de PROD : identifiant du SP interpolé avec `prevent_destroy`. | Mettre l'identifiant du SP de l'app en dur une fois l'app PROD créée. |
| N-b3 | Volume de transit non déclaré pour UAT et PROD. | Décider (voir les questions). |
| N-b13 | Tableaux DOCX des en-têtes/pieds de page ignorés. | Les lire. |
| N-b4 | L'alerte d'échec de job lit `system.lakeflow` sans filtre `workspace_id`. | Identifiants d'espace de travail par cible (DEV 2865348338307293, UAT 3155144190025826, PROD à trouver). |
| N-b5 | Nouvel essai de rang 2 à chaque run complet ; images disparues jamais supprimées. | À voir à l'exécution, avec L-21. |
| N-b6 | opencv 4.12 à côté de `numpy<2` sur la tâche GPU. | À vérifier à l'exécution sur le cluster GPU. |

### À vérifier après déploiement (non éprouvé sur Databricks)

- Pipeline Bitbucket : téléchargement de la CLI 1.20.0, installation de bun, construction du client.
- Politique de sécurité (CSP) : aucune violation dans la console ; l'app s'affiche (y compris en iframe si la plateforme l'utilise, `frame-ancestors 'self'`).
- Réécriture en panne : la recherche de secours demande 36 (ou 32) passages réordonnés ; la limite du réordonnanceur n'est pas vérifiée.
- 401 sans identité : non testable en DEV (contrôle d'accès coupé) ; à voir en UAT.
- Statut `check` de la recherche d'impact : effet sur les verdicts non mesuré.
- Découpage des rapports, formules Excel, `.ppt`, images réduites à 2000 px : à essayer sur quelques vrais fichiers.
- Migration du schéma Lakebase (`analysis_cache`, index, verrou) : premier démarrage de DEV.
- Imports de `leap_utils` réduit à 11 modules : une tâche courte du parsing.
- Attente de l'index (`ONLINE_TRIGGERED_UPDATE`) : noms d'état à confirmer dans un vrai run.
- Interface : aucun test automatique côté client.
- WebSocket en PROD : refus de toute origine tant que `CHAT_ALLOWED_ORIGINS` n'est pas renseigné ; refus d'un appelant sans identité.
- Chemin d'historique : `POST /history` avec d'anciennes sessions (une ligne qui nomme déjà un dossier ancien est acceptée, un chemin d'un autre est mis à NULL).
- Limite de débit sur le chat (WebSocket) et l'aperçu : seuil de 30 requêtes par minute et par utilisateur, assez large pour un usage normal.
- Nouvelles règles du parsing (statut d'erreur testé en premier, déduplication du checkpoint, garde `PARSING_MAX_SCOPE_DROP_PCT`, révisions remplacées exclues en mode complet) : jamais exécutées sur Spark ; un run court sur DEV.

### Avant la mise en production

- `prevent_destroy` du rôle Lakebase de PROD posé ; si l'app est recréée, mettre l'identifiant du principal de service en dur (N-b2).
- Renseigner `CHAT_ALLOWED_ORIGINS` de PROD avec l'adresse de l'app PROD (N-5) ; sans cela PROD refuse tout WebSocket.
- Export Lakebase de PROD dé-pausé : le principal de service de PROD doit avoir ses droits avant le déploiement.
