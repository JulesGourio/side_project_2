"""MLflow configuration (tracking in the workspace, prompt registry in Unity Catalog) for the chat traces."""

import mlflow

mlflow.set_tracking_uri('databricks')
mlflow.set_registry_uri('databricks-uc')   # chat prompts are registered in Unity Catalog (utils/evaluation/register_chat_prompts.py)
