"""Registers the chatbot's prompt files in the MLflow prompt registry (Unity Catalog).

Each file of ``server/prompts/chat/`` becomes the prompt ``<schema>.chat_<key>``: a new version only when
its text changed, alias ``deployed`` moved to the version whose ``sha`` tag matches the file. The version is
also written on the chat experiment as the tag ``chat_prompt.<key>`` = {name, version, sha}: the app reads
only that tag (it has no access to the registry) to link the version to every trace of a turn that used the
prompt (``server/services/chat/mlflow_turns.py``), and ignores a prompt whose hash differs from its file.
The experiment must exist (``create_chat_experiment``). Runs as the job ``register_chat_prompts_<target>``
(``resources/chat_mlflow.yml``).

USAGE
-----
    python utils/evaluation/register_chat_prompts.py --profile DEV --schema dev_proj.qualibot --experiment <chat_mlflow_experiment of databricks.yml>

Inside a job the script runs through ``exec`` (no ``__file__``), so the job passes ``--app-root``.

The identity running it needs USE CATALOG, USE SCHEMA and CREATE FUNCTION on the schema. With ``--warehouse-id`` it also
gives the app's service principal USE CATALOG, USE SCHEMA, CREATE FUNCTION on the schema (Unity Catalog refuses the
trace link with "Permission denied to create prompt in schema" without it) and EXECUTE on each prompt: the app links the prompt versions to its traces in the registry.
"""
import argparse
import json
import logging
import sys
from pathlib import Path

import mlflow

logger = logging.getLogger('register_chat_prompts')
EXPERIMENT_IDS_TAG = '_mlflow_experiment_ids'


def _link_to_experiment(name: str, experiment_id: str) -> None:
    """Lists the prompt in the experiment's Prompts tab. MLflow sets this tag asynchronously on registration, and not
    at all for an unchanged prompt, so it is set here, synchronously.

    :param name: Full name of the prompt in the registry.
    :param experiment_id: Id of the chat experiment.
    """
    tags = mlflow.genai.get_prompt_tags(name)
    tags = getattr(tags, 'tags', tags) or {}                           # a dict or a Prompt, depending on the MLflow version
    ids = [i for i in (tags.get(EXPERIMENT_IDS_TAG, '') or '').strip(',').split(',') if i]
    if experiment_id not in ids:
        if EXPERIMENT_IDS_TAG in tags:                                 # a local SQL store INSERTs instead of upserting
            mlflow.genai.delete_prompt_tag(name, EXPERIMENT_IDS_TAG)
        mlflow.genai.set_prompt_tag(name, EXPERIMENT_IDS_TAG, ',' + ','.join(ids + [experiment_id]) + ',')


def grant_app(names: list[str], app: str, warehouse_id: str, profile: str) -> None:
    """Gives the app's service principal the schema privileges the trace link needs, and EXECUTE on each prompt.

    :param names: Full names of the prompts, ``catalog.schema.chat_<key>``.
    :param app: Name of the Databricks app that writes the chat traces.
    :param warehouse_id: SQL warehouse that runs the GRANT statements.
    :param profile: Databricks CLI profile; empty inside a job.
    """
    from databricks.sdk import WorkspaceClient
    w = WorkspaceClient(profile=profile or None)
    principal = w.apps.get(app).service_principal_client_id
    errors = []
    catalog, schema = names[0].split('.')[:2]
    for statement in (f'GRANT USE CATALOG ON CATALOG `{catalog}` TO `{principal}`',
                      f'GRANT USE SCHEMA, CREATE FUNCTION ON SCHEMA `{catalog}`.`{schema}` TO `{principal}`'):
        result = w.statement_execution.execute_statement(statement=statement, warehouse_id=warehouse_id, wait_timeout='30s')
        if result.status.state.value == 'SUCCEEDED':
            logger.info('%s: done', statement)
        else:
            errors.append(f'{statement}: {result.status.error.message if result.status.error else result.status}')
    for name in names:
        full_name = '.'.join(f'`{part}`' for part in name.split('.'))
        result = w.statement_execution.execute_statement(
            statement=f'GRANT EXECUTE ON FUNCTION {full_name} TO `{principal}`', warehouse_id=warehouse_id, wait_timeout='30s')
        if result.status.state.value == 'SUCCEEDED':
            logger.info('EXECUTE on %s granted to app %s (%s)', name, app, principal)
        else:
            errors.append(f'{name}: {result.status.error.message if result.status.error else result.status}')
    if errors:
        raise RuntimeError('EXECUTE not granted to the app on: ' + '; '.join(errors))


