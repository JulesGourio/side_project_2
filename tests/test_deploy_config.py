"""Regression guard for utils/deploy/target_env.json's per-target overrides.

Bug found 2026-08-20: the 'uat-test' target never set its impact index, so it
silently fell back to app.yaml's default -- an index dropped two days earlier.
Real symptom: "Impacted Docs" 404ing on uat-test. Nothing caught it because every
eval queried the index directly by name, never through the app's env var. This
guard stops a target from omitting its index overrides again.

Since 2026-10-08 the names carry no version (chunks_index, not chunks_index_v1)
and the Knowledge Assistant is gone: no versioned index and no KA endpoint key
may come back.
"""

import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
TARGET_ENV_PATH = os.path.join(HERE, '..', 'utils', 'deploy', 'target_env.json')
APP_YAML_PATH = os.path.join(HERE, '..', 'app.yaml')

_INDEX_KEYS = ('CHAT_VSI_INDEX', 'COMPARE_IMPACT_INDEX')
_VERSIONED = re.compile(r'chunks\w*_v\d')

# Targets that are real apps and MUST set their own Vector Search indexes
# rather than rely on app.yaml's default (the DEV index).
_LIVE_TARGETS = ('dev', 'uat', 'uat-test', 'prod')


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
            catalog = 'uat' if target == 'uat-test' else target
            assert value.startswith(f'{catalog}_landingzone.'), f'{target}.{key} = {value!r} points at another workspace'


def test_no_versioned_index_names():
    for target, values in _targets(_load_target_env()):
        for name, value in values.items():
            assert not _VERSIONED.search(str(value)), f'{target}.{name} = {value!r}: index names carry no version'
    with open(APP_YAML_PATH, encoding='utf-8') as f:
        assert not _VERSIONED.search(f.read()), f'versioned index name in {APP_YAML_PATH}'


def test_no_knowledge_assistant_endpoint():
    for target, values in _targets(_load_target_env()):
        assert not [k for k in values if k.startswith('CHAT_ENDPOINT')], f'{target}: KA endpoint key (removed 2026-10-08)'
    with open(APP_YAML_PATH, encoding='utf-8') as f:
        assert 'CHAT_ENDPOINT' not in f.read()


# CAPS_BYPASS (server/services/user.py) turns off the group check for every
# visitor. Temporary, DEV only (2026-10-05) -- must never be switched on for a
# target that serves real users.
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
