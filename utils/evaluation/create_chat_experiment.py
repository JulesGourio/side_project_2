"""Creates the MLflow experiment that receives one trace per chat turn, and lets the app write to it.

The app (``server/services/chat/mlflow_turns.py``) never creates the experiment. Its name is the value of
``CHAT_MLFLOW_EXPERIMENT`` (``utils/deploy/target_env.json``). The experiment belongs to the identity that runs
the script: the job ``create_chat_experiment_<target>`` (``resources/chat_mlflow.yml``) runs as the job service
principal, which therefore owns it.

USAGE
-----
    python utils/evaluation/create_chat_experiment.py --profile DEV --app qualibot     # owned by you
    python utils/evaluation/create_chat_experiment.py --profile DEV --app qualibot \
        --uc-schema dev_proj.qualibot --warehouse-id <id>      # traces in Delta tables

Without ``--uc-schema`` the traces stay in the experiment's own storage. With it they go to Unity
Catalog tables (MLflow >= 3.11.1; permanent for the experiment). MLflow creates four tables named
``<experiment id>_otel_*`` in that schema, which can be the schema of the project itself: the tables are told apart by
their prefix. The script then gives the app service principal MODIFY and SELECT on those tables only (not on the
schema, which would let the app write to every other table), through ``--warehouse-id``. The app also needs USE CATALOG
and USE SCHEMA on the schema, which it already has.
"""
import argparse
import logging
import os

import mlflow
from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import NotFound
from databricks.sdk.service.ml import ExperimentAccessControlRequest, ExperimentPermissionLevel

logger = logging.getLogger('create_chat_experiment')


def _run_grant(w: WorkspaceClient, warehouse_id: str, privileges: str, full_name: str, principal: str) -> str | None:
    """Runs one GRANT; returns the error message, or None when it succeeded.

    :param w: Workspace client of the job service principal.
    :param warehouse_id: SQL warehouse that runs the statement.
    :param privileges: Privileges to grant, such as ``MODIFY, SELECT``.
    :param full_name: Backticked three-level name of the table or view.
    :param principal: Application id of the principal to grant.
    :return: error message, or None when the grant succeeded.
    """
    statement = f'GRANT {privileges} ON TABLE {full_name} TO `{principal}`'
    result = w.statement_execution.execute_statement(statement=statement, warehouse_id=warehouse_id, wait_timeout='30s')
    if result.status.state.value == 'SUCCEEDED':
        return None
    return result.status.error.message if result.status.error else str(result.status)


