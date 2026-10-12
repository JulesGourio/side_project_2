"""Creates the MLflow experiment that receives one trace per chat turn, and lets the app write to it.

The app (``server/services/mlflow_turns.py``) never creates the experiment: run this once per
workspace, then set ``CHAT_MLFLOW_EXPERIMENT`` for the target (``utils/deploy/target_env.json``).

USAGE
-----
    python utils/evaluation/create_chat_experiment.py --profile DEV --app qualibot
    python utils/evaluation/create_chat_experiment.py --profile DEV --app qualibot \
        --uc-schema dev_landingzone.qualibot --warehouse-id <id>      # traces in Delta tables

Without ``--uc-schema`` the traces stay in the experiment's own storage. With it they go to Unity
Catalog tables (MLflow >= 3.11.1; permanent for the experiment); the app service principal then also
needs USE CATALOG / USE SCHEMA / MODIFY / SELECT on that schema (printed at the end).
"""
import argparse
import logging
import os

import mlflow
from databricks.sdk import WorkspaceClient
from databricks.sdk.service.ml import ExperimentAccessControlRequest, ExperimentPermissionLevel

logger = logging.getLogger('create_chat_experiment')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--profile', required=True, help='Databricks CLI profile of the workspace (DEV, UAT…)')
    parser.add_argument('--app', default='qualibot', help='Databricks app that writes the traces')
    parser.add_argument('--experiment', default='/Shared/qualibot/chat_turns')
    parser.add_argument('--uc-schema', default='', help='catalog.schema for Unity Catalog trace tables (optional)')
    parser.add_argument('--warehouse-id', default='', help='SQL warehouse, required with --uc-schema')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

    w = WorkspaceClient(profile=args.profile)
    mlflow.set_tracking_uri(f'databricks://{args.profile}')
    parent = args.experiment.rsplit('/', 1)[0]
    if parent:
        w.workspace.mkdirs(parent)                       # create_experiment does not create the folder
    existing = mlflow.get_experiment_by_name(args.experiment)
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

    app = w.apps.get(args.app)
    principal = app.service_principal_client_id
    w.experiments.update_permissions(experiment_id, access_control_list=[ExperimentAccessControlRequest(
        service_principal_name=principal, permission_level=ExperimentPermissionLevel.CAN_EDIT)])
    logger.info('app %s (service principal %s) can edit the experiment', args.app, principal)

    if args.uc_schema:
        catalog, schema = args.uc_schema.split('.', 1)
        logger.info('now run in a SQL editor:\n'
                    'GRANT USE CATALOG ON CATALOG %s TO `%s`;\nGRANT USE SCHEMA, MODIFY, SELECT ON SCHEMA %s TO `%s`;',
                    catalog, principal, args.uc_schema, principal)


if __name__ == '__main__':
    main()
