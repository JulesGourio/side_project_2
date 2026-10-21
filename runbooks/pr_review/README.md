# Journal de revue — PR Bitbucket 2 (`latec-compare`)

Un bloc par commentaire (id Bitbucket), dans l'ordre de l'export. Les nouveaux commentaires s'ajoutent en bas, dans un
nouveau lot. Récupération : `python debug\fetch_comment_pr.py` (copie de `runbooks/scripts/bitbucket_pr/fetch_pr_comments.py`).

- PR faite sur le commit `14296499a19baf710dc6c10d74bdd71204176b2d`. Les lignes citées sont celles de ce commit ; les fichiers
  concernés n'ont pas changé depuis, sauf mention contraire, mais plusieurs numéros de ligne ne tombent pas pile sur le code
  visé : chaque commentaire a été rattaché au code par son sens.
- Relecteur : Yann Depledt. Jules Gourio a laissé un commentaire à lui-même (#885301989).
- Vérification de chaque lot : `pytest tests` (résultat en bas du lot), `runbooks/scripts/code_quality/check_docstrings.py server/routers`,
  et un essai des routes touchées avec le client de test FastAPI.

## Lot 1 — commentaires exportés le 2026-10-19 (41 commentaires)

### Fichiers de dépôt

**#885061633 — `.vscode/settings.json` L1 — « Exclure le .vscode non ? »** — Fait en partie.
`settings.json` (marqueurs de cellules Jupyter, réglage personnel) sort du suivi git et entre dans `.gitignore`.
`.vscode/tasks.json` reste versionné : c'est la tâche « Pull + Deploy » que le README documente.

**#885059913 — `README.md` L84 — « Switch to 6 Luna »** — Fait (lot 1, puis étendu le 2026-10-20 sur ta demande).
Le pont de traduction du chat, le jugement et le résumé de la comparaison (texte et images) ont `databricks-gpt-6-luna` pour
modèle principal, dans le code et dans `app.yaml`. Le repli est `databricks-gpt-5-6-luna` : le chat le prend dans `app.yaml`,
la comparaison le choisit seule (`compare_chain` : l'autre modèle Luna). Le parsing (description d'images) était déjà sur
GPT-6 Luna dans le code ; seule la ligne du README était restée sur l'ancien modèle. **Non mesuré** : le jugement d'impact change
de modèle sans comparaison des verdicts ; à regarder sur DEV (voir `runbooks/retest.md`).

**#885062343 — `client/out/index.html` L1 — « Pourquoi en double avec client/index.html »** — Pas de changement.
`client/index.html` est la source de Vite ; `client/out/index.html` est le résultat du build (mêmes balises plus les scripts et
feuilles de style au nom haché). `client/out/` est versionné exprès : l'application déployée sert ce dossier, et `databricks.yml`
n'envoie que lui (`client/src/**` et `client/public/**` sont exclus de la synchronisation).

**#885062844 — `client/public/logos/databricks-symbol-color.svg` — « Pareil logos en double ? »** — Pas de changement.
Ce fichier n'existe pas dans le dépôt que j'ai (il ne reste que `LOGO_LATECOERE.png` et `LOGO_Qualibot.png`). Les logos de
`client/public/logos/` et de `client/out/logos/` sont le même cas que ci-dessus : `out/` est la copie produite par le build.
Si le fichier existe chez toi, envoie-moi son chemin exact.

### Prompts du chat

**#885070557 — `server/prompts/chat/language_reminder_named.md` L1 — « pas compris pourquoi deux »** — Pas de changement de code ; explication ajoutée.
Les deux rappels servent à deux situations : `language_reminder.md` (langue inconnue) demande de répondre « dans la langue de la
question » ; `language_reminder_named.md` (langue détectée par fastText) la nomme avec `{language}`, ce qui est une consigne plus
forte. Un seul fichier changerait la formulation du cas « inconnu », donc le comportement mesuré. La raison est écrite dans
`server/prompts/README.md`.

**#885071187 — `language_reminder_named.md` L1 — « + j'aurais plutôt mis dans un fichier de config .py »** — Non fait.
Règle du projet : un prompt du chat est un fichier, jamais une chaîne Python (le job `register_chat_prompts` hache les fichiers et
une trace ne lie la version enregistrée que si le hash correspond). Ce qui varie entre prompts va en revanche dans la config
Python (voir #885138943).

**#885138943 — `server/prompts/chat/system/as.md` L1 — « Variabiliser pour réutiliser le même texte entre IS/AS »** — Fait.
`as.md` et `is.md` sont remplacés par `system/division.md`, un gabarit (`{code}`, `{name}`, `{other_code}`, `{other_name}`), rempli
par `division_prompt()` avec `DIVISION_NAMES` dans `server/services/chat/engine/config.py`. La règle propre à AS (ordre QP puis MI)
va dans `system/as_reference_ordering.md`, ajoutée au seul prompt AS. Le texte envoyé au modèle est le même à la mise en forme
près. Les clés du registre changent (`system_as`, `system_is` → `system_division`, `system_as_references`) : relancer
`register_chat_prompts` (voir `runbooks/retest.md`).

**#885138148 — `system/base.md` L2 — « un petit langdetect en amont ? »** — Pas de changement ; proposition.
La détection en amont existe déjà : fastText (`fast_lang_guess`) détecte la langue et `answer_language` la nomme dans le rappel
`{language}` ajouté après la question. Le bloc « CRITICAL LANGUAGE RULES » de `base.md` couvre le cas où la détection est
incertaine (rappel non nommé) et la règle de citation dans la langue du document. Le retirer change la configuration mesurée
(`archive/docs/chat_tests.md`) : à faire avec une mesure sur DEV avant, pas à l'aveugle.

### Routes du chat

**#885145601 — `server/routers/chat/helpers.py` L13 — « réfléchir en tokens plutôt que conversation turn ? »** — Fait.
`CHAT_MAX_HISTORY` (10 messages) est remplacé par `CHAT_MAX_HISTORY_TOKENS` (8000 par défaut, `app.yaml`). Les messages les plus
récents qui tiennent dans le budget sont gardés ; la dernière question l'est toujours, même seule au-dessus du budget ; la fenêtre
n'ouvre jamais sur un message de l'assistant. Taille estimée à 4 caractères par jeton, sans tokenizer (un appel de plus par tour
pour une fenêtre approximative n'apporte rien). Test mis à jour.

**#885146344 — `helpers.py` L111 — « same »** — Fait (lu comme #885146016 : constantes en haut). `_SOURCE_FIELDS` remonte avec les autres.

**#885146016 — `helpers.py` L36 — « bouge les constantes au début »** — Fait. `_CITE_MARKER_TMPL`, `_PROTECTED_SPAN_RE`, `_SOURCE_FIELDS`,
`CHAT_MAX_HISTORY_TOKENS` et les motifs de division sont en tête du module.

**#885147969 — `server/routers/chat/persistence.py` L8 — « Import absolu c'est mieux je pense »** — Fait, pour tout `server/`.
Les imports relatifs (`from ..services…`, `from .helpers…`) deviennent `from server.…` dans les 53 fichiers concernés, pour que le
style soit le même partout. Un script a fait la conversion ; le code compile, les tests passent.

**#885149621 — `server/routers/chat/sessions.py` L13 — « relatif… »** — Fait (même changement que #885147969).

**#885148302 — `persistence.py` L19 — « Doc function! »** — Fait. Docstring de `_upsert_session` avec ses paramètres.

**#885148691 — `persistence.py` L47 — « missing sphynx params ! A check dans toute la base de code »** — Fait (lot 1 pour `server/routers/`, 2026-10-20 pour `server/services/`).
Docstrings Sphinx (`:param`, `:return:`) sur toutes les fonctions de `server/` : `runbooks/scripts/code_quality/check_docstrings.py` ne signale
plus aucun `:param` manquant. La passe sur `server/services/` (51 fichiers, ~460 fonctions) a été écrite par un agent Haiku 5.5
(effort moyen), puis contrôlée : l'arbre syntaxique de chaque fichier est identique à celui d'avant une fois les docstrings
retirées (aucune ligne de code modifiée), les tests donnent le même résultat. Les descriptions les moins sûres sont à relire en
priorité : `engine/answer.py` `revision_note`, `compare/export_helpers.py` `_build_excel_bytes`, `_diff_engines/relocation.py`
(`_drop_present_verbatim`, `_align_duplicates`), `infra/error_origin.py` `caller`, `_diff_engines/images.py`
`_compute_image_diff_pairs`, `_diff_engines/engines.py` `_order_key`, `infra/vector_search.py` `run_impact_search`.
Reste ouvert : `CLAUDE.md` demande des commentaires courts, ce qui va à l'encontre de ces docstrings (décision du propriétaire).

**#885148761 — `persistence.py` L30 — « type »** — Fait. `pool: asyncpg.Pool`, `conn: asyncpg.Connection`, `sources: List[dict] | None`
(`persistence.py` et `sessions._turn_trace_id`).

**#885246366 — `server/routers/chat/ws.py` L159 — « 1 seul if »** — Fait (lot 1 en partie, terminé le 2026-10-20).
Le `if … pass / else …` du texte pendant la traduction est devenu un seul `if not (…)`. Le test de fin de flux, qui imbriquait
deux `if` dans la boucle, est sorti dans la fonction `_finish_turn()` : la boucle n'a plus qu'un `if chunk.startswith('data: [DONE]')`,
et `_finish_turn` traite d'abord le cas « flux sans texte » (sortie anticipée), puis le cas normal, sans `else`.

**#885245561 — `ws.py` L16 — « Changer le nom de ma var de base »** — Fait. `ENABLED` de `translation_bridge` devient
`TRANSLATE_BRIDGE_ENABLED` (comme la variable d'environnement), défini dans `server/services/chat/config.py`. L'alias `as` est
supprimé de `ws.py`, de `routers/chat/__init__.py` et des notebooks d'évaluation.

### Routes de comparaison

**#885248209 — `server/routers/compare/analyze.py` L57 — « Please ptit comments pour expliquer »** — Fait. Commentaires sur le sémaphore
(`MAX_CONCURRENT_ANALYSES`), les en-têtes SSE (`_SSE_HEADERS`) et `_sanitize_messages` ; docstrings de toutes les fonctions du module.

**#885252966 — `server/routers/compare/common.py` L86 — « docstrings + param descriptions »** — Fait.

**#885252414 — `common.py` L59 — « les valeurs par défaut dans un fichier de config perso. A discuter »** — Fait.
Chaque valeur par défaut de `get_config()` est une constante nommée `DEFAULT_*` dans `server/routers/compare/config.py`.

**#885251487 — `common.py` L18 — « Config file! ou alors ton common = config ? »** — Fait.
`common.py` est coupé : `config.py` (réglages, `APP_VERSION`, taille maximale, nombre d'analyses simultanées, prompt d'analyse par
défaut, `get_config()` sans souligné) et `common.py` (validation des fichiers, noms sûrs, `_is_within_volume`). Les imports de
`analyze`, `impact`, `summarize`, `volume`, `exports`, `history` sont mis à jour.

**#885255107 — `server/routers/compare/impact.py` L72 — « Fonction à split ? t'en penses quoi ? »** — Fait (mon avis : oui, mais pas en
la coupant en morceaux).
Les fonctions internes `_search`, `_audit`, `_warn`, `_judges` ne dépendaient que de valeurs fixées avant le flux : elles deviennent
les méthodes de `_ImpactSearch`. Le flux `_impact_events` est un générateur de module. `_counts` reste interne : il lit l'état qui
change pendant le flux. `find_impacted_documents` fait une cinquantaine de lignes.

**#885254220 — `common.py` L114 — « Boolean complexe à détailler en variables »** — Fait (`climbs_out`, `is_root`, `is_below_root`).

**#885251206 — `analyze.py` L123 — « Casser la fonction trop longue + ajouter les rtype »** — Fait.
`analyze_documents` passait de 290 lignes à une soixantaine. Extraits : `_AnalysisJob` (données de la requête), `_read_uploads`,
`_upload_to_volume`, `_acquire_slot`, `_keepalive_until_done`, `_track_llm_event`, `_analysis_events`. Types de retour ajoutés sur
tout `server/routers/`. `tests/test_compare_analyze_stream.py` (nouveau) couvre le flux ; il passe aussi sur l'ancien code, donc
le comportement est inchangé.

**#885259506 — `server/routers/exports.py` L101 — « choisis Optional[Type] ou Type | None et applique partout »** — Fait.
Choix : `Type | None` (Python 3.11 minimum d'après `pyproject.toml`, et déjà employé dans `exports.py`). Converti dans tout
`server/` (34 fichiers), imports `Optional` retirés. Les annotations entre guillemets deviennent `'X | None'`.

### Feedback et historique

**#885261374 — `server/routers/feedback.py` L18 — « pq str alors ? Enforcer up or down »** — Fait. `vote: Literal['up', 'down']`
dans les deux modèles de `feedback.py` et dans celui de `chat/sessions.py`. Une valeur invalide donne le 422 de FastAPI.

**#885261904 — `feedback.py` L26 — « T'aurais plus besoin de ces checks »** — Fait. Les contrôles manuels sont supprimés.

**#885262081 — `feedback.py` L62 — « Same! »** — Fait (même suppression pour le second endpoint, et pour le chat).

**#885262563 — `server/routers/history.py` L20 — « config file »** — Fait. `APP_VERSION` s'importe de `compare/config.py`.

**#885266896 — `history.py` L47 — « pas dict mais dict[Type, Type]… TypedDict c'est mieux »** — Fait avec `dict`, pas `TypedDict`.
`_serialize(row: Mapping[str, Any]) -> dict[str, JsonScalar]`. Pas de `TypedDict` : la fonction sert à `SELECT *` comme à des
listes de colonnes, les clés ne sont pas fixes.

**#885267451 — `history.py` L144 — « class pour 1 var ? »** — Fait. `SaveImpactRequest` est supprimée ; la route lit
`impact_json: str = Body(..., embed=True)`. Le JSON envoyé par le navigateur est le même (`{"impact_json": "…"}`), vérifié.

### Services du chat

**#885269686 — `server/services/chat/engine/config.py` L67 — « en haut »** — Fait. `_RAW_ON_ALL` et `_RAW_ON` remontent avec les autres constantes.

**#885269518 — `engine/config.py` L32 — « config == config pas common »** — Compris comme : un module de réglages s'appelle `config`.
`engine/config.py` l'était déjà ; c'est le `common.py` de la comparaison qui est corrigé (#885251487). À confirmer si je me suis trompé.

**#885270078 — `server/services/chat/engine/queries.py` L49 — « config »** — Fait. `_COMPANY` et `_NOISE_PHRASES` passent dans
`engine/config.py` (`NOISE_PHRASES`).

**#885271146 — `server/services/chat/mlflow_turns.py` L119 — « en haut + config »** — Fait. L'import de `translation_bridge` est en tête du
module (il était dans la fonction), et les limites de l'export (`MLFLOW_*`) sont dans le nouveau `server/services/chat/config.py`.
`_fast_lang_guess` devient publique (`fast_lang_guess`) puisqu'elle sert à plusieurs modules ; tests et notebooks d'évaluation mis à jour.

**#885271431 — `mlflow_turns.py` L287 — « pas cool à lire »** — Fait. Le span `rewrite` utilise des variables nommées avant l'appel, et
le libellé des appels Vector Search est calculé par `_vs_call_label()`.

**#885272147 — `server/services/chat/translation_bridge.py` L67 — « config! »** — Fait. Les motifs de nettoyage de texte, les seuils de
détection et les noms de langues vont dans `services/chat/config.py`.

**#885271954 — `translation_bridge.py` L36 — « model hardcodé ! »** — Fait. Les modèles par défaut sont dans `services/chat/config.py` (et
passent à GPT-6 Luna, voir #885059913) ; `app.yaml` les fixe aussi.

**#885272670 — `server/services/chat/turn_log.py` L33 — « config »** — Pas de changement ; à discuter.
`INFO`, `WARNING`, `ERROR` sont le vocabulaire du journal de tour (importé par `ws.py`, le moteur et `lakebase`), pas des réglages
qu'on ajuste par cible. Les mettre en config ferait dépendre le journal d'un module de réglages.

### Commentaire de Jules

**#885301989 — `utils/lakebase_sync/export_lakebase_uat_to_volume_job.py` L1 — « Les jobs là sont refaits… »** — Déjà fait avant cette revue.
Le commit `97f8853` a remplacé les jobs par un seul (`lakebase_export`, `export_lakebase_to_volume`, `import_lakebase_tables_to_delta`),
sans `uat` dans les noms, avec catalogue et planning par cible ; chaque job copie Lakebase{env} vers UC{env}. Il ne reste aucun
fichier ni aucune référence `export_lakebase_uat*`. Rien à changer.

### Vérification du lot 1

- `pytest tests` : 527 réussis, 5 échecs, 12 ignorés. Les 5 échecs (`test_compare_documents` ×3, `test_lakebase_carry_over` ×2) sont
  les mêmes avant mes changements, sur ce poste (module PDF et base locale).
- Essai des routes avec le client de test : `PUT /history/{id}/impact` (corps `{"impact_json"}` accepté, corps vide refusé),
  `POST /feedback` et `/chat/feedback` avec un vote invalide (422), `/compare/analyze` et `/compare/impact` sans réglages (erreurs claires).
- Rien n'a été déployé ni exécuté sur Databricks. Étapes à faire : `runbooks/retest.md`, section « revue de la PR Bitbucket 2 ».

### Points ouverts pour le relecteur

1. ~~Passer la comparaison et le parsing sur GPT-6 Luna~~ : décidé le 2026-10-20, fait (#885059913).
2. Le retrait des règles de langue de `base.md` demande une mesure sur DEV (#885138148).
3. `CLAUDE.md` (commentaires courts) contredit les docstrings Sphinx, désormais sur tout `server/` : à aligner si Yann maintient sa demande (#885148691).
4. `turn_log.py` : garder les niveaux de gravité dans le module du journal ? (#885272670)

## Lot 2 — commentaires exportés le 2026-10-20 (8 commentaires)

PR au commit `c7d44ccaefee72b53c4afe04caed162b877dbb23`. Les numéros de ligne cités ne correspondent pas exactement à ce
dépôt (les docstrings ont décalé les fichiers) : chaque commentaire a été rattaché au code par son sens.

**#885789675 — `engine/retrieval.py` L52 — « pas super lisible mais bon à voir si on change »** — Fait.
`refs_named_in` : la double boucle imbriquée devient une liste de REF (la référence et ses variantes de langue) dédupliquée
une fois par `dict.fromkeys`. Même simplification pour la construction de `named` dans `retrieve_for_turn` et pour
`one_language_per_document` (boucle explicite à la place d'un `setdefault` dans une compréhension). Comportement identique ; deux tests
ajoutés (ordre et déduplication des REF, une variante de langue par document).

**#885790057 — `retrieval.py` L57 — « Any c'est pas incroyable. Si possible des TypedDict »** — Fait.
Nouveau `engine/types.py` : `SearchRow` (ligne de l'index), `Hit`, `PassageRecord` (enregistrement `chat_retrieved_chunks`) et
`DocumentPassages`. Ils remplacent `Dict[str, Any]` dans `search.py` et `retrieval.py`. Les filtres et le corps des requêtes
gardent `Dict[str, Any]` : leur forme est libre. Aucun outil de typage statique ne tourne sur le dépôt, donc ces types
documentent et aident l'éditeur, ils ne sont pas vérifiés à l'exécution.

**#885793573 — `engine/search.py` L54 — « danger !! Aucun avertissement qu'on coupe la query »** — Fait, et c'était vrai.
Une requête de plus de 20 000 caractères était coupée sans trace. Maintenant : un avertissement dans les logs, un incident
`query_truncated` sur le tour (colonne `warnings`, ligne dans `app_errors`) et la longueur d'origine dans `vs_calls[].truncated_from`.
Code ajouté à la liste de `schema.md`. Deux tests.

**#885835700 — `compare/conversion.py` L44 — « danger si concurrence non ? »** — Fait, et c'était vrai.
`_libreoffice_cmd` marquait la recherche comme faite avant de l'avoir terminée : un second thread arrivant pendant la
recherche recevait `None` et prenait le rendu de secours. La fonction est supprimée : la conversion passe par `soffice.py`
(`convert_docx_to_pdf`), qui cherche le binaire une fois sous verrou et limite les conversions simultanées. La vérification
d'antiword est aussi mise sous verrou.

**#885835950 — `conversion.py` L94 — « encodage manquant non ? »** — Fait, et c'était vrai.
antiword était appelé sans fichier de correspondance, donc il écrit en Windows-1252 alors qu'on lisait de l'UTF-8 : les accents
étaient remplacés par `�`. Maintenant `-m UTF-8.txt` (comme le pipeline de parsing). Si ce fichier n'est pas installé, antiword
échoue : on relance sans lui et on lit en Windows-1252. Deux tests.

**#885834566 — `conversion.py` L162 — « pq 400 ? »** — Fait (nommé et signalé).
Le plafond de 400 passages de texte extraits d'un `.doc` par olefile était un nombre libre dans le code. C'est une borne contre
le bruit du balayage binaire : constante `_OLEFILE_MAX_RUNS`, et un avertissement dans les logs quand elle coupe. Valeur inchangée :
je n'ai pas de mesure qui dise mieux.

**#885833326 — `conversion.py` L178 — « ça a été testé cette fonction ? »** — Non, pas avant ; fait.
Aucun test ne couvrait `conversion.py`. `tests/test_conversion.py` (10 tests) couvre : le texte `.docx` et le cas vide, antiword en UTF-8
et en repli, la borne d'olefile, un fichier qui n'est pas OLE, et le rendu des diapositives, y compris un vrai essai avec LibreOffice.

**#885833609 — `conversion.py` L200 — « tu es sûr que ça convertit toutes les pages »** — Non : c'était faux. Corrigé.
L'export PNG de LibreOffice ne rend que la première diapositive (vérifié ici sur un diaporama de 3 : un seul fichier). L'aperçu
d'un PowerPoint n'affichait donc qu'une image. Maintenant LibreOffice produit un PDF et chaque page devient une image PNG, dans
l'ordre, 100 diapositives au plus (avertissement dans les logs au-delà). Test avec un vrai diaporama de 3 diapositives : 3 images.

## Lot 3 — commentaires exportés le 2026-10-21 (14 commentaires)

PR au commit `7c2b82c51a07a3210778879a6f3e4571940b718b`. Plusieurs commentaires portent sur une version plus ancienne de la
PR : les numéros de ligne ne correspondent plus, chaque commentaire est rattaché au code par son sens.

**#886269905 — `compare/processors/factory.py` L89 — « param sous cette forme :param: »** — Fait.
`get_processor` gardait un bloc `Args:` (style Google) au-dessus des `:param`. Il est supprimé et son contenu (valeurs de
`method_override`) passe dans le `:param`. Il n'y avait qu'un autre bloc de ce style, `Returns:` dans `section_canonical_diff`
(`_diff_engines/engines.py`, commentaire #885860992) : supprimé aussi, le `:return:` le disait déjà.
Pas de `:rtype:` : les annotations de retour portent les types. Question ouverte : voir la fin du lot.

**#885860006 — `compare/llm.py` L236 — « C'est normal d'ignorer ? »** — Oui, et c'est maintenant écrit.
L'échec d'un modèle n'est pas ignoré : il est journalisé, le modèle est mis en pause, le suivant de la chaîne est essayé, et
`CompareLlmUnavailable` est levée quand aucun ne répond. Un commentaire le dit.

**#885864584 — `compare/processors/docx/extract.py` L27 — « fonction trop longue à split »** — Fait.
`_extract_docx` (200 lignes, 6 fonctions imbriquées qui partageaient 9 variables) devient la classe `_DocxWalk` (`emit`,
`count_breaks`, `harvest_images`, `walk_paragraph`, `walk_table`, `finish`), plus `_oriented_image` (lecture et retournement d'une image)
et deux petites fonctions de module. Même résultat que l'ancien code sur 3 documents de test (texte, tableaux, images
retournées, saut de page).

**#885869279 — `third_party/antiword/share/8859-1.txt` L69 — « C'est quoi ces trucs ? Il faut les garder dans la PR ? »** — Garder.
Ce sont les tables de caractères du programme antiword (conversion des `.doc` en texte), plus l'exécutable. Le job de parsing
les lit (`ANTIWORDHOME`) : le cluster ne peut pas installer antiword. L'app, elle, installe le paquet `.deb` au démarrage
(`start.sh`) et n'utilise pas ces fichiers. Le README de `third_party/` le décrit. Seule `UTF-8.txt` est demandée par le code ;
les autres tables viennent avec le paquet et ne gênent pas, mais on peut les retirer si tu préfères une PR plus courte.

**#886268712 — `factory.py` L27 — « pas de .PDF ? »** — Déjà géré. `get_extension` passe l'extension en minuscules : `Plan.PDF` est un PDF.
Test ajouté (`tests/test_excel_processor.py`).

**#886269539 — `factory.py` L34 — « xlmx en + ? »** — Fait. `.xlsm` (classeur avec macros) est ajouté côté serveur
(`EXTENSION_MAP`, aperçu) et côté client (`files.ts`). openpyxl le lit comme un `.xlsx` (test avec un vrai contenu de type xlsm).
Le client versionné `client/out` doit être reconstruit pour que le navigateur accepte `.xlsm`.

**#886274477 — `compare/soffice.py` L126 — « comments »** — Fait. Commentaires sur le marqueur de fin d'extraction, le dossier remis à
zéro, le téléchargement par blocs, la confiance dans l'archive et le bit exécutable.

**#886275266 — `compare/summarize.py` L18 — « Mérite un fichier de config »** — Fait pour les nombres.
`SUMMARY_LLM_TIMEOUT_S` va dans `services/compare/config.py`. Les deux prompts de résumé restent des constantes Python :
la règle « un prompt est un fichier » de `CLAUDE.md` vise le chat. Question ouverte : voir la fin du lot.

**#886276049 — `infra/pdf_report/markdown.py` L58 — « Détailler avec un comments ce que ça fait »** — Fait. Commentaire sur l'expression
régulière du Markdown en ligne : l'ordre des alternatives (code, lien, citation, gras-italique, gras, italique, barré, URL) et pourquoi.

**#886277833 — `infra/vector_search.py` L49 — « pareil config »** — Fait pour les nombres. Délais, nombre de requêtes en parallèle,
reprises, plafonds de passages et de jetons du juge d'impact vont dans `services/infra/config.py` (`IMPACT_*`,
`VECTOR_SEARCH_QUERY_TIMEOUT_S`), importés par la recherche d'impact et par le moteur du chat. Le prompt du juge reste en Python (voir ci-dessus).

**#885862606 — `_diff_engines/relocation.py` L196 — « mettre en haut »** — Fait. Les trois constantes (`_RELOCATION_COVERAGE_THRESHOLD`,
`_RELOCATION_MIN_RUN_WORDS`, `_STRIKETHROUGH_RE`) rejoignent les autres en tête du module.

**#885862314 — `relocation.py` L58 — « double boucle. Pas moyen de mieux faire ? »** — Non pour l'algorithme, mieux pour la lecture.
La double boucle remplit une table de programmation dynamique : le meilleur appariement des blocs identiques qui garde leur ordre et
minimise l'écart de position. Un appariement glouton (le plus proche) se trompe dès qu'un bloc est inséré tôt dans une version.
Le coût est borné (`_DUP_ALIGN_MAX_CELLS` : au-delà, appariement dans l'ordre). La table est maintenant calculée par
`_drift_cost_table`, avec sa docstring ; `_align_duplicates` ne fait plus que lire l'appariement. Résultat identique à l'ancien code
sur 3000 cas tirés au hasard.

**#885865732 — `compare/processors/excel.py` L64 — « split »** — Fait. `_build_diff_keyed` (170 lignes, 3 fonctions imbriquées) est coupée en
`_parse_sheets`, `_parse_rows`, `_is_auto_increment`, `_column_name`, `_volatile_columns`, `_row_fields`, `_modified_rows` et
`_sheet_diff`. Même résultat que l'ancien code sur 400 tirages au hasard, sauf un défaut corrigé : une feuille sans ligne de données
faisait planter l'ancien code (`IndexError`) ; elle donne maintenant un rapport. Tests ajoutés (`tests/test_excel_processor.py`).

### Vérification du lot 3
- Aucun test ne couvrait ces fichiers avant ce lot ; `tests/test_excel_processor.py` est nouveau. Les tests donnent les mêmes échecs
  qu'avant mes changements (PDF, base locale) plus `test_code_conventions` (notebooks du parsing modifiés par une autre
  session : les cellules de code consécutives).
- Les trois découpages (docx, excel, relocation) ont été comparés à l'ancien code : mêmes sorties.

### Points ouverts pour le relecteur
1. `:rtype:` en plus de `:return:` ? Les annotations de retour disent déjà le type ; l'écrire deux fois risque la divergence. Réponse
   proposée : garder les annotations, et ajouter `:rtype:` seulement si l'outil de documentation le demande.
2. Les prompts de la comparaison (résumé, juge d'impact, diff) en fichiers sous `server/prompts/`, comme ceux du chat ?
