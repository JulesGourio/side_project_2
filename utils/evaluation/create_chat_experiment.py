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


def grant_app_on_trace_tables(w: WorkspaceClient, uc_schema: str, experiment_id: str, principal: str, warehouse_id: str) -> None:
    """MODIFY and SELECT for the app on the trace tables of the experiment (``<experiment id>_*``) and nothing else.
    Run by the job service principal, which owns the tables MLflow just created. Idempotent."""
    catalog, schema = uc_schema.split('.', 1)
    tables = sorted(t.name for t in w.tables.list(catalog_name=catalog, schema_name=schema) if t.name.startswith(f'{experiment_id}_'))
    if not tables:
        raise SystemExit(f'No trace table named {experiment_id}_* in {uc_schema}: give the app service principal MODIFY and SELECT '
                         'on them once they exist.')
    for name in tables:
        statement = f'GRANT MODIFY, SELECT ON TABLE `{catalog}`.`{schema}`.`{name}` TO `{principal}`'
        result = w.statement_execution.execute_statement(statement=statement, warehouse_id=warehouse_id, wait_timeout='30s')
        if result.status.state.value != 'SUCCEEDED':
            raise SystemExit(f'{statement} did not succeed: {result.status}')
        logger.info('granted MODIFY, SELECT on %s.%s.%s to %s', catalog, schema, name, principal)


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
                             f'service principal first (runbooks/uat_bascule.md Q13). No experiment was created.') from exc
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
