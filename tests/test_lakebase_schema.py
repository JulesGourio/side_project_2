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
    """The legacy tables exist. `used_ids`: new tables where a legacy id is already taken. `orphans`: a legacy feedback
    points to a message that is not there. `nothing_missing`: new tables that already hold every legacy row."""

    def __init__(self, used_ids=(), orphans=False, nothing_missing=()):
        self.used_ids = set(used_ids)
        self.orphans = orphans
        self.nothing_missing = set(nothing_missing)
        self.inserts = {}

    async def fetchval(self, query, *args):
        if 'to_regclass' in query:
            return True
        if 'f.message_id IS NOT NULL' in query:
            return self.orphans
        if ' JOIN ' in query:
            return any(f'JOIN {table} c' in query for table in self.used_ids)
        for table in ('compare_messages', 'compare_feedbacks', 'app_errors'):
            if f'NOT EXISTS (SELECT 1 FROM {table} c' in query:
                return table not in self.inserts and table not in self.nothing_missing
        return None

    async def fetch(self, query, *args):
        return [{'column_name': 'id'}, {'column_name': 'message_id'}]

    async def execute(self, query, *args):
        if query.startswith('INSERT INTO'):
            self.inserts[query.split()[2]] = query
        return 'INSERT 0 1'


def _carry_over(conn):
    from server.services.infra.lakebase.schema import _carry_over_legacy_rows
    asyncio.run(_carry_over_legacy_rows(conn))
    return conn


def test_legacy_rows_are_copied_into_tables_that_already_hold_rows():
    conn = _carry_over(_LegacyConn())
    assert set(conn.inserts) == {'compare_messages', 'compare_feedbacks', 'app_errors'}
    assert all('WHERE NOT EXISTS (SELECT 1 FROM' in query for query in conn.inserts.values())


def test_a_feedback_loses_the_message_deleted_before_the_rename():
    conn = _carry_over(_LegacyConn(orphans=True))
    assert 'CASE WHEN EXISTS (SELECT 1 FROM compare_messages p WHERE p.id = l.message_id)' in conn.inserts['compare_feedbacks']


def test_nothing_is_copied_when_a_legacy_id_is_already_used():
    conn = _carry_over(_LegacyConn(used_ids={'compare_messages', 'compare_feedbacks', 'app_errors'}))
    assert conn.inserts == {}


def test_feedbacks_wait_for_their_messages_when_some_point_to_a_missing_one():
    conn = _carry_over(_LegacyConn(used_ids={'compare_messages'}, orphans=True))
    assert 'compare_messages' not in conn.inserts and 'compare_feedbacks' not in conn.inserts
    assert 'app_errors' in conn.inserts


def test_feedbacks_are_copied_as_they_are_when_every_message_they_point_to_is_there():
    conn = _carry_over(_LegacyConn(used_ids={'compare_messages'}, orphans=False))
    assert 'compare_feedbacks' in conn.inserts and 'CASE WHEN' not in conn.inserts['compare_feedbacks']


def test_a_second_start_copies_nothing():
    conn = _carry_over(_LegacyConn(nothing_missing={'compare_messages', 'compare_feedbacks', 'app_errors'}))
    assert conn.inserts == {}
