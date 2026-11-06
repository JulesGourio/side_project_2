"""The chatbot spec records every setting that can change an answer, and nothing else changes its signature."""

import re
from pathlib import Path

import pytest

from server.services.chat import prompts, spec

ROOT = Path(__file__).resolve().parents[1]
READ_RE = re.compile(r"""\(\s*['"](CHAT_[A-Z0-9_]+)['"]""")


def variables_read() -> set[str]:
    """Every ``CHAT_*`` variable the server reads (first argument of a call: getenv, _env_float, _int_env...).

    :return: the variable names
    """
    found = set()
    for path in (ROOT / 'server').rglob('*.py'):
        found |= set(READ_RE.findall(path.read_text(encoding='utf-8')))
    return found


def test_every_variable_is_in_the_spec_or_operational() -> None:
    """A new CHAT_* setting must be added to the spec, or declared operational."""
    missing = variables_read() - set(spec.SPEC_VARIABLES) - spec.OPERATIONAL_VARIABLES
    assert not missing, f'add to spec.SPEC_VARIABLES (and spec()) or OPERATIONAL_VARIABLES: {sorted(missing)}'


def test_no_variable_is_both() -> None:
    """A variable is either in the spec or operational."""
    assert not set(spec.SPEC_VARIABLES) & spec.OPERATIONAL_VARIABLES


def test_spec_holds_every_key_it_declares() -> None:
    """Every key of SPEC_VARIABLES, every prompt and the code are written in the spec."""
    values = spec.spec()
    assert set(spec.SPEC_VARIABLES.values()) <= set(values)
    assert {f'prompt.{key}' for key in prompts.FILES} <= set(values)
    assert 'code.engine' in values and 'retrieval.code_normalization' in values
    assert all(isinstance(v, str) and v for v in values.values())


@pytest.mark.parametrize('variable', sorted(spec.SPEC_VARIABLES))
def test_each_variable_changes_its_key(monkeypatch: pytest.MonkeyPatch, variable: str) -> None:
    """Changing one variable changes its own key and the signature.

    :param monkeypatch: pytest fixture
    :param variable: a variable of SPEC_VARIABLES
    """
    import importlib
    from server.services.chat import config as chat_config
    key = spec.SPEC_VARIABLES[variable]
    before = spec.spec()
    value = {'retrieval.translate_only': 'on', 'retrieval.stopwords': 'on', 'retrieval.translate_bridge': 'true',
             'retrieval.passage_order': 'rank' if before[key] == 'score' else 'score',
             'retrieval.raw_on': 'question,en', 'rewrite.reasoning_effort': 'high'}.get(key)
    if value is None:
        value = 'endpoint-x' if not before[key].isdigit() else str(int(before[key]) + 7)
    if key in ('retrieval.translate_only', 'retrieval.stopwords') and before[key] == 'true':
        value = 'off'
    if key == 'retrieval.translate_bridge' and before[key] == 'true':
        value = 'false'
    monkeypatch.setenv(variable, value)
    importlib.reload(chat_config)                                      # read at import
    try:
        after = spec.spec()
    finally:
        monkeypatch.undo()
        importlib.reload(chat_config)
    assert after[key] != before[key], f'{variable}={value} did not change {key}'
    assert spec.signature(after) != spec.signature(before)


def test_signature_is_stable() -> None:
    """The same settings give the same signature."""
    assert spec.signature(spec.spec()) == spec.signature(spec.spec())


def test_version_name_is_readable() -> None:
    """The version name carries the search sizes and 6 characters of the signature."""
    values = spec.spec()
    name = spec.version_name(values, 'qualibot-prod')
    assert re.fullmatch(r'qualibot-prod-k\d+-\d+(-brute)?-[0-9a-f]{6}', name)
    assert spec.describe(values).startswith('Top-k ')
