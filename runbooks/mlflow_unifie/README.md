# MLflow unifié : traces de l'app + évaluations dans une seule experiment

Objectif : une seule experiment par environnement (`/Shared/Experiment/qualibot_chat_traces`), où chaque onglet est
rempli (Traces, Datasets, Evaluation runs, Prompts, Agent versions), sans que les traces d'évaluation ne noient les
traces des utilisateurs. Chaque version de configuration (retrieval + génération) est une **Agent version** dont la
fiche décrit toute la configuration, réglage par réglage. Un déploiement en PROD qui change la configuration lance
l'évaluation tout seul.

Légende : `[x]` fait et vérifié dans Databricks, `[~]` code poussé mais pas encore vérifié, `[ ]` à faire.

## 0. Déjà fait

- [x] Droits du SP de l'app DEV sur `dev_proj.qualibot` : `USE SCHEMA`, `CREATE FUNCTION`, `EXECUTE` (lien prompts → traces).
- [~] `register_chat_prompts.py` (e8860da) : tag `mlflow.promptRegistryLocation` sur l'experiment, vérification par
  schéma, `EXECUTE` sur le schéma. À vérifier : un run SUCCESS du job + les 9 prompts dans l'onglet Prompts.
- [ ] `resources/chat_mlflow.yml` de ton repo Bitbucket : il lui manque `--warehouse-id` pour `register_chat_prompts`.
- [ ] Une question dans l'app DEV : plus de `prompt versions not linked` dans les logs, prompts visibles sur la trace.

## 1. Une seule experiment, sans pollution

Ce que Databricks permet : les traces d'une évaluation (`mlflow.genai.evaluate`) vont dans la même table de traces
que l'app. On ne peut pas les envoyer ailleurs sans changer d'experiment. Par contre, chaque trace d'évaluation est
rattachée à son run, et on peut la marquer.

- [ ] Tag `qualibot.source` sur **toutes** les traces : `app` (app, `mlflow_turns.py`), `eval_retrieval`, `eval_generation`.
- [ ] Vue enregistrée dans l'onglet Traces : filtre `tags.qualibot.source = 'app'` (traces des utilisateurs seules).
- [ ] Les métriques restent dans Evaluation runs et Agent versions : elles sont sur les runs et les modèles, pas sur les traces.
- [ ] L'ancienne experiment `dev_retrieval_eval` : gardée en lecture seule, plus rien n'y écrit (question Q5).

## 2. Agent versions = une configuration complète

Une Agent version (LoggedModel) par **signature** de configuration. Même configuration = même version, réutilisée.

- [ ] `server/services/chat/spec.py` (nouveau) : `spec()` renvoie **tous** les réglages qui influencent une réponse, un
  par clé, à plat : index, top-k (premier message / suite), recherche brute, fusion, ordre des passages, plafond de
  passages, mots vides, normalisation des codes, traduction seule, modèles (réponse, réécriture, traduction, secours),
  effort de raisonnement, max tokens, versions de prompts (`chat_system_base@v3`…), empreinte du code du moteur.
- [ ] `signature = sha256(spec)` : un seul réglage change, la signature change, nouvelle Agent version.
- [ ] Test `tests/test_chat_spec.py` : chaque variable `CHAT_*` lue par `server/services/chat/` et chaque prompt de
  `prompts.FILES` apparaît dans `spec()`. Un nouveau réglage oublié dans la spec fait échouer le test.
- [ ] Fiche de l'Agent version : chaque clé de la spec en **paramètre** (une ligne par réglage), plus une description
  texte lisible (« Top-k 32/21 · brute coupée · fusion meilleur rang · GPT-6 Luna · prompts base v3… »).
- [ ] Nom lisible au lieu de `deploye` (question Q2), par exemple `qualibot-k32-21-sansbrute-a1b2c3`.
- [ ] Prompts de la version : chargés via `load_prompt(..., link_to_model=True)`, ils apparaissent dans la section Prompts de la fiche.
- [ ] L'app déclare sa version active (`set_active_model`) : les traces de prod sont rattachées à l'Agent version déployée.

## 3. Datasets et Evaluation runs

- [ ] Dataset UC `dev_proj.qualibot.golden_retrieval_eval` (déjà créé par Génie) rempli depuis `eval_golden_retrieval`
  à chaque run du job, versionné (`golden_version`).
- [ ] `retrieval_eval.py` : `mlflow.genai.evaluate(data=<dataset UC>, model_id=<Agent version>)` → le dataset apparaît
  dans « Datasets used » et le run dans Evaluation runs et sur la fiche de la version.
- [ ] Toutes les métriques (`recall_01..50`, `allrefs_10/50/all`, `mrr_*`, `docs`, `chunks`, `tokens_k`) sont
  remplies pour chaque run, sinon le run échoue (fini les colonnes vides).
- [ ] Le script de Génie `retrieval_eval_to_chat_*.py` est fusionné dans `retrieval_eval.py` : pas de deuxième script (Q1).
- [ ] Étape 2, plus tard : évaluation de la génération (juges de `chat_scorers.py` sur le golden set) dans la même Agent version.

## 4. Évaluation automatique au déploiement PROD

- [ ] Le job `Z_4_Qualibot_Retrieval_Eval` calcule la signature de la configuration déployée. Si une Agent version
  avec cette signature a déjà un run d'évaluation, il s'arrête en 1 minute (rien à faire).
- [ ] Le pipeline Bitbucket `deploy-prod` lance ce job en dernière étape avec `databricks jobs run-now` **sans
  attendre** (fire and forget) : le déploiement ne dépend pas du résultat. Le job envoie son mail habituel en cas d'échec.
- [ ] Même chose en DEV et en UAT (`deploy-dev`, `deploy-uat`).

## 5. Nettoyage

- [ ] Supprimer le job de test `Z_TEST_Qualibot_Link_Pr...` et `qualibot_tests/test_link_prompts_to_ex...`.
- [ ] Supprimer le run en échec `retrieval-deploye` et l'Agent version `qualibot-retrieval-deploye-95c74aa9` une fois la nouvelle en place.
- [ ] Vérifier que le cluster LEAP Unity Catalog n'a plus `mlflow 3.14.0` installé à la main (autres utilisateurs).

## Questions ouvertes

- **Q1.** Script de Génie : colle-moi `qualibot_tests/repo_changes/utils/evaluation/retrieval_eval_to_chat_*.py`,
  ou je repars de `retrieval_eval.py` du repo (recommandé) ?
- **Q2.** Nom des Agent versions : `qualibot-k32-21-sansbrute-<6 car. de signature>` (recommandé) ou une date
  (`qualibot-2026-11-05-<sig>`) ?
- **Q3.** La spec inclut aussi la génération (modèles, prompts, max tokens, effort de raisonnement) : oui (recommandé) ?
- **Q4.** Évaluation au déploiement : preset `deploye` seul (une config, ~5 min, recommandé), ou `tailles` ?
- **Q5.** Ancienne experiment `dev_retrieval_eval` : on la garde en lecture seule (recommandé) ou on la supprime ?
- **Q6.** Les traces d'évaluation restent visibles (filtrables par `qualibot.source`) : c'est le seul moyen d'avoir
  le détail question par question dans Evaluation runs. OK ?
