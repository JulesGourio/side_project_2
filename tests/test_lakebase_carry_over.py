"""Copy of the rows of the tables renamed in the Lakebase refactor (server/services/infra/lakebase/schema.py)."""
import asyncio

from server.services.infra.lakebase import schema


class FakeConn:
    def __init__(self, tables):
        self.tables = tables  # name -> {"cols": [...], "rows": int}
        self.executed = []

    def _name_arg(self, args):
        return args[0]

    async def fetchval(self, sql, *args):
        if 'to_regclass' in sql:
            return self._name_arg(args) in self.tables
        if 'SELECT EXISTS' in sql:
            table = sql.split('FROM ')[1].split(')')[0].strip()
            return self.tables[table]['rows'] > 0
        raise AssertionError(sql)

    async def fetch(self, sql, *args):
        table = self._name_arg(args)
        return [{'column_name': c} for c in self.tables[table]['cols']]

    async def execute(self, sql, *args):
        self.executed.append(sql)
        return 'INSERT 0 3' if sql.startswith('INSERT') else 'SELECT 1'


def _run(conn):
    asyncio.run(schema._carry_over_legacy_rows(conn))


def test_copies_the_common_columns_into_an_empty_new_table():
    conn = FakeConn({
        'messages': {'cols': ['id', 'user_id', 'old_filename', 'legacy_only'], 'rows': 3},
        'compare_messages': {'cols': ['id', 'user_id', 'old_filename', 'new_only'], 'rows': 0},
    })
    _run(conn)
    inserts = [s for s in conn.executed if s.startswith('INSERT')]
    assert inserts == ['INSERT INTO compare_messages (id, user_id, old_filename) SELECT id, user_id, old_filename FROM messages']
    assert any('setval' in s and "'compare_messages'" in s for s in conn.executed)


def test_does_nothing_once_the_new_table_holds_rows():
    conn = FakeConn({
        'messages': {'cols': ['id'], 'rows': 3},
        'compare_messages': {'cols': ['id'], 'rows': 1},
    })
    _run(conn)
    assert not any(s.startswith('INSERT') for s in conn.executed)


def test_does_nothing_without_a_legacy_table():
    conn = FakeConn({'compare_messages': {'cols': ['id'], 'rows': 0}})
    _run(conn)
    assert conn.executed == []
