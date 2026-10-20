"""Guard for utils/deploy/target_env.json's per-target overrides.

A target that omits its index override silently falls back to app.yaml's default index. Evals query
the index by name, not through the app's env var, so only this guard sees the omission.

Index names carry no version suffix, and no CHAT_ENDPOINT* key is set.
"""

import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
TARGET_ENV_PATH = os.path.join(HERE, '..', 'utils', 'deploy', 'target_env.json')
APP_YAML_PATH = os.path.join(HERE, '..', 'app.yaml')

_INDEX_KEYS = ('CHAT_INDEX', 'COMPARE_IMPACT_INDEX')
_VERSIONED = re.compile(r'chunks\w*_v\d')

# Targets that are real apps and MUST set their own Vector Search indexes
# rather than rely on app.yaml's default (the DEV index).
_LIVE_TARGETS = ('dev', 'uat', 'prod')


def _load_target_env() -> dict:
    with open(TARGET_ENV_PATH, encoding='utf-8') as f:
        return json.load(f)


def _targets(config: dict):
    return [(t, v) for t, v in config.items() if not t.startswith('_')]


def test_live_targets_set_their_indexes():
    config = _load_target_env()
    for target in _LIVE_TARGETS:
        assert target in config, f'{target!r} is missing from {TARGET_ENV_PATH}'
        for key in _INDEX_KEYS:
            value = config[target].get(key)
            assert value, f'{target!r} has no {key} override — it would silently fall back to app.yaml\'s default'
            assert value.startswith((f'{target}_landingzone.', f'{target}_proj.')), f'{target}.{key} = {value!r} points at another workspace'


def test_no_versioned_index_names():
    for target, values in _targets(_load_target_env()):
        for name, value in values.items():
            assert not _VERSIONED.search(str(value)), f'{target}.{name} = {value!r}: index names carry no version'
    with open(APP_YAML_PATH, encoding='utf-8') as f:
        assert not _VERSIONED.search(f.read()), f'versioned index name in {APP_YAML_PATH}'


def test_no_knowledge_assistant_endpoint():
    for target, values in _targets(_load_target_env()):
        assert not [k for k in values if k.startswith('CHAT_ENDPOINT')], f'{target}: no chatbot endpoint key other than CHAT_ENDPOINT_*'
    with open(APP_YAML_PATH, encoding='utf-8') as f:
        assert 'CHAT_ENDPOINT' not in f.read()


# CAPS_BYPASS (server/services/infra/user.py) turns off the group check for every
# visitor. DEV only: it must never be switched on for a target that serves real users.
_CAPS_BYPASS_ALLOWED = ('dev',)


def test_caps_bypass_only_on_dev():
    config = _load_target_env()
    for target, values in config.items():
        if target.startswith('_') or target in _CAPS_BYPASS_ALLOWED:
            continue
        assert str(values.get('CAPS_BYPASS', 'false')).lower() != 'true', (
            f'CAPS_BYPASS is on for {target!r} in {TARGET_ENV_PATH} — DEV only'
        )
    with open(APP_YAML_PATH, encoding='utf-8') as f:
        app_yaml_text = f.read()
    assert 'name: CAPS_BYPASS\n    value: "false"' in app_yaml_text, 'app.yaml must default CAPS_BYPASS to "false"'


# The chat experiment path is read by the chat jobs (variable of databricks.yml) and by the app (target_env.json): a move of
# the experiment that updates only one of them silently stops the traces.
BUNDLE_PATH = os.path.join(HERE, '..', 'databricks.yml')
_BUNDLE_TARGETS = {'dev': 'dev', 'uat': 'uat', 'prod': 'prod'}


def test_chat_experiment_path_is_the_same_for_the_jobs_and_the_app():
    import yaml
    with open(BUNDLE_PATH, encoding='utf-8') as f:
        bundle = yaml.safe_load(f)
    default = bundle['variables']['chat_mlflow_experiment']['default']
    config = _load_target_env()
    for env, target in _BUNDLE_TARGETS.items():
        jobs = (bundle['targets'][target].get('variables') or {}).get('chat_mlflow_experiment', default)
        assert config[env].get('CHAT_MLFLOW_EXPERIMENT') == jobs, (
            f'{env}: the app reads {config[env].get("CHAT_MLFLOW_EXPERIMENT")!r}, the jobs use {jobs!r}')
