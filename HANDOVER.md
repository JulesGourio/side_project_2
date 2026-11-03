# QualiBOT : passation

Complète le [README](README.md) (qui décrit le code). Ici : ce qu'il faut savoir pour **opérer** l'app. Écrit le 2026-11-03 depuis le code de la branche et la mémoire du projet. Rien n'a été lu sur Databricks : tout ce qui est marqué **à vérifier** vient de la mémoire ou du code, pas de l'état réel.

Liens : base `https://github.com/JulesGourio/side_project_2/blob/claude/sharp-mayer-uqwx8x/`

## 1. Règles à ne jamais enfreindre

1. **Ne jamais perdre le parsing des fichiers de l'index DEV/UAT, ne jamais forcer leur re-parsing.** Jamais de `full`/`rechunk` avec `parse_filter` (voir §6).
2. **Ordre : DEV → UAT, tester, puis UAT → PROD.** Jamais l'inverse.
3. **Rien ne s'écrit dans UAT sans go explicite** (UAT est utilisé tous les jours sauf week-end).
4. **L'index utilisé par l'app ne change pas** (l'app redémarre toute seule). Supprimer un index supprime ses grants : regranter `SELECT` au SP de l'app.
5. **Jamais de `--auto-approve` sur un deploy.** Jamais de PR : commit + push direct sur la branche.
6. **Pas d'accès Databricks depuis Claude.** Le code est poussé d'ici, déployé depuis une autre machine (repo Bitbucket de Jules). Ne jamais supposer qu'un deploy ou un run a eu lieu.
7. **Pas de copie directe de table DEV ↔ UAT** ("on ne peut pas écrire en avant et lire en arrière") : export/import via un volume `staging`. UAT ne peut pas écrire dans PROD ; PROD ne lit pas UAT.
8. **Le Lakebase n'est jamais copié d'un env à l'autre** par défaut (voir §7 pour l'exception faite en PROD le 2026-10-30).
9. Tout ce qui demande des droits (GRANT, DDL catalogue, bind/unbind, deploy, run de job) passe par le **service principal**, pas par le compte personnel.

## 2. Architecture en 1 minute

- **App** : FastAPI (`server/`) + React (`client/`), Databricks App `qualibot`. 3 fonctions : comparaison de documents, recherche d'impact, chat. [README](README.md)
- **Données** : chaîne de parsing quotidienne → Delta + index Vector Search `chunks_index` → lu par le chat et l'impact. [utils/parsing_pipeline/README.md](utils/parsing_pipeline/README.md)
- **Historique** : Lakebase (Postgres), base `doccompare`, projet `qualibot`. Schéma : [schema.md](server/services/infra/lakebase/schema.md)
- **Déploiement** : bundle Databricks `qualibot` ([databricks.yml](databricks.yml), [resources/](resources/)) lancé par pipelines Bitbucket ([bitbucket-pipelines.yml](bitbucket-pipelines.yml)).
- **Source des documents** : Intraqual, `prod_landingzone.intraqual` (lue par tous les envs, jamais modifiée).

| Env | Workspace | Tables | SP des jobs |
|---|---|---|---|
| DEV | `dbc-c623749d-731b` | `dev_proj.qualibot` | `fde6ff28-739f-4a41-b61e-604a298c8478` (job-runner-sa-dev) |
| UAT | `dbc-3a17bfce-9e88` | `uat_proj.qualibot` | `3e5cd4e5-f765-4760-b974-ee7715258b39` (job-runner-sa-uat) |
| PROD | `dbc-77b87d5a-6bcb` | `prod_proj.qualibot` | `b82d3b5a-b633-4738-8dde-68bb2142cb02` (job-runner-sa-prod) |

Mêmes noms de tables, volumes et index dans chaque env. Volumes : `doc_compare`, `images`, `staging`, `docling_models`.
Profils CLI : `DEV`, `UAT`, `PROD`. Groupes : CoreAdmin et CoreDev (CAN_MANAGE), End-users DocCompare / ChatBot (CAN_USE).
Alertes SQL : [resources/alerts.yml](resources/alerts.yml). Email d'échec de tous les jobs : `jules.gourio.external@latecoere.aero`.

## 3. Les jobs

Définitions : [resources/](resources/). Les noms sont identiques dans les 3 envs. Les ids de jobs ne sont pas dans le code.

