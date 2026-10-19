"""The Lakebase schema bootstrap must keep emitting exactly the same SQL, in the same order.

tests/golden/lakebase_schema.json holds the whitespace-normalised statements; there is no database in the
test environment, so a recording fake connection stands in for the pool.
"""

import asyncio
import json
from pathlib import Path

from server.services.infra.lakebase.schema import ensure_schema

GOLDEN = Path(__file__).parent / 'golden' / 'lakebase_schema.json'


class _RecordingConn:
    def __init__(self):
        self.statements = []

    async def execute(self, query, *args):
        self.statements.append(' '.join(query.split()))

    async def fetch(self, query, *args):
        self.statements.append(' '.join(query.split()))
        return []

    async def fetchval(self, query, *args):
        return False


class _RecordingPool:
    def __init__(self):
        self.conn = _RecordingConn()

    def acquire(self):
        conn = self.conn

        class _Ctx:
            async def __aenter__(self_inner):
                return conn

            async def __aexit__(self_inner, *exc):
                return False

        return _Ctx()


def test_ensure_schema_emits_expected_sql():
    pool = _RecordingPool()
    asyncio.run(ensure_schema(pool))
    expected = json.loads(GOLDEN.read_text(encoding='utf-8'))
    assert pool.conn.statements == expected


def test_every_index_targets_a_table_the_schema_creates():
    import re
    statements = json.loads(GOLDEN.read_text(encoding='utf-8'))
    created = {m.group(1) for s in statements for m in re.finditer(r'CREATE TABLE IF NOT EXISTS (\w+)', s)}
    indexed = {m.group(1) for s in statements for m in re.finditer(r'CREATE INDEX IF NOT EXISTS \w+ ON (\w+)', s)}
    assert indexed, 'no index statement found: the pattern is out of date'
    assert indexed <= created, sorted(indexed - created)


class _LegacyConn:
    """The legacy tables exist, the new ones are empty; `orphans` says whether a legacy feedback points to a missing message."""

    def __init__(self, orphans):
        self.orphans = orphans
        self.inserts = []

    async def fetchval(self, query, *args):
        if 'to_regclass' in query:
            return True
        if 'SELECT EXISTS (SELECT 1 FROM compare_' in query or 'SELECT EXISTS (SELECT 1 FROM app_errors' in query:
            return False
        if 'NOT EXISTS (SELECT 1 FROM compare_messages' in query:
            return self.orphans
        return None

    async def fetch(self, query, *args):
        return [{'column_name': 'id'}]

    async def execute(self, query, *args):
        if query.startswith('INSERT INTO'):
            self.inserts.append(query.split()[2])
        return 'INSERT 0 1'


def test_legacy_feedbacks_are_not_copied_when_their_messages_are_missing():
    from server.services.infra.lakebase.schema import _carry_over_legacy_rows
    conn = _LegacyConn(orphans=True)
    asyncio.run(_carry_over_legacy_rows(conn))
    assert 'compare_feedbacks' not in conn.inserts
    assert 'compare_messages' in conn.inserts and 'app_errors' in conn.inserts


def test_legacy_feedbacks_are_copied_when_every_message_is_there():
    from server.services.infra.lakebase.schema import _carry_over_legacy_rows
    conn = _LegacyConn(orphans=False)
    asyncio.run(_carry_over_legacy_rows(conn))
    assert 'compare_feedbacks' in conn.inserts
