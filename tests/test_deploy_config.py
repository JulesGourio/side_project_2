"""Regression guard for utils/deploy/target_env.json's per-target overrides.

Bug found 2026-08-20: the 'uat-test' target never set CompareImpactIndex, so
it silently fell back to app.yaml's default -- which was itself stale
("chunks_index_v2", an index dropped during the 2026-08-18 retention fix;
only the _v1 indexes exist now). Real symptom: "Impacted Docs" 404ing in
production on uat-test. Nothing caught this because every test/eval so far
queried the vector index directly by name, never through the deployed app's
actual env var. This can't be fully prevented without a live click-through
after each deploy, but this guard at least stops a target from silently
omitting the override again, and locks the known-dead index string out.

Moved 2026-08-21 from parsing deploy_qualibot.ps1's PS1 hashtable text to
reading target_env.json directly -- that JSON file is now the single source
of truth for these overrides (shared with bitbucket-pipelines.yml).

Also guards two standalone eval scripts under utils/databricks_ops/evaluation/
that hardcoded the same dead index as a live query parameter -- not deployed,
so nothing had run them since the index was dropped. probe_ka_routing_consistency.py
also mentions "_v2", but only as a historical note about a specific past
experiment's endpoint config -- deliberately excluded here, since that
mention is meant to stay factual, not track the current index.
"""

import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
TARGET_ENV_PATH = os.path.join(HERE, '..', 'utils', 'deploy', 'target_env.json')
APP_YAML_PATH = os.path.join(HERE, '..', 'app.yaml')
EVAL_SCRIPT_PATHS = (
    os.path.join(HERE, '..', 'utils', 'databricks_ops', 'evaluation', 'eval_rag_vs_agent.py'),
    os.path.join(HERE, '..', 'utils', 'databricks_ops', 'evaluation', 'multilingual_audit', 'probe_index_language_bias.py'),
)

_DEAD_INDEX = 'chunks_index_v2'

# Targets that are real, currently-deployed apps and MUST set their own
# Vector Search index explicitly rather than relying on app.yaml's default.
# 'prod' is deliberately excluded -- it has no workspace/KA agents yet
# (see utils/deploy/deploy_qualibot.ps1's own "TODO: set prod KA endpoints").
_LIVE_TARGETS = ('uat', 'uat-test')


def _load_target_env() -> dict:
    with open(TARGET_ENV_PATH, encoding='utf-8') as f:
        return json.load(f)


def test_live_targets_set_compare_impact_index():
    config = _load_target_env()
    for target in _LIVE_TARGETS:
        assert target in config, f'{target!r} is missing from {TARGET_ENV_PATH}'
        value = config[target].get('COMPARE_IMPACT_INDEX')
        assert value, f'{target!r} has no COMPARE_IMPACT_INDEX override — it would silently fall back to app.yaml\'s default'


def test_dead_index_v2_is_gone():
    config = _load_target_env()
    for target, values in config.items():
        if target.startswith('_'):
            continue
        for name, value in values.items():
            assert _DEAD_INDEX not in str(value), (
                f'{_DEAD_INDEX!r} does not exist (dropped 2026-08-18) — '
                f'must not be reintroduced ({target}.{name} in {TARGET_ENV_PATH})'
            )
    with open(APP_YAML_PATH, encoding='utf-8') as f:
        app_yaml_text = f.read()
    assert _DEAD_INDEX not in app_yaml_text, f'{_DEAD_INDEX!r} does not exist (dropped 2026-08-18) — must not be reintroduced in {APP_YAML_PATH}'
    for path in EVAL_SCRIPT_PATHS:
        with open(path, encoding='utf-8') as f:
            text = f.read()
        assert _DEAD_INDEX not in text, f'{_DEAD_INDEX!r} does not exist (dropped 2026-08-18) — must not be reintroduced in {path}'
