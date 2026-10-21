"""requirements.txt, what Databricks Apps installs, pins every package to one version and agrees with uv.lock."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_LINE = re.compile(r'^(?P<name>[A-Za-z0-9._-]+)(\[[^\]]*\])?==(?P<version>[^\s#]+)')


def _normalise(name: str) -> str:
    """Canonical package name: lowercase, runs of ``-``, ``_`` and ``.`` collapsed to ``-``.

    :param name: package name as written
    :return: the canonical name
    """
    return re.sub(r'[-_.]+', '-', name).lower()


def _requirements() -> dict[str, str]:
    """Pinned versions of requirements.txt by canonical package name.

    :return: ``{name: version}``
    """
    pins: dict[str, str] = {}
    for raw in (ROOT / 'requirements.txt').read_text(encoding='utf-8').splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        match = _LINE.match(line)
        assert match, f'requirements.txt line is not an exact pin (name==version): {line!r}'
        pins[_normalise(match['name'])] = match['version']
    return pins


def test_every_requirement_is_an_exact_pin() -> None:
    """A range lets the installed version drift from the tested one."""
    assert _requirements()


def test_pins_agree_with_the_lock_file_when_there_is_one() -> None:
    """uv.lock is what the tests resolve to: a pin that differs would deploy an untested version."""
    lock = ROOT / 'uv.lock'
    if not lock.exists():
        return
    locked: dict[str, str] = {}
    for block in lock.read_text(encoding='utf-8').split('[[package]]')[1:]:
        name = re.search(r'^name = "([^"]+)"', block, re.M)
        version = re.search(r'^version = "([^"]+)"', block, re.M)
        if name and version:
            locked[_normalise(name.group(1))] = version.group(1)
    differing = {n: (v, locked[n]) for n, v in _requirements().items() if n in locked and locked[n] != v}
    assert not differing, f'requirements.txt vs uv.lock: {differing}'
