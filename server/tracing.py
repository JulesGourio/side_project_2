"""Minimal MLflow configuration for feedback logging."""

import mlflow

mlflow.set_tracking_uri('databricks')
mlflow.set_registry_uri('databricks-uc')   # chat prompts are registered in Unity Catalog (utils/evaluation/register_chat_prompts.py)