def grant_app_on_trace_tables(w: WorkspaceClient, uc_schema: str, experiment_id: str, principal: str, warehouse_id: str) -> None:
    """MODIFY and SELECT for the app on the tables of the experiment (``<experiment id>_*``) and nothing else.
    MLflow also creates views there: MODIFY does not apply to a view, which gets SELECT only. Run by the job service
    principal, which owns what MLflow created. Idempotent.

    :param w: Workspace client of the job service principal.
    :param uc_schema: Unity Catalog schema, ``catalog.schema``, that holds the trace tables.
    :param experiment_id: Id of the experiment; its trace tables are named ``<experiment id>_*``.
    :param principal: Application id of the app service principal to grant.
    :param warehouse_id: SQL warehouse that runs the GRANT statements.
    """
    catalog, schema = uc_schema.split('.', 1)
    names = sorted(t.name for t in w.tables.list(catalog_name=catalog, schema_name=schema) if t.name.startswith(f'{experiment_id}_'))
    if not names:
        raise SystemExit(f'No object named {experiment_id}_* in {uc_schema}: give the app service principal MODIFY and SELECT '
                         'on the trace tables once they exist.')
    for name in names:
        full_name = f'`{catalog}`.`{schema}`.`{name}`'
        error = _run_grant(w, warehouse_id, 'MODIFY, SELECT', full_name, principal)
        privileges = 'MODIFY, SELECT'
        if error and 'PRIVILEGE_NOT_APPLICABLE' in error:
            privileges = 'SELECT'
            error = _run_grant(w, warehouse_id, privileges, full_name, principal)
        if error:
            raise SystemExit(f'GRANT {privileges} ON {full_name} TO {principal} did not succeed: {error}')
        logger.info('granted %s on %s.%s.%s to %s', privileges, catalog, schema, name, principal)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--profile', default='', help='Databricks CLI profile (DEV, UAT…); empty inside a job')
    parser.add_argument('--app', default='qualibot', help='Databricks app that writes the traces')
    parser.add_argument('--experiment', required=True, help='workspace path (chat_mlflow_experiment in databricks.yml)')
    parser.add_argument('--uc-schema', default='', help='catalog.schema for Unity Catalog trace tables (optional)')
    parser.add_argument('--warehouse-id', default='', help='SQL warehouse, required with --uc-schema')
    parser.add_argument('--managers', default='', help='comma-separated groups given CAN_MANAGE (they cannot own it)')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

    w = WorkspaceClient(profile=args.profile or None)
    mlflow.set_tracking_uri(f'databricks://{args.profile}' if args.profile else 'databricks')
    parent = args.experiment.rsplit('/', 1)[0]
    if parent:
        w.workspace.mkdirs(parent)                       # create_experiment does not create the folder
    existing = mlflow.get_experiment_by_name(args.experiment)
    if existing is not None and existing.lifecycle_stage == 'deleted':
        raise SystemExit(f'Experiment {args.experiment} is in the trash and its name stays reserved: empty it from the workspace '
                         'trash, or change chat_mlflow_experiment (databricks.yml) and CHAT_MLFLOW_EXPERIMENT (target_env.json).')
    if args.uc_schema and not existing:
        # Checked first: a failed link soft-deletes the new experiment and its name stays reserved.
        try:
            w.schemas.get(args.uc_schema)
        except NotFound as exc:
            raise SystemExit(f'Schema {args.uc_schema} does not exist: create it and grant the job service principal and the app '
                             f'service principal first. No experiment was created.') from exc
    if existing:
        experiment_id = existing.experiment_id
        logger.info('experiment %s already exists (id %s), left as is', args.experiment, experiment_id)
    elif args.uc_schema:
        from mlflow.entities.trace_location import UnityCatalog
        if not args.warehouse_id:
            parser.error('--warehouse-id is required with --uc-schema')
        os.environ['MLFLOW_TRACING_SQL_WAREHOUSE_ID'] = args.warehouse_id
        catalog, schema = args.uc_schema.split('.', 1)
        experiment_id = mlflow.create_experiment(
            args.experiment, trace_location=UnityCatalog(catalog_name=catalog, schema_name=schema))
        logger.info('experiment %s created (id %s), traces in %s', args.experiment, experiment_id, args.uc_schema)
    else:
        experiment_id = mlflow.create_experiment(args.experiment)
        logger.info('experiment %s created (id %s)', args.experiment, experiment_id)

    mlflow.MlflowClient().set_experiment_tag(experiment_id, 'mlflow.experimentKind', 'genai_development')

    app = w.apps.get(args.app)
    principal = app.service_principal_client_id
    acl = [ExperimentAccessControlRequest(service_principal_name=principal,
                                          permission_level=ExperimentPermissionLevel.CAN_EDIT)]
    managers = [g.strip() for g in args.managers.split(',') if g.strip()]
    acl += [ExperimentAccessControlRequest(group_name=g, permission_level=ExperimentPermissionLevel.CAN_MANAGE)
            for g in managers]
    w.experiments.update_permissions(experiment_id, access_control_list=acl)
    logger.info('app %s (service principal %s) can edit the experiment; managers: %s',
                args.app, principal, ', '.join(managers) or 'none')

    if args.uc_schema:
        grant_app_on_trace_tables(w, args.uc_schema, str(experiment_id), principal, args.warehouse_id)


if __name__ == '__main__':
    main()
