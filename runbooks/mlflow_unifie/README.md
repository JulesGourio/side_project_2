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

Les traces d'évaluation vont forcément dans la même table que celles de l'app (même experiment). Databricks ne permet
pas de les cacher par défaut dans l'onglet Traces à ma connaissance : on filtre.

- [~] Tag `qualibot.source` sur chaque trace : `app` pour l'app, `evaluation` pour une évaluation (`mlflow_turns.py`, à déployer).
- [~] Colonne User des traces : l'identifiant réel de la personne (son e-mail) pour l'app, `evaluation` sinon (à déployer).
  Avant, l'identifiant était haché.
- [ ] Vue enregistrée dans l'onglet Traces : filtre `qualibot.source = app` ; une deuxième vue `evaluation`.
- [ ] Ancienne experiment `dev_retrieval_eval` : laissée de côté (deux experiments seulement si la vue unique ne convient pas).

## 2. Agent versions = une configuration complète

- [~] `server/services/chat/spec.py` : `spec()` = chaque réglage qui peut changer une réponse, une clé par réglage
  (retrieval, réponse, réécriture, traduction, chaque prompt, empreinte du code). `signature()`, `version_name()`,
  `describe()` (une ligne en français).
- [~] `tests/test_chat_spec.py` (28 tests, verts) : toute variable `CHAT_*` lue par le serveur est dans la spec ou
  déclarée opérationnelle (timeouts, accès, logs) ; changer une variable change sa clé et la signature.
- [ ] Nom : `qualibot-<env>-k32-21-sansbrute-<sig6>` pour la version déployée d'un environnement,
  `qualibot-k16-12-sansbrute-<sig6>` pour une config d'évaluation. Toujours la spec complète en paramètres de la fiche.
- [ ] Fiche : chaque clé de la spec en paramètre, `describe()` en description, prompts liés (section Prompts).
- [ ] L'app déclare sa version active au démarrage (`set_active_model`, nom ci-dessus) : ses traces sont rattachées à la version déployée.

## 3. Datasets, Evaluation runs et traces d'évaluation lisibles

- [ ] Plus de traces « replay » : l'évaluation rejoue la vraie recherche (`retrieve_for_turn`) et écrit la trace avec
  le **même code que l'app** (`mlflow_turns`) : mêmes étapes (traduction, recherches, rerank, passages envoyés),
  mêmes tags, coût compris, `qualibot.source = evaluation`.
- [ ] Le script de Génie (`retrieval_eval_to_chat_experiment.py`) est fusionné dans `retrieval_eval.py` : dataset
  `golden_retrieval_eval`, Agent version par config, `mlflow.genai.evaluate(data=dataset, model_id=...)`.
- [ ] Toutes les métriques remplies (`recall_*`, `allrefs_*`, `mrr_*`, `docs`, `chunks`, `tokens_k`, `cost_eur`), sinon le run échoue.
- [ ] Prix : `cost_eur` en tag sur chaque trace (déjà le cas pour l'app) et en métrique des Evaluation runs. À vérifier
  si Databricks affiche une colonne coût native (sinon le tag reste la source).
- [ ] Plus tard : évaluation de la génération (juges de `chat_scorers.py`) dans la même Agent version.

## 4. Évaluation automatique au déploiement

- [ ] Le job `Z_4_Qualibot_Retrieval_Eval` reçoit `preset=deploye` et calcule la spec déployée ; si son Agent
  version a déjà un run d'évaluation, il s'arrête tout de suite.
- [ ] Les pipelines `deploy-dev`, `deploy-uat`, `deploy-prod` le lancent en dernière étape avec `databricks jobs run-now`
  sans attendre (le déploiement ne dépend pas du résultat ; mail en cas d'échec).

## 5. Nettoyage

- [ ] Supprimer le job de test `Z_TEST_Qualibot_Link_Pr...` et `qualibot_tests/test_link_prompts_to_ex...`.
- [ ] Supprimer le run en échec `retrieval-deploye` et l'Agent version `qualibot-retrieval-deploye-95c74aa9` une fois la nouvelle en place.
- [ ] Vérifier que le cluster LEAP Unity Catalog n'a plus `mlflow 3.14.0` installé à la main (autres utilisateurs).

## Réponses de Jules (2026-11-05)

Q1 : prendre le meilleur des deux scripts. Q2 : nom lisible pour la version déployée, specs consultables ailleurs.
Q3 : génération dans la spec, oui. Q4 : seulement la nouvelle version déployée. Q5 : de côté. Q6 : OK, idéalement cachées par défaut.
