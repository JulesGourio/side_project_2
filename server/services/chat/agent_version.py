"""Agent versions of the chatbot: one MLflow LoggedModel per spec signature, in the chat experiment.

The app attaches every trace to the version it runs (``mlflow.modelId``) and marks it ``qualibot.deployed.<env>``; the
retrieval evaluation attaches its traces and runs to the version of each configuration it measures. A configuration
measured by the evaluation and the one the app runs share the same version when their specs are equal.
The version's parameters are the spec, one per setting; its description is the spec in one line of French; its
prompts are the registered versions whose hash matches the files.
"""

import json
import logging
import threading
from datetime import datetime, timezone
from typing import Any, Dict

from server.services.chat import spec as chat_spec

logger = logging.getLogger(__name__)

DEPLOYED_TAG = 'qualibot.deployed.{env}'
EVALUATED_TAG = 'qualibot.evaluated.retrieval'
_lock = threading.Lock()
_versions: Dict[tuple, str] = {}            # (experiment id, signature) -> model id


def deployed_env() -> str:
    """Environment the app runs in, from the catalog of its index.

    :return: ``dev``, ``uat`` or ``prod``; empty when the index name does not tell
    """
    catalog = chat_spec.engine_config.index_name().split('.', 1)[0]
    return catalog[:-len('_proj')] if catalog.endswith('_proj') else ''


def _link_prompts(mlflow: Any, experiment_id: str, model_id: str, values: Dict[str, str]) -> int:
    """Links to the version the registered prompts whose hash is the one in the spec.

    :param mlflow: the mlflow module
    :param experiment_id: chat experiment, whose ``chat_prompt.<key>`` tags name the registered versions
    :param model_id: the version
    :param values: its spec
    :return: the number of prompts linked
    """
    client = mlflow.MlflowClient()
    tags = client.get_experiment(experiment_id).tags or {}
    linked = 0
    for key, value in tags.items():
        if not key.startswith('chat_prompt.'):
            continue
        entry = json.loads(value)
        if not str(entry.get('sha', '')).startswith(values.get(f"prompt.{key[len('chat_prompt.'):]}", '-')):
            continue
        try:
            client.link_prompt_version_to_model(name=entry['name'], version=str(entry['version']), model_id=model_id)
            linked += 1
        except Exception as exc:  # noqa: BLE001 — the version stays usable without the link
            logger.warning('agent_version: prompt %s v%s not linked to %s: %s', entry['name'], entry['version'],
                           model_id, exc)
    return linked


def ensure(experiment_id: str, values: Dict[str, str] | None = None, deployed: str = '') -> str:
    """Model id of the agent version of a spec, created on first use.

    :param experiment_id: chat experiment
    :param values: spec of the version, None for the one the process runs now
    :param deployed: environment that runs this version (tags it ``qualibot.deployed.<env>``), empty for none
    :return: the model id
    """
    import mlflow
    values = values or chat_spec.spec()
    signature = chat_spec.signature(values)
    key = (experiment_id, signature, deployed)
    with _lock:
        if key in _versions:
            return _versions[key]
    name = chat_spec.version_name(values)
    found = mlflow.search_logged_models(experiment_ids=[experiment_id], filter_string=f"name = '{name}'",
                                        output_format='list')
    if found:
        model_id = found[0].model_id
    else:
        model_id = mlflow.create_external_model(
            name=name, model_type='agent', experiment_id=experiment_id, params=values,
            tags={'qualibot.signature': signature, 'mlflow.note.content': chat_spec.describe(values)}).model_id
        logger.info('agent_version: %s created (%s), %d prompts linked', name, model_id,
                    _link_prompts(mlflow, experiment_id, model_id, values))
    if deployed:
        mlflow.set_logged_model_tags(model_id, {DEPLOYED_TAG.format(env=deployed):
                                                datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')})
    with _lock:
        _versions[key] = model_id
    return model_id
