# Journal de revue — PR Bitbucket 2 (`latec-compare`)

Un bloc par commentaire (id Bitbucket), dans l'ordre de l'export. Les nouveaux commentaires s'ajoutent en bas, dans un
nouveau lot. Récupération : `python debug\fetch_comment_pr.py` (copie de `runbooks/scripts/bitbucket_pr/fetch_pr_comments.py`).

- PR faite sur le commit `14296499a19baf710dc6c10d74bdd71204176b2d`. Les lignes citées sont celles de ce commit ; les fichiers
  concernés n'ont pas changé depuis, sauf mention contraire, mais plusieurs numéros de ligne ne tombent pas pile sur le code
  visé : chaque commentaire a été rattaché au code par son sens.
- Relecteur : Yann Depledt. Jules Gourio a laissé un commentaire à lui-même (#885301989).
- Vérification de chaque lot : `pytest tests` (résultat en bas du lot), `utils/code_quality/check_docstrings.py server/routers`,
  et un essai des routes touchées avec le client de test FastAPI.

## Lot 1 — commentaires exportés le 2026-10-19 (41 commentaires)

### Fichiers de dépôt

**#885061633 — `.vscode/settings.json` L1 — « Exclure le .vscode non ? »** — Fait en partie.
`settings.json` (marqueurs de cellules Jupyter, réglage personnel) sort du suivi git et entre dans `.gitignore`.
`.vscode/tasks.json` reste versionné : c'est la tâche « Pull + Deploy » que le README documente.

**#885059913 — `README.md` L84 — « Switch to 6 Luna »** — Fait pour le chat, pas ailleurs.
Le pont de traduction du chat tournait encore sur GPT-5.6 Luna : le défaut du code, `app.yaml` et la ligne du README passent à
`databricks-gpt-6-luna` avec repli `databricks-gpt-5-6-luna` (les deux modèles du chat se sauvent l'un l'autre). Les autres lignes
sur GPT-5.6 Luna (jugement et résumé de la comparaison, description d'images du parsing) ne sont pas touchées : les changer
demande une mesure que je n'ai pas. **Question** : veux-tu aussi les passer à GPT-6 Luna ?

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

**#885148691 — `persistence.py` L47 — « missing sphynx params ! A check dans toute la base de code »** — Fait pour `server/routers/`,
reste à faire ailleurs.
Docstrings Sphinx (`:param`, `:return:`) pour `_save_turn` et toutes les fonctions de `server/routers/` (chat, compare, exports,
feedback, history, preview, config, health). Le script `utils/code_quality/check_docstrings.py` liste ce qui manque : 0 dans
`server/routers/`, environ 460 dans `server/services/`. **Décision à prendre** : j'ai limité le lot aux routes ; le reste (une
passe de plusieurs centaines de fonctions) est à planifier. Attention : `CLAUDE.md` demande des commentaires courts et pas de
paraphrase du code ; des docstrings Sphinx complètes vont plus loin que cette règle, à trancher avec le relecteur.

**#885148761 — `persistence.py` L30 — « type »** — Fait. `pool: asyncpg.Pool`, `conn: asyncpg.Connection`, `sources: List[dict] | None`
(`persistence.py` et `sessions._turn_trace_id`).

**#885246366 — `server/routers/chat/ws.py` L159 — « 1 seul if »** — Fait en partie.
Le `if … pass / else …` qui supprime les morceaux de texte pendant la traduction devient un seul `if not (…)`. Le test
`if user_content and accumulated … else …` à la fin du flux garde deux niveaux : ses deux branches sont différentes (réponse
sauvegardée / flux vide traité comme une erreur). Si le commentaire visait autre chose, dis-moi lequel.

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

1. Faut-il passer aussi la comparaison (jugement, résumé) et le parsing (images) sur GPT-6 Luna ? (#885059913)
2. Le retrait des règles de langue de `base.md` demande une mesure sur DEV (#885138148).
3. Docstrings Sphinx sur les ~460 fonctions de `server/services/` : à planifier, et à accorder avec `CLAUDE.md` (#885148691).
4. `turn_log.py` : garder les niveaux de gravité dans le module du journal ? (#885272670)