| Job | Rôle | Planning (Europe/Brussels) | DEV / UAT / PROD (code) |
|---|---|---|---|
| `D_1_Qualibot_Parsing_Pipeline` | Parsing + chunks + index (§4) | `0 0 4 * * ?` | PAUSED / UNPAUSED / UNPAUSED |
| `D_1_Qualibot_Lakebase_Export_To_Volume` | Export Lakebase → volume `staging` + tables Delta | 02:00 et 14:00 | UNPAUSED partout |
| `D_2_Qualibot_User_Capabilities_Sync` | Groupes des utilisateurs depuis `system.access.audit` → table `users` | toutes les 2 h | suit le pause de l'export |
| `D_2_Qualibot_Score_Chat_Traces` | Score les traces MLflow du chat | 02:30 | PAUSED / UNPAUSED / PAUSED |
| `Z_2_Qualibot_Create_Chat_Experiment`, `Z_3_Qualibot_Register_Chat_Prompts` | Setup MLflow (manuel) | aucun | |
| `Z_4_Qualibot_Retrieval_Eval` | Éval retrieval sur le golden set (§8) | manuel | |
| `D_1_Qualibot_Apps_Stop_Nightly_Dev` | Stop de l'app DEV | 21:00 chaque jour | UNPAUSED (DEV seul) |
| `W_1_Qualibot_Stop_Weekend_<Env>` | Stop de l'app | vendredi 21:00 | DEV PAUSED / UAT UNPAUSED / PROD PAUSED |
| `W_1_Qualibot_Start_Weekend_<Env>` (PROD : `Start_Weekday_Prod`) | Start de l'app | lundi 07:00 | idem |

IDs connus (mémoire, **à vérifier**) : Parsing DEV `362367007936662`, UAT `762813135325407`. PROD : inconnu. Retrouver un job par son nom : `databricks jobs list --profile <ENV>`.

Les valeurs "code" ci-dessus sont celles de `databricks.yml` ; l'état réel dans le workspace peut différer (**à vérifier**), surtout le planning PROD : le parsing PROD a été mis en PAUSED le 2026-10-29 puis réactivé dans le code au go-live (commit `47e64cd`).

## 4. Pipeline de parsing

6 tâches enchaînées, une seule exécution à la fois (`max_concurrent_runs: 1` + file). Détail : [README du pipeline](utils/parsing_pipeline/README.md).

`1_categories` → `2_manifest` → `3_parse` → (`3b_parse_pdf` GPU si plus de 30 PDFs) → `3c_chunks` → `4_describe_images` → `5_sync_index` → `6_update_kb_metadata`

**Paramètres du job** (à changer via "Run now with different parameters") :

| Paramètre | Défaut | Effet |
|---|---|---|
| `run_mode` | `incremental` | `full` = tout reparser (checkpoint cloné avant) ; `rechunk` = refaire les chunks depuis le checkpoint |
| `parse_filter` | vide | liste d'IDDOC ; seulement avec `incremental` |
| `max_parse_files` | 500 | plafond par run (DEV : 1000 en défaut du job) ; 0 = illimité |
| `max_source_staleness_hours` | 36 | garde de fraîcheur d'Intraqual ; 0 = coupée |
| `redescribe_all_images` | false | redécrire toutes les images |
| `describe_images` | true | false = aucun appel LLM, descriptions existantes quand même injectées |

