"""Test environment: the suite runs as a development app, with no visitor header.

The access checks treat ENV=development as the only open mode, so the tests set it explicitly
instead of relying on the default, which is production.
"""
import os

os.environ.setdefault('ENV', 'development')


import pytest


@pytest.fixture(autouse=True)
def _chat_settings_from_defaults(monkeypatch):
    """Every test starts from the default chat settings, whatever the developer's shell exports."""
    for name in [n for n in os.environ if n.startswith('CHAT_')]:
        monkeypatch.delenv(name, raising=False)
