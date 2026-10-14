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
