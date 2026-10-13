"""The Lakebase schema bootstrap must keep emitting exactly the same SQL, in the same order.

tests/golden/lakebase_schema.json holds the whitespace-normalised statements captured before the
module was split; there is no database in the test environment, so a recording fake connection
stands in for the pool.
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
