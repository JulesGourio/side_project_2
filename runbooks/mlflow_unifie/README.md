# MLflow unifié : traces de l'app et évaluations dans une seule experiment

Une experiment par environnement, `/Shared/Experiment/qualibot_chat_traces`. Chaque onglet est rempli, chaque
configuration est une **Agent version** décrite réglage par réglage, et un déploiement lance tout seul l'évaluation
de la version déployée.

Légende : `[x]` vérifié dans Databricks, `[~]` codé et poussé, pas encore vérifié, `[ ]` à faire.

## Ce que tu verras dans MLflow

| Onglet | Contenu |
|---|---|
| **Traces** | Une trace par question de l'app. Colonne **User** = e-mail de la personne. Tag `qualibot.source = app`. Les traces d'éval ont exactement les mêmes étapes (traduction ou réécriture, requêtes Vector Search, passages envoyés), User `evaluation`, tag `qualibot.source = evaluation` + `qualibot.eval_config`, `qualibot.case_id`. Session `golden-<exécution>-<config>`. Coût en $ sur chaque appel LLM (`mlflow.llm.cost`), en € dans le tag `cost_eur`. |
| **Datasets** | `<env>_proj.qualibot.golden_retrieval_eval` : les questions golden éligibles, avec leurs documents attendus. |
| **Evaluation runs** | Un run `golden · <nom de version>` par configuration mesurée. Scores `recall_01…50/all`, `allrefs_10/50/all`, `mrr_*`, `docs`, `chunks`, plus `tokens_k`, `cost_eur`, `recall_by_k`. Une ligne par question = la vraie trace. Avec plusieurs configs, un run `golden · comparaison <date>` contient le classement et l'effet de chaque réglage. |
| **Prompts** | Les 9 prompts `chat_*` du schéma (tag d'experiment `mlflow.promptRegistryLocation`). |
| **Agent versions** | Une version par **spec** : `qualibot-k32-21-a1b2c3` (top-k 1er message-suite, `-brute` seulement si la recherche brute est active, 6 caractères de signature). La fiche contient la spec complète en **paramètres**, un par réglage (`retrieval.*`, `answer.*`, `rewrite.*`, `translate.*`, `prompt.*` = hash du fichier, `code.engine` = empreinte du code). La description est la configuration complète en anglais : un titre par section (Retrieval, Answer, Rewrite, Translation, Prompts, Code) et une ligne par réglage (`clé = valeur : ce que c'est`), vérifiée par un test. Elle contient aussi les prompts liés, les runs d'éval, les métriques et les traces. Tags : `qualibot.deployed.<env>` sur la version que l'app fait tourner, `qualibot.evaluated.retrieval` = golden set mesuré. |

Le moindre réglage changé (top-k, modèle, prompt, max tokens, code du moteur) donne une autre signature, donc une
autre Agent version. Quand une config d'évaluation a la même spec que l'app (`deploye`), c'est **la même** Agent
version : ses traces de prod et ses runs d'éval sont au même endroit.

## Ce qui a été codé

- [~] `server/services/chat/spec.py` : `spec()`, `signature()`, `version_name()`, `describe()`.
- [~] `server/services/chat/agent_version.py` : une Agent version par spec (création, réutilisation, prompts liés si leur hash correspond, tag de déploiement).
- [~] `server/services/chat/mlflow_turns.py` : tag `qualibot.source`, User réel, `mlflow.modelId` (version de l'app), coût `mlflow.llm.cost` sur les spans LLM, tags et version passés par une évaluation.
- [~] `utils/evaluation/retrieval_eval.py` : chaque question cherchée écrit sa trace avec le code de l'app. Une Agent version par config, `evaluate` sur ces traces (fini les traces « replay »), dataset golden, `cost_eur`, `only_if_new`.
- [~] `resources/retrieval_eval.yml` : experiment de l'app par défaut, `warehouse_id`, `only_if_new`, `max_parallel` 16.
- [~] `utils/deploy/start_retrieval_eval.sh` + `bitbucket_deploy.sh` : après `deploy-dev`, `deploy-uat` et `deploy-prod` (mode app), le job est lancé sans attendre, avec `preset=deploye` et `only_if_new=true`. Il s'arrête tout de suite si la version est déjà mesurée. Un échec ne casse jamais le déploiement.
- [~] `utils/evaluation/register_chat_prompts.py` : onglet Prompts, droits du SP de l'app.
- [~] Tests : `tests/test_chat_spec.py` (une variable `CHAT_*` oubliée fait échouer), `tests/test_agent_version.py`, `tests/test_mlflow_turns.py`.

Fichiers à recopier dans Bitbucket : tous ceux ci-dessus + `resources/chat_mlflow.yml` (paramètre `--warehouse-id` de
`register_chat_prompts`) + les 3 tests.

## Vérifications, dans l'ordre (DEV)

- [ ] 1. Pipeline `deploy-dev`. À la fin du log : `Retrieval evaluation started (job …), not awaited`.
- [ ] 2. `databricks bundle run register_chat_prompts_dev -t dev --profile DEV` : SUCCESS, `Experiment … linked to dev_proj.qualibot: 9 prompts`, `Grants to app qualibot done`.
- [ ] 3. Onglet **Prompts** : les 9 `chat_*`.
- [ ] 4. Une question dans l'app DEV, puis onglet **Traces** : ta ligne a ton e-mail en User, le tag `qualibot.source = app` et un coût. Aucun `prompt versions not linked` ni `agent version not attached` dans les logs de l'app.
- [ ] 5. Onglet **Agent versions** : `qualibot-k…-…-…` avec le tag `qualibot.deployed.dev` et les paramètres de la spec. Ta trace de l'étape 4 apparaît dans sa liste de traces. Section Prompts remplie.
- [ ] 6. Run du job `Z_4_Qualibot_Retrieval_Eval` lancé à l'étape 1. Dans le log : `Agent versions: {…}`, puis `MLflow run=… | agent version=… | traces=61`. Onglets **Evaluation runs** (run `golden · qualibot-…`, toutes les colonnes remplies), **Datasets** (`golden_retrieval_eval`) et **Traces** (filtre `qualibot.source = evaluation` : mêmes étapes que l'app).
- [ ] 7. Même Agent version qu'à l'étape 5, maintenant avec le run d'éval et le tag `qualibot.evaluated.retrieval`.
- [ ] 8. Relancer `deploy-dev` sans changement. Le job s'arrête en environ 1 min avec `already evaluated`.
- [ ] 9. Onglet **Traces** : enregistrer deux vues, `qualibot.source = app` (par défaut) et `qualibot.source = evaluation`.
- [ ] 10. Nettoyage de l'essai Génie : Agent version `qualibot-retrieval-deploye-95c74aa9`, runs `retrieval-deploye` et traces « replay », job `Z_TEST_Qualibot_Link_Pr…` et `qualibot_tests/`. Vérifier que `mlflow 3.14.0` n'est plus installé à la main sur le cluster LEAP.
- [ ] 11. Puis UAT, puis PROD (`deploy-uat`, `deploy-prod`) : mêmes vérifications 1 à 8.

## Points que je ne peux pas garantir sans Databricks (à demander à Génie si un contrôle échoue)

1. Cacher les traces d'éval par défaut : à ma connaissance impossible. On utilise une vue filtrée (étape 9).
2. Le lien trace → Agent version passe par la métadonnée `mlflow.modelId` posée sur la trace (écrite avec `start_span_no_context`). À confirmer à l'étape 5.
3. La description d'une Agent version passe par le tag `mlflow.note.content`. Si la case Description reste vide, la spec reste lisible dans les paramètres.
4. « Datasets used » de l'Agent version : le dataset golden est lié au run et à la version par `log_inputs`. `evaluate` y ajoute aussi un dataset nommé `dataset`, celui des traces.
5. Colonne coût de l'onglet Traces : MLflow additionne `mlflow.llm.cost` des spans. Si Databricks n'affiche rien, le tag `cost_eur` reste filtrable.
6. Liens prompts → Agent version : `link_prompt_version_to_model` sur les prompts UC. Un échec n'est qu'un avertissement `prompt … not linked`.

## Plus tard

- [ ] Évaluation de la génération (juges de `chat_scorers.py`) dans les mêmes Agent versions.
- [ ] Deux experiments si la vue unique ne convient pas (décision de Jules, de côté pour l'instant).
