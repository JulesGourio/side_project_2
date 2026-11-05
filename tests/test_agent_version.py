"""Agent versions: one per spec signature, reused, tagged when deployed, prompts linked by hash."""

import json
from types import SimpleNamespace

import pytest

pytest.importorskip('mlflow')

from server.services.chat import agent_version, prompts, spec


@pytest.fixture
def registry(monkeypatch):
    """A fake MLflow: logged models by name, tags, prompt links, experiment tags naming registered prompts.

    :param monkeypatch: pytest fixture
    :return: the recorded state
    """
    import mlflow
    state = {'models': {}, 'tags': {}, 'links': [], 'created': 0}
    values = spec.spec()
    experiment_tags = {f'chat_prompt.{key}': json.dumps({'name': f'c.s.chat_{key}', 'version': '3',
                                                         'sha': prompts.digest(key)}) for key in prompts.FILES}
    experiment_tags['chat_prompt.rewrite'] = json.dumps({'name': 'c.s.chat_rewrite', 'version': '1', 'sha': 'stale'})

    def search(experiment_ids, filter_string, output_format):
        name = filter_string.split("'")[1]
        return [state['models'][name]] if name in state['models'] else []

    def create(name, model_type, experiment_id, params, tags):
        state['created'] += 1
        state['models'][name] = SimpleNamespace(model_id=f'm-{state["created"]}', params=params, tags=tags)
        return state['models'][name]

    class Client:
        def get_experiment(self, experiment_id):
            return SimpleNamespace(tags=experiment_tags)

        def link_prompt_version_to_model(self, name, version, model_id):
            state['links'].append((name, version, model_id))

    monkeypatch.setattr(mlflow, 'search_logged_models', search)
    monkeypatch.setattr(mlflow, 'create_external_model', create)
    monkeypatch.setattr(mlflow, 'set_logged_model_tags', lambda model_id, tags: state['tags'].update(tags))
    monkeypatch.setattr(mlflow, 'MlflowClient', Client)
    monkeypatch.setattr(agent_version, '_versions', {})
    state['values'] = values
    return state


def test_created_once_with_the_spec_as_params(registry):
    first = agent_version.ensure('7', registry['values'])
    agent_version._versions.clear()
    assert agent_version.ensure('7', registry['values']) == first
    model = next(iter(registry['models'].values()))
    assert registry['created'] == 1 and model.params == registry['values']
    assert model.tags['qualibot.signature'] == spec.signature(registry['values'])
    assert model.tags['mlflow.note.content'].startswith('Top-k ')


def test_prompts_linked_only_when_the_hash_matches(registry):
    agent_version.ensure('7', registry['values'])
    names = {name for name, _, _ in registry['links']}
    assert 'c.s.chat_system_base' in names and 'c.s.chat_rewrite' not in names
    assert len(names) == len(prompts.FILES) - 1


def test_another_setting_is_another_version(registry):
    other = {**registry['values'], 'retrieval.top_k_first_message': '99'}
    assert agent_version.ensure('7', registry['values']) != agent_version.ensure('7', other)


def test_deployed_version_is_tagged(registry):
    agent_version.ensure('7', registry['values'], deployed='prod')
    assert 'qualibot.deployed.prod' in registry['tags']


def test_deployed_env_from_the_index(monkeypatch):
    monkeypatch.setenv('CHAT_INDEX', 'uat_proj.qualibot.chunks_index')
    assert agent_version.deployed_env() == 'uat'
    monkeypatch.setenv('CHAT_INDEX', 'other.qualibot.x')
    assert agent_version.deployed_env() == ''
