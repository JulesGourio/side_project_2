"""Registers the chatbot's prompt files in the MLflow prompt registry (Unity Catalog).

Each file of ``server/prompts/chat/`` becomes the prompt ``<schema>.chat_<key>``: a new version only when
its text changed, alias ``deployed`` moved to the version whose ``sha`` tag matches the file. The app
links that version to every trace of a turn that used the prompt (``server/services/chat/mlflow_turns.py``),
and ignores a prompt whose registered hash differs from its file: run this job after every deploy that
touches a prompt (the ``register_chat_prompts_<target>`` job, ``resources/chat_mlflow.yml``).

USAGE
-----
    python utils/evaluation/register_chat_prompts.py --profile DEV --schema dev_proj.qualibot

In a job the script is run with ``exec`` (no ``__file__``): the job passes ``--app-root`` (the bundle's synced files).

The identity running it needs USE CATALOG, USE SCHEMA and CREATE FUNCTION on the schema.
"""
import argparse
import logging
import sys
from pathlib import Path

import mlflow

logger = logging.getLogger('register_chat_prompts')


def register(schema: str) -> None:
    from server.services.chat import prompts
    for key in prompts.FILES:
        name, text, sha = prompts.registry_name(schema, key), prompts.read(key), prompts.digest(key)
        current = mlflow.genai.load_prompt(f'prompts:/{name}@deployed', allow_missing=True, link_to_model=False)
        if current is not None and (current.tags or {}).get('sha') == sha:
            logger.info('%s: version %s already matches the file', name, current.version)
            continue
        version = mlflow.genai.register_prompt(
            name=name, template=text, commit_message=f'server/prompts/chat/{prompts.FILES[key]}', tags={'sha': sha})
        mlflow.genai.set_prompt_alias(name, 'deployed', version.version)
        logger.info('%s: registered version %s, alias deployed', name, version.version)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--profile', default='', help='Databricks CLI profile; empty inside a job')
    parser.add_argument('--schema', required=True, help='catalog.schema of the prompt registry (dev_proj.qualibot)')
    parser.add_argument('--app-root', default='', help='folder holding `server/` (default: two levels above this file)')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    sys.path.insert(0, args.app_root or str(Path(__file__).resolve().parents[2]))
    mlflow.set_tracking_uri(f'databricks://{args.profile}' if args.profile else 'databricks')
    mlflow.set_registry_uri(f'databricks-uc://{args.profile}' if args.profile else 'databricks-uc')
    register(args.schema)


if __name__ == '__main__':
    main()
