"""Registers the chatbot's prompt files in the MLflow prompt registry (Unity Catalog).

Each file of ``server/prompts/chat/`` becomes the prompt ``<schema>.chat_<key>``: a new version only when
its text changed, alias ``deployed`` moved to the version whose ``sha`` tag matches the file. The version is
also written on the chat experiment as the tag ``chat_prompt.<key>`` = {name, version, sha}: the app reads
only that tag (it has no access to the registry) to link the version to every trace of a turn that used the
prompt (``server/services/chat/mlflow_turns.py``), and ignores a prompt whose hash differs from its file.
Run this job after every deploy that touches a prompt (``register_chat_prompts_<target>``, ``resources/chat_mlflow.yml``);
the experiment must exist (``create_chat_experiment``).

USAGE
-----
    python utils/evaluation/register_chat_prompts.py --profile DEV --schema dev_proj.qualibot --experiment /Shared/qualibot/chat_turns

In a job the script is run with ``exec`` (no ``__file__``): the job passes ``--app-root`` (the bundle's synced files).

The identity running it needs USE CATALOG, USE SCHEMA and CREATE FUNCTION on the schema.
"""
import argparse
import json
import logging
import sys
from pathlib import Path

import mlflow

logger = logging.getLogger('register_chat_prompts')


def register(schema: str, experiment: str) -> None:
    from server.services.chat import prompts
    found = mlflow.get_experiment_by_name(experiment)
    if found is None:
        raise RuntimeError(f'Experiment {experiment} does not exist: run the create_chat_experiment job first')
    client = mlflow.MlflowClient()
    for key in prompts.FILES:
        name, text, sha = prompts.registry_name(schema, key), prompts.read(key), prompts.digest(key)
        current = mlflow.genai.load_prompt(f'prompts:/{name}@deployed', allow_missing=True, link_to_model=False)
        if current is not None and (current.tags or {}).get('sha') == sha:
            version = current.version
            logger.info('%s: version %s already matches the file', name, version)
        else:
            version = mlflow.genai.register_prompt(
                name=name, template=text, commit_message=f'server/prompts/chat/{prompts.FILES[key]}',
                tags={'sha': sha}).version
            mlflow.genai.set_prompt_alias(name, 'deployed', version)
            logger.info('%s: registered version %s, alias deployed', name, version)
        client.set_experiment_tag(found.experiment_id, f'chat_prompt.{key}',
                                  json.dumps({'name': name, 'version': str(version), 'sha': sha}))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--profile', default='', help='Databricks CLI profile; empty inside a job')
    parser.add_argument('--schema', required=True, help='catalog.schema of the prompt registry (dev_proj.qualibot)')
    parser.add_argument('--experiment', default='/Shared/qualibot/chat_turns', help='chat experiment that receives the version tags')
    parser.add_argument('--app-root', default='', help='folder holding `server/` (default: two levels above this file)')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    sys.path.insert(0, args.app_root or str(Path(__file__).resolve().parents[2]))
    mlflow.set_tracking_uri(f'databricks://{args.profile}' if args.profile else 'databricks')
    mlflow.set_registry_uri(f'databricks-uc://{args.profile}' if args.profile else 'databricks-uc')
    register(args.schema, args.experiment)


if __name__ == '__main__':
    main()
