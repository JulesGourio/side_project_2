"""The grants the experiment-creation job gives the app on the trace objects: tables get MODIFY and SELECT, views SELECT only."""

import importlib.util
import os
from types import SimpleNamespace

import pytest

pytest.importorskip('mlflow')
SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'utils', 'evaluation', 'create_chat_experiment.py')
spec = importlib.util.spec_from_file_location('create_chat_experiment', SCRIPT)
create_chat_experiment = importlib.util.module_from_spec(spec)
spec.loader.exec_module(create_chat_experiment)


class _FakeWorkspace:
    """Objects of the schema; a GRANT with MODIFY fails on a view, as Unity Catalog answers."""

    def __init__(self, names, views=()):
        self.statements = []
        self.tables = SimpleNamespace(list=lambda catalog_name, schema_name: [SimpleNamespace(name=n) for n in names])
        self.statement_execution = SimpleNamespace(execute_statement=self._execute)
        self._views = views

    def _execute(self, statement, warehouse_id, wait_timeout):
        self.statements.append(statement)
        bad = 'MODIFY' in statement and any(f'.`{v}`' in statement for v in self._views)
        state = SimpleNamespace(value='FAILED' if bad else 'SUCCEEDED')
        error = SimpleNamespace(message='[ErrorClass=INVALID_PARAMETER_VALUE.PRIVILEGE_NOT_APPLICABLE_TO_ENTITY] Privilege MODIFY is not applicable') if bad else None
        return SimpleNamespace(status=SimpleNamespace(state=state, error=error))


def test_views_get_select_only_and_tables_get_modify_and_select():
    w = _FakeWorkspace(['42_otel_spans', '42_trace_metadata', 'chunks', '421_otel_spans'], views=['42_trace_metadata'])
    create_chat_experiment.grant_app_on_trace_tables(w, 'uat_proj.qualibot', '42', 'app-sp', 'wh')
    grants = [s for s in w.statements if s.startswith('GRANT SELECT ON') or s.startswith('GRANT MODIFY, SELECT ON')]
    assert 'GRANT MODIFY, SELECT ON TABLE `uat_proj`.`qualibot`.`42_otel_spans` TO `app-sp`' in grants
    assert 'GRANT SELECT ON TABLE `uat_proj`.`qualibot`.`42_trace_metadata` TO `app-sp`' in grants
    assert not [s for s in w.statements if '`chunks`' in s or '`421_otel_spans`' in s]


def test_no_object_of_the_experiment_is_an_error():
    w = _FakeWorkspace(['chunks'])
    with pytest.raises(SystemExit):
        create_chat_experiment.grant_app_on_trace_tables(w, 'uat_proj.qualibot', '42', 'app-sp', 'wh')