def register(schema: str, experiment: str) -> list[str]:
    """Register every chat prompt under ``schema`` and tag the experiment with each version.

    :param schema: Catalog and schema of the prompt registry, ``catalog.schema``.
    :param experiment: Workspace path of the chat experiment.
    :return: full names of the registered prompts.
    """
    from server.services.chat import prompts
    found = mlflow.get_experiment_by_name(experiment)
    if found is None:
        raise RuntimeError(f'Experiment {experiment} does not exist: run the create_chat_experiment job first')
    client = mlflow.MlflowClient()
    mlflow.set_experiment(experiment_id=found.experiment_id)           # prompts register and load under it
    for key in prompts.FILES:
        name, text, sha = prompts.registry_name(schema, key), prompts.read(key), prompts.digest(key)
        current = mlflow.genai.load_prompt(f'prompts:/{name}@deployed', allow_missing=True, link_to_model=False,
                                            cache_ttl_seconds=0)
        if current is not None and (current.tags or {}).get('sha') == sha:
            version = current.version
            logger.info('%s: version %s already matches the file', name, version)
        else:
            version = mlflow.genai.register_prompt(
                name=name, template=text, commit_message=f'server/prompts/chat/{prompts.FILES[key]}',
                tags={'sha': sha}).version
            mlflow.genai.set_prompt_alias(name, 'deployed', version)
            logger.info('%s: registered version %s, alias deployed', name, version)
        _link_to_experiment(name, found.experiment_id)
        client.set_experiment_tag(found.experiment_id, f'chat_prompt.{key}',
                                  json.dumps({'name': name, 'version': str(version), 'sha': sha}))
    if '.' not in schema:                                              # no Unity Catalog (local store): nothing to list
        return [prompts.registry_name(schema, key) for key in prompts.FILES]
    catalog, schema_name = schema.split('.', 1)
    try:
        listed = mlflow.genai.search_prompts(
            filter_string=f"catalog = '{catalog}' AND schema = '{schema_name}' AND experiment_id = '{found.experiment_id}'")
    except mlflow.exceptions.MlflowException as exc:                   # some workspaces refuse any search filter
        logger.warning('Prompts tab of experiment %s not checked: %s', found.experiment_id, exc)
        return [prompts.registry_name(schema, key) for key in prompts.FILES]
    logger.info('Prompts tab of experiment %s (schema %s selected): %d prompts listed: %s', found.experiment_id, schema,
                len(listed), ', '.join(sorted(p.name for p in listed)))
    if not listed:
        raise RuntimeError(f'No prompt of {schema} is linked to experiment {found.experiment_id}')
    return [prompts.registry_name(schema, key) for key in prompts.FILES]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--profile', default='', help='Databricks CLI profile; empty inside a job')
    parser.add_argument('--schema', required=True, help='catalog.schema of the prompt registry (dev_proj.qualibot)')
    parser.add_argument('--experiment', required=True, help='chat experiment that receives the version tags')
    parser.add_argument('--app', default='qualibot', help='Databricks app that writes the chat traces')
    parser.add_argument('--warehouse-id', default='', help='SQL warehouse for the GRANTs to the app; empty = no grant')
    parser.add_argument('--app-root', default='', help='folder holding `server/` (default: two levels above this file)')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    sys.path.insert(0, args.app_root or str(Path(__file__).resolve().parents[2]))
    mlflow.set_tracking_uri(f'databricks://{args.profile}' if args.profile else 'databricks')
    mlflow.set_registry_uri(f'databricks-uc://{args.profile}' if args.profile else 'databricks-uc')
    names = register(args.schema, args.experiment)
    if args.warehouse_id:
        grant_app(names, args.app, args.warehouse_id, args.profile)


if __name__ == '__main__':
    main()