"""Tests for utils/evaluation/register_chat_prompts.py: new version only when the text changed; alias follows the file."""

import importlib.util
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
    old = mlflow.get_tracking_uri()
    mlflow.set_tracking_uri(f'sqlite:///{tmp_path}/m.db')
    spec = importlib.util.spec_from_file_location('register_chat_prompts', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    yield module
    mlflow.set_tracking_uri(old)


def test_registers_every_prompt_once(script):
    import mlflow
    script.register('chat_test')
    script.register('chat_test')                                       # second run: nothing new
    for key in prompts.FILES:
        found = mlflow.genai.load_prompt(f'prompts:/{prompts.registry_name("chat_test", key)}@deployed')
        assert found.version == 1 and found.tags['sha'] == prompts.digest(key)
        assert found.template == prompts.read(key)


def test_a_changed_file_gets_a_new_version(script, tmp_path, monkeypatch):
    import shutil
    import mlflow
    script.register('chat_test')
    copy = tmp_path / 'prompts'
    shutil.copytree(prompts.PROMPTS_DIR, copy)
    (copy / 'rewrite.md').write_text('Rewrite differently.\n', encoding='utf-8')
    monkeypatch.setattr(prompts, 'PROMPTS_DIR', copy)
    prompts.read.cache_clear()
    try:
        script.register('chat_test')
        found = mlflow.genai.load_prompt(f'prompts:/{prompts.registry_name("chat_test", "rewrite")}@deployed')
        other = mlflow.genai.load_prompt(f'prompts:/{prompts.registry_name("chat_test", "system_base")}@deployed')
    finally:
        prompts.read.cache_clear()
    assert found.version == 2 and found.template == 'Rewrite differently.'
    assert other.version == 1                                           # untouched files keep their version
