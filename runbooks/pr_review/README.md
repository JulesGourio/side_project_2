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


## Revue indépendante d'un autre agent — 2026-10-21 (`QualiBOT_PR_Review.html`)

Revue en lecture seule du commit `db1f6d7` (PR « Full code of DEV for review »), faite le 2026-10-20 par un autre agent : 3 critiques,
7 hautes, 19 moyennes, une vingtaine de basses, plus un jugement sur chacun des 48 commentaires de Yann. Le verdict : code bien construit
(SQL paramétré, contrôle d'accès fail-closed, citations cohérentes, repli LLM borné), mais **pas prêt pour PROD** pour deux raisons :
les utilisateurs ne sont pas isolés entre eux (fichiers, cache d'analyse, sessions de chat) et plusieurs faux négatifs silencieux
(colonnes Excel, signes inversés, révisions remplacées remises dans l'index). L'agent n'a pas lancé les tests (dépendances absentes).
Le texte complet est conservé ci-dessous ; la colonne « Traitement » est ce qui a été fait ici le 2026-10-21.

Légende : **Fait** = corrigé dans ce dépôt avec test ; **Partiel** = une partie corrigée, le reste est dit ; **Ouvert** = pas fait ;
**Décision** = demande ton choix (voir la fin de la section).

### Les 29 constats

| # | Gravité | Constat (résumé fidèle) | Traitement |
|---|---|---|---|
| 1 | Critique | `/api/preview` écrit l'upload à `os.path.join(tmp, filename)` : un nom absolu ou `../` sort du dossier ; la route n'a ni `require_compare`, ni taille, ni type. Écrasement possible du binaire LibreOffice ou de `client/out/index.html`. | **Fait.** Suffixe vérifié (`_SAFE_SUFFIX`), nom fixe, `require_compare` + `_validate_file` sur les deux routes, rendu hors boucle d'événements, erreurs génériques. |
| 2 | Critique | `session_path` (query) jamais vérifié : lecture et écrasement des documents d'autres utilisateurs (`/compare/load`, `save-*`) ; dossiers nommés à la seconde. | **Fait.** Racine par utilisateur + dossier UUID, `owns_session` sur `load` et `save-*` (403), plus de copie `.tmp` inutile à l'analyse. |
| 3 | Critique | En mode `full`, le checkpoint n'est pas filtré sur le manifeste : révisions remplacées remises dans `chunks` et l'index. | **Fait.** Checkpoint filtré sur les IDDOC cibles dans tous les modes (`build_steps.py`). Le nettoyage des lignes de checkpoint hors manifeste n'est pas ajouté (**Ouvert**, simple ajout si tu le veux). |
| 4 | Haute | Cache d'analyse alimenté par `POST /history` (corps de requête) et servi aux autres utilisateurs avec les identifiants du premier. | **Fait.** Table `analysis_cache` écrite par le serveur seul en fin de flux réussi ; le résultat en cache ne renvoie plus d'identifiant d'un autre utilisateur. |
| 5 | Haute | Le lien de partage donne l'id réel de session ; le WebSocket accepte n'importe quel `session_id` et l'`upsert` ne vérifie pas le propriétaire. | **Fait.** Vue partagée avec le jeton comme id, `upsert` conditionné au propriétaire (compte de lignes), `session_not_owned` enregistré. |
| 6 | Haute | Le diff perd de vrais changements : colonnes Excel « volatiles », feuilles ajoutées, colonnes ajoutées, en-têtes ; signes perdus (−40 → 40) ; XML malformé = « aucun changement ». | **Fait**, sauf les formules (`data_only=True`, **Ouvert**) : colonne exclue seulement avec ≥ 20 lignes et listée, feuilles et colonnes nouvelles rapportées, changement d'en-tête, signes capturés, XML malformé = erreur visible. |
| 7 | Haute | Une révision remplacée perd ses chunks mais garde `SUCCESS` : `MISSING_CHUNKS`, re-parsing et re-description d'images payante, un jour sur deux. | **Fait.** Statut terminal `SUPERSEDED_REVISION` à l'élagage. |
| 8 | Haute | Données de chat/utilisateurs hors plateforme : artefacts Bitbucket du pipeline d'export, jetons de partage copiés en Delta, DEV chargé depuis des instantanés UAT. | **Décision.** Aucune modification de code ici (étapes côté Databricks/Bitbucket). |
| 9 | Haute | PROD déployable depuis n'importe quelle branche, aucun test en CI, CLI installée par script non épinglé. | **Partiel.** Étape de tests avant les déploiements et garde de branche sur PROD dans `bitbucket-pipelines.yml`, phrase du README corrigée. Épinglage de la CLI et OAuth/OIDC : **Décision**. |
| 10 | Haute | Si Lakebase est down au démarrage, plus aucune reconnexion et `/health` reste vert. | **Fait.** Boucle de reconnexion avec attente croissante, état Lakebase dans `/health`. Test : `tests/test_lakebase_connection.py`. |
| 11 | Moyenne | Boucle d'événements bloquée (preview synchrone, `get_credentials` crée un `Config()` à chaque tour, grosse regex des REF, sémaphore libéré à la déconnexion, `httpx.AsyncClient` par appel) ; cache PDF jamais nettoyé. | **Partiel.** Preview via `asyncio.to_thread`, configuration SDK mise en cache, cache PDF borné à 200 fichiers. Reste **Ouvert** : regex des REF, sémaphore compare, client HTTP partagé. |
| 12 | Moyenne | Pas de rétention ni d'effacement par utilisateur (RGPD) : textes complets dans `llm_requests`, passages dans `chat_retrieved_chunks`, `localStorage`. | **Décision.** |
| 13 | Moyenne | Feedback non lié au votant ; la vue partagée montre les commentaires privés ; `get_session` ne filtre pas `deleted`. | **Fait.** Vote accepté seulement sur un message de l'utilisateur, vue partagée = vote du propriétaire seul, sessions supprimées exclues. |
| 14 | Moyenne | WebSocket : contrôle d'accès fail-open, pas de contrôle d'Origin, entrée non validée (rôle `system`, taille, message sans contenu). | **Fait.** Échec = refus, liste d'Origin optionnelle (`ALLOWED_ORIGINS`), rôles et taille validés (`_clean_messages`). L'Origin n'est vérifiée que si la liste est renseignée. |
| 15 | Moyenne | Tout utilisateur du chat peut atteindre tout document indexé (compte de service) ; les puces du catalogue ignorent `in_chat` et la division. | **Décision** (à documenter comme contrôle accepté). |
| 16 | Moyenne | Recherche d'impact : sections coupées à 2000 caractères, runs partiels mis en cache, citation inventée = quand même « impacté », auto-exclusion par REF seulement. | **Ouvert.** |
| 17 | Moyenne | Gros documents (découpés) : lignes « aucun changement » entre les vrais changements, titre collé en fin de ligne, titres perdus, Excel jamais découpé. | **Ouvert.** |
| 18 | Moyenne | Clés de cache sans prompt, modèle ni réglages. | **Fait** pour l'analyse et le résumé (`_analysis_cache_version`, `_summary_cache_version`, comme `_impact_cache_version`). |
| 19 | Moyenne | Tours en échec renvoyés au modèle comme historique ; fenêtre de N−1 messages ; « Stop » n'arrête pas le serveur. | **Partiel.** Nettoyage côté serveur (`_clean_messages`). Côté navigateur (ne plus renvoyer les bulles d'erreur) : **Ouvert**. |
| 20 | Moyenne | Alertes qui ne peuvent pas se déclencher (copie Delta rafraîchie 2 fois par jour), pas de job d'export en PROD, tableaux DEV figés. | **Ouvert** (ressources Databricks). |
| 21 | Moyenne | Robustesse et coût du pipeline : `redescribe_all_images`, prune qui échoue en silence, attente de l'index qui passe au premier sondage, pas de délai maximal, descriptions d'images non mises en cache, PNG étiqueté jpeg, PROD en pause. | **Ouvert.** |
| 22 | Moyenne | `dry_run=false` depuis DEV/UAT supprimerait des dossiers de la zone PROD partagée. | **Fait.** Suppression des dossiers autorisée seulement quand l'environnement d'écriture est `prod`. |
| 23 | Moyenne | Température/« thinking » Claude morts ou invalides (400 silencieux, repli sur Luna). | **Fait.** Pas de température quand le raisonnement est actif ; test corrigé. |
| 24 | Moyenne | Courses côté client (historique A puis B, résumé du fichier précédent, `running` bloqué, pas de watchdog du chat, suppression sans erreur, `/api/me` en échec). | **Ouvert** (client, besoin de reconstruire `client/out`). |
| 25 | Moyenne | Config et docs incohérentes : modèle de réécriture Gemini Flash Lite alors que trois endroits disent « Luna seulement », `CHAT_TRANSLATE_ONLY` documenté « off », fichiers cités absents, README CI faux, README du moteur vs `base.md`, repli « rewrite failed » mort, volumes `uat_landingzone`, pas de `prevent_destroy`, test `uat-test`. | **Décision** (modèle de réécriture) ; README CI corrigé. Le reste est **Ouvert**. |
| 26 | Moyenne | `leap_utils` copié : 78 % jamais atteint. | **Décision.** |
| 27 | Moyenne | Un `upsert` d'utilisateur en échec perd la sauvegarde du chat ; deux messages d'un tour hors transaction ; index manquants ; pas de verrou de migration. | **Fait.** `upsert` non fatal (`user_upsert_failed`), tour écrit en une transaction, index sur `compare_messages(user_id, created_at)`, `chat_sessions(user_id, updated_at)`, `chat_feedbacks(message_id)`, verrou consultatif pour la migration. Golden de schéma régénéré. |
| 28 | Moyenne | Dépendances non reproductibles (`requirements.txt` en `>=` ≠ `uv.lock`), bibliothèques de jobs non épinglées, `.deb` antiword en `http://` sans somme de contrôle, `@playwright/mcp@latest`. | **Décision.** |
| 29 | Moyenne | Conversions Office (soulevées par Yann) : aperçu PPTX = diapositive 1 seulement, `.doc` coupé à 400 fragments, encodage antiword, course sur la détection LibreOffice. | **Fait** aux lots 1 et 2 (voir les lignes 43–48 du tableau de Yann). |

### Constats de gravité basse

| Constat | Traitement |
|---|---|
| `str(exc)` renvoyé au navigateur (chemins de volume, erreurs SQL). | **Fait.** Réponse générique (`INTERNAL_ERROR`) sur exports, historique, config, sessions, volume, feedback, impact, aperçu ; le détail va dans les logs. |
| Exfiltration par injection de prompt (images Markdown vers un hôte quelconque, pas de CSP, liens non limités à https). | **Ouvert** (client). |
| Injection de formule à l'export Excel (texte commençant par `=`). | **Fait.** Texte stocké comme texte (`_store_text_as_text`). |
| Coûts falsifiables : `cost_eur` et jetons envoyés par le client à `/api/history`. | **Ouvert.** |
| `.ppt` annoncé mais illisible par python-pptx ; grandes images non redimensionnées. | **Ouvert.** |
| Images au-delà de 95 supprimées en silence, étiquettes restées dans le prompt. | **Ouvert.** |
| Trous d'extraction : en-têtes/pieds DOCX, notes PPTX ; ligne courte ajoutée identique à une voisine masquée. | **Ouvert.** |
| Tâches `create_task` sans référence (`store_error`, votes, uploads). | **Ouvert** (`store_error` doit rester synchrone d'après `CLAUDE.md`). |
| Rafraîchissement du jeton Lakebase bloquable par `old_pool.close()`. | **Ouvert.** |
| `_caps_cache` sans éviction, pas de limitation de débit par utilisateur. | **Ouvert.** |
| Historique anonyme partagé (`user_id = ''`) sans en-tête d'identité. | **Ouvert** (non atteignable derrière le proxy). |
| Doublons `document_change_log`, clones `_pipeline_checkpoint_<ts>` jamais supprimés. | **Ouvert.** |
| `client/out` versionné, exclu du scan de secrets. | **Décision.** |
| Bundle JS de 1,14 Mo, `pdfjs-dist` inutilisé, favicon de 1 Mo, polices Google chargées à l'exécution. | **Ouvert.** |
| Notices de licence manquantes (antiword GPL-2, modèle fastText CC-BY-SA). | **Ouvert.** |
| Identités individuelles dans `databricks.yml` et les jobs. | **Ouvert.** |
| Chemins de sécurité sans test. | **Fait** pour la propriété des sessions (`load`/`save-*`), la réutilisation de `session_id` au WebSocket, l'aperçu protégé, la reconnexion Lakebase (`tests/test_chat.py`, `test_compare_audit.py`, `test_lakebase_connection.py`). Le code client reste sans test. |

### Ce que l'agent a vérifié et trouvé sain

SQL entièrement paramétré ; contrôle d'accès fail-closed, échecs non mis en cache, `CAPS_BYPASS` limité à DEV (config et test) ;
filtres de propriétaire sur l'historique et les sessions (jetons `token_urlsafe(20)`, la duplication d'une session partagée crée
une session au nom de l'appelant) ; sous-processus sans shell et avec délai ; citations cohérentes du serveur au client, sémaphore
de réponse libéré sur tous les chemins ; pas de `dangerouslySetInnerHTML` ni `rehype-raw`, iframe d'aperçu sans script ;
écritures du pipeline idempotentes, un seul run à la fois, index `TRIGGERED` ; rafraîchissement du jeton Lakebase planifié sur
l'expiration réelle ; aucun secret commité ; hachage serveur des uploads, flux tronqués non mis en cache, pas de XXE dans le XML.

### Les 48 commentaires de Yann, vus par l'autre agent

Verdict de l'agent : 31 d'accord, 9 en partie, 8 en désaccord. Chaque ligne donne son avis, sa raison, et ce qui a été fait ici
dans les lots 1 à 3. Les écarts sont signalés : là où l'agent est en désaccord avec Yann, le code suit la demande de Yann (c'est lui le relecteur) sauf mention.

| # | Fichier:ligne | Commentaire de Yann | Avis de l'agent | Raison de l'agent (résumé) | Notre traitement |
|---|---|---|---|---|---|
| 1 | `.vscode/settings.json:1` | Exclure .vscode ? | En partie | `settings.json` = réglages personnels ; `tasks.json` sert à l'équipe. | `settings.json` sort du suivi, `tasks.json` reste (#885061633). Aligné. |
| 2 | `README.md:84` | Passer à 6 Luna | D'accord | Les cibles mettent Gemini Flash Lite pour la réécriture, contraire à « Luna seulement » (#25). | GPT-6 Luna partout où le code appelait un autre modèle Luna ; la réécriture Gemini reste (**Décision**). |
| 3 | `client/out/index.html:1` | Pourquoi en double ? | En partie | Pas un doublon (source Vite / build) ; le vrai problème est de versionner le build. | Pas de changement de code, explication (#885062343). **Décision** sur le build au déploiement. |
| 4 | `client/public/logos/databricks-symbol-color.svg` | Logos en double ? | En partie | Même cause que 3 ; réduire le favicon de 1 Mo. | Pas de changement (#885062844). Favicon : **Ouvert**. |
| 5 | `language_reminder_named.md:1` | Pourquoi deux ? | Désaccord | Deux cas distincts (langue détectée / non détectée), documenté dans `prompts/README.md`. | Pas de changement de code, explication (#885070557). Aligné. |
| 6 | `language_reminder_named.md:1` | Plutôt dans un `.py` de config | Désaccord | Les `.md` sont enregistrés dans le registre MLflow et éditables sans code. | Non fait (#885071187). Aligné. |
| 7 | `system/as.md:1` | Variabiliser AS/IS | D'accord | Texte répété avec seulement le nom de division changé ; un gabarit `{division}`. | Fait (#885138943). |
| 8 | `system/base.md:2` | Détection de langue en amont plutôt que polluer le prompt | En partie | Elle existe déjà (fastText) ; on peut réduire le bloc de `base.md` à une ligne de repli. | Pas de changement, proposition (#885138148). Réduction du bloc : **Décision** (mesure sur DEV nécessaire). |
| 9 | `chat/helpers.py:13` | Raisonner en jetons plutôt qu'en tours | D'accord | Dix messages peuvent être énormes ; bug réel : fenêtre de N−1 messages, tours en échec qui prennent des places (#19). | Fait (#885145601) ; nettoyage serveur des tours en échec (#19, partiel). |
| 10 | `chat/helpers.py:111` | Pareil (constante en haut) | D'accord | Style. | Fait (#885146344). |
| 11 | `chat/persistence.py:8` | Imports absolus mieux | Désaccord | Les imports relatifs explicites sont corrects selon la PEP 8 ; changement sans gain. | Fait malgré tout, pour tout `server/` (#885147969), la demande de Yann et la règle de `CLAUDE.md` (`from server.…`). **Écart avec l'agent, assumé.** |
| 12 | `chat/helpers.py:36` | Constantes au début | D'accord | Style. | Fait (#885146016). |
| 13 | `chat/sessions.py:13` | Relatif… | Désaccord | Idem 11. | Fait (#885149621). **Écart avec l'agent, assumé.** |
| 14 | `chat/persistence.py:19` | Documenter la fonction | D'accord | C'est la fonction sans contrôle de propriétaire (#5). | Fait (#885148302) et contrôle de propriétaire ajouté (#5). |
| 15 | `chat/persistence.py:47` | Paramètres Sphinx manquants, toute la base | D'accord | Standard d'équipe ; le code a des docstrings en prose. | Fait pour `server/` et `utils/` (#885148691), `check_docstrings.py` = 0 problème. |
| 16 | `chat/persistence.py:30` | Type | D'accord | `pool: asyncpg.Pool`. | Fait (#885148761). |
| 17 | `chat/ws.py:159` | Un seul `if` | Désaccord | Le `if` interne a son propre `else` (flux vide) ; fusionner changerait le comportement. | Fait en gardant le comportement (#885246366) : le cas du flux vide est conservé. Aligné sur le fond. |
| 18 | `chat/ws.py:16` | Renommer à la définition | D'accord | `ENABLED` → `TRANSLATE_BRIDGE_ENABLED`. | Fait (#885245561). |
| 19 | `compare/analyze.py:57` | Un petit commentaire | D'accord | Le sémaphore est relâché à la déconnexion alors que le thread tourne (#11). | Commentaires faits (#885248209). Le sémaphore lui-même : **Ouvert** (#11). |
| 20 | `compare/common.py:86` | Docstrings + paramètres | D'accord | Style. | Fait (#885252966). |
| 21 | `compare/common.py:59` | Défauts dans un fichier de config, à discuter | D'accord | Voir 22. | Fait (#885252414). |
| 22 | `compare/common.py:18` | Fichier de config ! | D'accord | Prompt par défaut différent de celui d'`app.yaml`, `APP_VERSION` à deux valeurs : un seul module de réglages. | Fait : `services/compare/config.py` (#885251487). Prompts de la comparaison dans `server/prompts/` : **Décision**. |
| 23 | `compare/impact.py:72` | Fonction à découper ? | D'accord | 160 lignes ; garde `queries_failed > 0` sur le cache (#16). | Découpée (#885255107). Garde de cache : **Ouvert** (#16). |
| 24 | `compare/common.py:114` | Booléen complexe | D'accord | Lisibilité seulement, logique correcte. | Fait (#885254220). |
| 25 | `compare/analyze.py:123` | Fonction trop longue + types de retour | D'accord | ~92 lignes avec fermetures ; peu de types de retour. | Fait (#885251206). |
| 26 | `exports.py:101` | `Optional[T]` ou `T \| None` | D'accord | Utiliser `T \| None`. | Fait (#885259506). |
| 27 | `feedback.py:18` | Pourquoi `str` ? | D'accord | `Literal['up', 'down']`. | Fait (#885261374). |
| 28 | `history.py:20` | Fichier de config | En partie | Un objet réponse global : une petite fonction qui renvoie une réponse neuve est mieux. | Avait été traité en important `APP_VERSION` (#885262563). Le 2026-10-21, `_UNAVAILABLE` devient la fonction `_unavailable()`. |
| 29 | `feedback.py:26` | Ces contrôles inutiles | D'accord | Pydantic renvoie déjà 422 ; le vrai problème : feedback non lié au votant (#13). | Fait (#885261904) et propriété du message vérifiée (#13). |
| 30 | `engine/config.py:67` | En haut | D'accord | Style. | Fait (#885269686). |
| 31 | `mlflow_turns.py:119` | En haut + config | En partie | En haut oui ; une regex de parsing n'est pas de la config. | Fait (#885271146). Aligné. |
| 32 | `translation_bridge.py:67` | Config ! | Désaccord | Regex de nettoyage, pas des réglages. | Déplacé dans la config du chat (#885272147), la demande de Yann. **Écart avec l'agent, assumé.** |
| 33 | `turn_log.py:33` | Config | Désaccord | Idem : regex de parsing. | Pas de changement ; à discuter (#885272670). Aligné. |
| 34 | `feedback.py:62` | Pareil ! | D'accord | Idem 27. | Fait (#885262081). |
| 35 | `history.py:47` | `dict[K, V]`, mieux un `TypedDict` | D'accord | Basse priorité. | `dict[K, V]` fait, pas de `TypedDict` (#885266896). |
| 36 | `history.py:144` | Une classe pour une variable ? | Désaccord | Un modèle Pydantic est la façon normale de recevoir un corps JSON. | Fait quand même : `SaveImpactRequest` supprimée, la route lit le corps (#885267451). **Écart avec l'agent, assumé.** |
| 37 | `engine/config.py:32` | config = config, pas common | D'accord | Renommer `common.py` de la comparaison. | Compris comme « un module de réglages s'appelle `config` » (#885269518) : `services/compare/config.py` créé. |
| 38 | `engine/queries.py:49` | Config | En partie | Liste de phrases-bruit : constante nommée en haut suffit. | Fait (#885270078). Aligné. |
| 39 | `mlflow_turns.py:287` | Pas agréable à lire | D'accord | Conditionnel dans une f-string. | Fait (#885271431). |
| 40 | `translation_bridge.py:36` | Modèle codé en dur | En partie | Lu dans `CHAT_TRANSLATE_ENDPOINT` avec défaut qui duplique `app.yaml`. | Défauts regroupés dans `services/chat/config.py` (#885271954). |
| 41 | `engine/retrieval.py:52` | Pas très lisible | D'accord | `setdefault` dans une compréhension. | Fait (#885789675). |
| 42 | `engine/search.py:54` | Danger ! Aucun avertissement quand on coupe | En partie | Silencieux, mais impact faible (plafond sous la limite, seule la question brute peut l'atteindre). | Fait : avertissement, incident `query_truncated`, longueur d'origine (#885793573). |
| 43 | `conversion.py:178` | Fonction testée ? | D'accord | Aucun test, et cassée : seule la diapositive 1 (#29). | Fait : tests et conversion via PDF (#885833326). |
| 44 | `conversion.py:200` | Convertit-elle toutes les pages ? | D'accord | Non, LibreOffice n'écrit que la première (#29). | Corrigé (#885833609). |
| 45 | `conversion.py:162` | Pourquoi 400 ? | D'accord | Bug réel : `.doc` long coupé en silence, aussi dans la comparaison sans antiword (#29). | Nommé et signalé (#885834566). |
| 46 | `engine/retrieval.py:57` | `Any` pas terrible, `TypedDict` | D'accord | Basse priorité. | Fait : `engine/types.py` (#885790057). |
| 47 | `conversion.py:94` | Encodage manquant ? | D'accord | Charset de sortie dépendant de la locale, toujours décodé en UTF-8 ; passer `-m UTF-8.txt` (#29). | Fait (#885835950). |
| 48 | `conversion.py:44` | Dangereux en concurrence ? | D'accord | Le drapeau « vérifié » est posé avant la fin de la recherche (#29). | Fait (#885835700). |
| Jules | `export_lakebase_uat_to_volume_job.py:1` | Les jobs sont refaits (plus de « uat » dans les noms) | Confirmé | Confirmé sur `origin/DEV` ; la partie export Bitbucket reste (#8). | Déjà fait avant la revue (#885301989). |

Les 14 commentaires du lot 3 (docstring `Args:`, `.PDF`, `.xlsm`, `soffice.py`, `summarize.py`, `markdown.py`, `vector_search.py`,
`relocation.py`, `excel.py`, `extract.py`, `llm.py`, antiword) sont arrivés après cette revue : l'agent n'en a pas jugé. Ils restent traités
aux entrées du lot 3 plus haut.

### Décisions que j'attends de toi
1. **#8 données hors plateforme** : retirer `staging/**` des artefacts Bitbucket ; ne créer le jeton de partage qu'au clic sur Partager,
   l'exclure des exports et faire tourner les jetons existants (étapes à exécuter par le principal de service, je te donne les commandes) ;
   dire si l'historique de chat peut aller dans les environnements bas.
2. **#12 RGPD** : durée de conservation de `llm_requests.messages_json` et `chat_retrieved_chunks`, et `localStorage`.
3. **`client/out`** : versionné (actuel) ou construit au déploiement (recommandé par l'agent).
4. **Modèle de réécriture** : tranché le 2026-10-21, GPT-6 Luna partout (les surcharges Gemini de `target_env.json` sont retirées).
5. **`base.md`** : réduire le bloc « langue » à une ligne ; à mesurer sur DEV avant.
6. **#9 CI** : épingler la CLI, OAuth/OIDC ; les tests bloquent-ils le déploiement ?
7. **#28 dépendances** et **#26 `leap_utils`** : paquet épinglé ou module par module.
8. **Prompts de la comparaison** en fichiers sous `server/prompts/` ; `:rtype:` en plus de `:return:`.