Notions clés :
- **Checkpoint** (`parsing_checkpoint`) : un état par document (IDDOC + sha256). C'est le cache de reprise. Il garde l'historique : compter les états sur la dernière ligne par IDDOC.
- **Docling vs XML** : PDF/DOCX/PPTX/HTML passent par Docling ; `xml_fallback` = lecteur DOCX/PPTX de secours quand Docling échoue. Un PDF scanné (<150 caractères) devient `pending_llm_ocr`.
- **Un IDDOC = une révision.** Si plusieurs révisions partagent une REF, seule la plus récente garde ses chunks.
- **Archive** : documents avant 2016-01-01 → `chunks_archive` (alimente l'impact search, jamais le chat).
- **Périmètre** : 3 racines officielles + garde-fou (le job échoue si le périmètre rétrécit de plus de 30 %).
- **Variables par env** : [databricks.yml](databricks.yml) (workers GPU DEV 4 / UAT 4 / PROD 8, index, planning).
- Endpoints LLM vision : `databricks-gpt-6-luna` + repli `databricks-gpt-5-6-luna`, chacun avec son quota (80 % budgété). Embeddings : `databricks-qwen3-embedding-0-6b`.

## 5. Landingzone, volumes, index

- **`prod_landingzone`** : seule la partie `intraqual` est à nous en lecture. Les anciennes tables `prod_landingzone.qualibot` sont obsolètes (à supprimer, pas les volumes) et ont bloqué le deploy PROD le 2026-10-29 (§9).
- **`dev_landingzone`** : lignes obsolètes mais elles servent encore de **cache de reprise** dans le checkpoint DEV/UAT. Ne pas les purger sans vérifier quels documents perdraient leur seule ligne (= re-parsing). Mesure du 2026-10-26 : 5994/7631 lignes DEV et 5993/9950 UAT.
- **Index** : un seul, `<env>_proj.qualibot.chunks_index`, sert chat et impact. `chunks_text_index` : supprimé à la main. `chunks_baseline_index` (DEV) : sert à l'éval seulement.
- **Images** : volume `images`, extraites par `3_parse`, décrites par `4_describe_images`.

## 6. Pièges connus (parsing)

- `run_mode=full`/`rechunk` **écrase `chunks`** : les passages d'images disparaissent jusqu'à la tâche 4, et l'index ré-embedde tout (~100 lignes/s en MERGE, ~8,5/s après un overwrite à 4 fichiers).
- Un run `parse_filter` **supprime quand même** les chunks des anciennes révisions (`2_manifest`) sans parser les nouvelles : les documents disparaissent jusqu'au prochain run incrémental normal.
- Les lignes `ERROR` du checkpoint sont retentées à chaque run incrémental.
- `3c_chunks` : OOM corrigé (lots Arrow de 16 ; PROD via table temporaire `parsing_chunks_temp`).
- Les workers Spark avaient perdu leurs réglages (antiword `binary not found`, tailles de chunks de repli) : corrigé, mais les chunks DEV d'avant ce correctif sont plus gros que ceux de PROD (voir §8).
- Catégorie manquante : une révision sans catégorie hérite de la précédente (corrigé en DEV, commit `ce2e5bd`) ; vérifier que PROD l'a aussi.
- Relancer une seule tâche : `databricks jobs run-now --json` avec `only`, mais `only` reprend les défauts du job ; pour garder les paramètres d'origine, "Repair run" dans l'UI.
- Tailles de chunks : `config.py` = 150/300/450 tokens, 1600 caractères ; ne pas changer sans go.

## 7. Copier des données entre environnements

Toujours par volume `staging`, jamais en direct. Scripts prêts à coller (chemins absolus à adapter à l'env) :
- DEV ↔ UAT (fusion du checkpoint, Docling prioritaire sur XML) : [runbooks/scripts/fusion_dev_uat/README.md](runbooks/scripts/fusion_dev_uat/README.md) et le dossier partagé `uat_sync/notebooks/README_ordre.md`.
- UAT → PROD (checklist cochable) : [runbooks/deploiement_prod/README.md](runbooks/deploiement_prod/README.md).
- **Lakebase UAT → PROD** : fait **une fois**, le 2026-10-30 16:42 (13 tables, 16:09 d'export, sans sauvegarde, voulu par Jules). Procédure : [runbooks/deploiement_prod/](runbooks/deploiement_prod/) (`copie_lakebase_uat_vers_prod.py`, `4_prod_ecrasement_lakebase.py`). L'app n'avait jamais tourné en PROD, donc rien n'a été perdu.
- Les scripts de `runbooks/scripts/*_submit.json` sont d'anciens modèles ; la règle actuelle est : code collé dans une cellule de notebook, paramètres en dur, pas de JSON.

## 8. Éval retrieval (MLflow)

- Job `Z_4_Qualibot_Retrieval_Eval` ([resources/retrieval_eval.yml](resources/retrieval_eval.yml)), notebooks dans [utils/evaluation/](utils/evaluation/). Manuel. Presets `deploye`, `tailles`, `complet`, `perso`. Toujours `max_parallel=16`.
- Table du golden set : `<env>_proj.qualibot.eval_golden_retrieval`, rédigée à la main dans le notebook `create_golden_retrieval_set.py`. Ne proposer une question que si le contexte du terme a été vérifié.
- Expérience MLflow : `/Shared/qualibot/<env>_retrieval_eval`. Clé de cache = libellé de config + signature (pas le contenu de l'index) : après un rechunk, renommer les configs (suffixe) et mettre `rerun_existing=false`.
- Résultats retenus (DEV, 54 puis 61 questions) : **recherche brute coupée** (`CHAT_RAW_TOP_K=0`, `CHAT_PAIR_RAW_TOP_K=0`) gagne (R@5 82,9 contre 73,5) ; RRF et poids BM25 ne servent à rien ; plateau à 94 % dès 12 passages. DEV tourne en 32/21 sans brute ([utils/deploy/target_env.json](utils/deploy/target_env.json)). **UAT et PROD : pas encore portés** (sur ton go).
- **Écart de chunks PROD vs DEV** (2026-11-02) : R@10 DEV 91,4 contre PROD 78,8 à cause de tailles de chunks différentes. Chemin choisi : tous les chunks DEV + chunks PROD des documents absents de DEV ; basculé en PROD le 2026-11-02 12:33 (`chunks` 166 125 → 124 147 lignes). Retour arrière : `RESTORE TABLE prod_proj.qualibot.chunks TO VERSION AS OF 22` puis resync de l'index. Index PROD recréé ensuite ; ré-éval PROD vs 78,8 : **à vérifier**. Garder le planning de parsing PROD en pause tant que la tâche 4 réinjecterait d'anciennes images PROD (**à vérifier**).
- Cause d'échec connue : les codes (`LN1337`) sont noyés par les mots de la question (hybride RRF). Piste non faite : requête "code seul" derrière `CHAT_CODE_QUERY`.

## 9. Déploiement

- Pipelines Bitbucket **manuels** (rien ne part sur push) : `deploy-<env>` (bundle + app) et `deploy-<env>-jobs` (bundle sans l'app). UAT et PROD lancent les tests d'abord ; PROD refuse toute branche autre que `main`.
- Réparation d'état du bundle PROD (bloqué le 2026-10-29, pipeline #195, état qui suivait encore `prod_landingzone.qualibot`) : pipelines `prod-state-check` (lecture seule) puis `prod-state-fix` ([utils/deploy/prod_state_fix.sh](utils/deploy/prod_state_fix.sh)). Les schéma/volumes PROD ont `prevent_destroy`. Checklist : [runbooks/deploiement_prod/README.md](runbooks/deploiement_prod/README.md). Statut réel du go-live PROD : **à vérifier**.
- Pipelines ponctuels : `migrate-*-state`, `plan-dev|uat`, `uat-state-fix`, `bind-uat-lakebase`, `bind-uat-staging`, `unbind-dev-test-volume`, `sync-prod-data` (export UAT puis import PROD).
- Config de l'app par env : [utils/deploy/target_env.json](utils/deploy/target_env.json) > [app.yaml](app.yaml) > défauts du code.
- Le dépôt de déploiement réel est le Bitbucket de Jules, pas ce GitHub. Ce GitHub sert de copie de travail.

## 10. Accès et sécurité

- Chat ouvert à tout visiteur de l'app (le `CAN_USE` de l'app est le filtre) ; comparaison réservée aux groupes DocCompare/CoreDev/CoreAdmin. `CAPS_BYPASS` autorisé en DEV seulement. Détail et raisons : README, sections "Startup and access" et "Design decisions".
- Le job de capacités lit `system.access.audit` ; un retrait de groupe met jusqu'à ~2 h à arriver.
- L'app se connecte au Lakebase avec son propre SP (rôle `app-doc-compare-sp`, id en dur dans `databricks.yml` : le changer seulement si l'app est recréée).

## 11. Où est quoi

| Besoin | Fichier |
|---|---|
| Consignes de travail pour Claude | [CLAUDE.md](CLAUDE.md) |
| Opérations DEV / UAT / PROD | [runbooks/dev.md](runbooks/dev.md), [uat.md](runbooks/uat.md), [prod.md](runbooks/prod.md) |
| Quoi re-tester | [runbooks/retest.md](runbooks/retest.md) |
| Revue de PR et conventions | [runbooks/pr_review/README.md](runbooks/pr_review/README.md) |
| Scripts ponctuels (Lakebase, grants, nettoyages) | [runbooks/README.md](runbooks/README.md) |
| Retrieval (codes, rechunk, éval) | [runbooks/retrieval/README.md](runbooks/retrieval/README.md) |
| Carte de `utils/` | [utils/README.md](utils/README.md) |
| Lakebase export/import/migrations | [utils/lakebase_sync/README.md](utils/lakebase_sync/README.md) |

Dossier partagé du projet (hors repo, **à vérifier** s'il est conservé) : `uat_sync/` (notebooks de fusion et diagnostic), `retrieval_eval/` (aperçus de graphiques), `pr_review/`.

## 12. Ouvert à la date d'écriture

- Épingler les versions de la bibliothèque du cluster de parsing (remarque 28 de la revue).
- Porter `CHAT_RAW_TOP_K=0` / `CHAT_PAIR_RAW_TOP_K=0` et les tailles 32/21 en UAT et PROD (go de Jules requis).
- Option "full en MERGE au lieu d'overwrite" : proposée, non validée. Corriger le prune de `2_manifest` sous `parse_filter` : idem.
- Nettoyage de `dev_landingzone` : pas prioritaire.
- Tâches de la liste "à vérifier" ci-dessus (ids de jobs PROD, planning réel, ré-éval PROD).
