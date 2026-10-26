"""Tests for utils/evaluation/register_chat_prompts.py: new version only when the text changed; alias and experiment tag follow the file."""

import importlib.util
import json
import os

import pytest

pytest.importorskip('mlflow')
pytest.importorskip('sqlalchemy')
pytest.importorskip('alembic')

from server.services.chat import prompts

SCRIPT = os.path.join(os.path.dirname(__file__), '..', 'utils', 'evaluation', 'register_chat_prompts.py')


@pytest.fixture
def script(tmp_path):
    import mlflow
    old, old_registry = mlflow.get_tracking_uri(), mlflow.get_registry_uri()
    mlflow.set_tracking_uri(f'sqlite:///{tmp_path}/m.db')
    mlflow.set_registry_uri(f'sqlite:///{tmp_path}/m.db')           # server.tracing points it at Unity Catalog
    spec = importlib.util.spec_from_file_location('register_chat_prompts', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    mlflow.create_experiment('chat', artifact_location=str(tmp_path))
    yield module
    mlflow.set_tracking_uri(old)
    mlflow.set_registry_uri(old_registry)


def test_registers_every_prompt_once(script):
    import mlflow
    script.register('chat_test', 'chat')
    script.register('chat_test', 'chat')                                       # second run: nothing new
    for key in prompts.FILES:
        found = mlflow.genai.load_prompt(f'prompts:/{prompts.registry_name("chat_test", key)}@deployed', cache_ttl_seconds=0)
        assert found.version == 1 and found.tags['sha'] == prompts.digest(key)
        assert found.template == prompts.read(key)
    experiment = mlflow.get_experiment_by_name('chat')
    for key in prompts.FILES:                                          # listed in the experiment's Prompts tab
        found = mlflow.genai.get_prompt_tags(prompts.registry_name('chat_test', key))
        listed = getattr(found, 'tags', found)['_mlflow_experiment_ids']
        assert listed == f',{experiment.experiment_id},'
    tags = experiment.tags
    entry = json.loads(tags['chat_prompt.rewrite'])
    assert entry == {'name': 'chat_test.chat_rewrite', 'version': '1', 'sha': prompts.digest('rewrite')}


def test_the_experiment_must_exist(script):
    with pytest.raises(RuntimeError, match='does not exist'):
        script.register('chat_test', 'no-such-experiment')


def test_a_changed_file_gets_a_new_version(script, tmp_path, monkeypatch):
    import shutil
    import mlflow
    script.register('chat_test', 'chat')
    copy = tmp_path / 'prompts'
    shutil.copytree(prompts.PROMPTS_DIR, copy)
    (copy / 'rewrite.md').write_text('Rewrite differently.\n', encoding='utf-8')
    monkeypatch.setattr(prompts, 'PROMPTS_DIR', copy)
    prompts.read.cache_clear()
    try:
        script.register('chat_test', 'chat')
        found = mlflow.genai.load_prompt(f'prompts:/{prompts.registry_name("chat_test", "rewrite")}@deployed', cache_ttl_seconds=0)
        other = mlflow.genai.load_prompt(f'prompts:/{prompts.registry_name("chat_test", "system_base")}@deployed', cache_ttl_seconds=0)
    finally:
        prompts.read.cache_clear()
    assert found.version == 2 and found.template == 'Rewrite differently.'
    assert json.loads(mlflow.get_experiment_by_name('chat').tags['chat_prompt.rewrite'])['version'] == '2'
    assert other.version == 1                                           # untouched files keep their version
