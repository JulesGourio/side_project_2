"""Sync of the visitors' groups from the audit log to ``users`` (utils/lakebase_sync/user_capabilities.py)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'utils', 'lakebase_sync'))

import user_capabilities as uc  # noqa: E402

CHATBOT = 'Role-Project-LEAP-End-users-Qualibot-ChatBot'
DOCCOMPARE = 'Role-Project-LEAP-End-users-Qualibot-DocCompare'
COREDEV = 'Role-Project-LEAP-CoreDev'


def test_the_job_tracks_the_groups_the_app_checks():
    from server.services.infra import user

    assert uc.CAPS_CHAT_GROUPS == user._CAPS_CHAT_GROUPS
    assert uc.CAPS_COMPARE_GROUPS == user._CAPS_COMPARE_GROUPS
    for group in uc.CAPS_ALL_GROUPS:
        assert f"'{group}'" in uc.AUDIT_SQL


def test_the_audit_wins_for_the_pairs_it_holds_and_the_rest_is_kept():
    events = [
        ('1', 'a@x.com', CHATBOT, True),
        ('1', None, DOCCOMPARE, False),
        ('2', 'b@x.com', DOCCOMPARE, True),
        ('3', 'c@x.com', 'Some-Other-Group', True),
    ]
    stored = {'1': {DOCCOMPARE, COREDEV}, '9': {CHATBOT}}
    merged = uc.merge_groups(events, stored)
    assert merged == {
        '1': {'email': 'a@x.com', 'groups': [COREDEV, CHATBOT], 'can_chat': True, 'can_compare': True},
        '2': {'email': 'b@x.com', 'groups': [DOCCOMPARE], 'can_chat': False, 'can_compare': True},
    }


def test_a_visitor_removed_from_every_group_gets_no_capability():
    merged = uc.merge_groups([('1', 'a@x.com', CHATBOT, False)], {'1': {CHATBOT}})
    assert merged['1']['groups'] == [] and not merged['1']['can_chat'] and not merged['1']['can_compare']


class _Cursor:
    def __init__(self, rowcounts, rows=None):
        self.rowcounts = list(rowcounts)
        self.rows = rows or []
        self.statements = []
        self.rowcount = 0

    def execute(self, query, params=None):
        self.statements.append((' '.join(query.split()), params))
        if query.lstrip().startswith('UPDATE'):
            self.rowcount = self.rowcounts.pop(0)

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None

    def fetchall(self):
        return self.rows

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Conn:
    def __init__(self, cursor):
        self.cur = cursor
        self.committed = False

    def cursor(self):
        return self.cur

    def commit(self):
        self.committed = True


def test_write_updates_the_row_on_user_id_or_email_and_inserts_a_new_visitor():
    merged = uc.merge_groups([('1', 'a@x.com', CHATBOT, True), ('2', 'b@x.com', DOCCOMPARE, True)], {})
    cur = _Cursor(rowcounts=[1, 0])
    conn = _Conn(cur)
    assert uc.write_groups(conn, merged) == 2
    kinds = [s.split()[0] for s, _ in cur.statements]
    assert kinds == ['UPDATE', 'UPDATE', 'INSERT']
    assert cur.statements[0][1] == ([CHATBOT], True, False, '1', 'a@x.com')
    assert 'groups_synced_at = NOW()' in cur.statements[0][0]
    assert cur.statements[2][1] == ('2', 'b@x.com', [DOCCOMPARE], False, True)
    assert conn.committed


def test_missing_columns_stop_the_write_without_altering_the_table():
    """The job identity may write rows but not alter a table owned by the app."""
    import pytest

    cur = _Cursor(rowcounts=[], rows=[('user_id',), ('groups',)])
    with pytest.raises(RuntimeError, match='can_chat'):
        uc.check_columns(_Conn(cur))
    assert all(s.startswith('SELECT') for s, _ in cur.statements)
    uc.check_columns(_Conn(_Cursor(rowcounts=[], rows=[(c,) for c, _ in uc.USER_GROUP_COLUMNS])))


def test_stored_groups_are_empty_before_the_columns_exist():
    cur = _Cursor(rowcounts=[], rows=[])
    assert uc.read_stored_groups(_Conn(cur)) == {}
    assert len(cur.statements) == 1
