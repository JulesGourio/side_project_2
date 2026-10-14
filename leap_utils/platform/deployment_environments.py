"""
This module centralizes Enums and Mappings related to the Databricks environments
"""

from enum import Enum


class WorkspaceIds(Enum):
    """Databricks Workspace ID for each environment"""

    dev = 2865348338307293
    prod = 1210945332048776
    uat = 3155144190025826


class WorkspaceHosts(Enum):
    """Databricks Workspace Hostname for each environment"""

    dev = "dbc-c623749d-731b.cloud.databricks.com"
    prod = "dbc-77b87d5a-6bcb.cloud.databricks.com"
    uat = "dbc-3a17bfce-9e88.cloud.databricks.com"


class DefaultSqlWarehouse(Enum):
    """HTTP path of the default SQL Warehouse in each environment"""

    dev = "/sql/1.0/warehouses/13eff4a6095513cb"
    prod = "/sql/1.0/warehouses/03054ddc44b7f902"
    uat = "/sql/1.0/warehouses/5890912c31867b77"


# -------------------------------------------------------------------
# IAM Role definitions for s3
# -------------------------------------------------------------------
class RoleArn(Enum):
    """AWS IAM Role ARN by environment"""

    dev = (
        "arn:aws:iam::512505875250:role/databricks-workspace-stack-access-data-buckets"
    )
    uat = "arn:aws:iam::760394329341:role/databricks-workspace-stack-uat-access-data-buckets"
    prod = (
        "arn:aws:iam::952338794904:role/databricks-workspace-stack-access-data-buckets"
    )


class ExternalId(Enum):
    """External IDs depending on environment"""

    dev = "e2c872e5-b20a-435d-ba42-e5574e8cb999"
    uat = "e2c872e5-b20a-435d-ba42-e5574e8cb999"
    prod = f"databricks-serverless-{WorkspaceIds.prod.value}"
    # WorkspaceIds is already imported above in the module


# Mapping buckets → environment
BUCKET_ENV = {
    "leap-s3-weeu1-dev": "dev",
    "leap-s3-raw-weeu1-dev": "dev",
    "leap-s3-dbx-sd-dev": "dev",
    "leap-s3-raw-sd-lat-dev": "dev",
    "leap-s3-weeu1-uat": "uat",
    "leap-s3-raw-weeu1-uat": "uat",
    "leap-s3-dbx-sd-uat": "uat",
    "leap-s3-raw-sd-lat-uat": "uat",
    "leap-s3-weeu1-prod": "prod",
    "leap-s3-raw-weeu1-prod": "prod",
    "leap-s3-dbx-sd-prod": "prod",
    "leap-s3-raw-sd-lat-prod": "prod",
}


def get_iam_for_bucket(bucket: str):
    """
    Resolve IAM role, external_id and assumption mode for a given bucket.
    """
    env = BUCKET_ENV.get(bucket)
    if not env:
        raise KeyError(f"Unknown bucket: {bucket}")

    return {
        "env": env,
        "assume": True,
        "role_arn": RoleArn[env].value,
        "external_id": ExternalId[env].value,
    }


# -------------------------------------------------------------------
# STS Client Wrapper
# -------------------------------------------------------------------
import boto3
from datetime import datetime, timedelta, timezone
from botocore.config import Config
from botocore.exceptions import ClientError
from time import sleep


class STSClientWrapper:
    """
    Wrapper that manages STS tokens and automatically refreshes S3 client.
    Uses environment IAM resolution via get_iam_for_bucket().

    :param bucket: S3 bucket name
    :param force_no_assume: force local credentials (same account)
    """

    def __init__(self, bucket: str, force_no_assume: bool = False):
        self.bucket = bucket
        self.force_no_assume = force_no_assume
        self.client = None
        self.expiration = datetime.now(timezone.utc)
        self.refresh_client()

    def _assume_role(
        self, role_arn: str, external_id: str, session_name: str, retries: int = 3
    ):
        cfg = Config(max_pool_connections=64)
        sts = boto3.client("sts", config=cfg)
        last_err = None

        for attempt in range(1, retries + 1):
            try:
                kwargs = {
                    "RoleArn": role_arn,
                    "RoleSessionName": session_name,
                }
                if external_id:
                    kwargs["ExternalId"] = external_id

                return sts.assume_role(**kwargs)["Credentials"]

            except ClientError as e:
                last_err = e

                if e.response.get("Error", {}).get("Code") in {
                    "Throttling",
                    "TooManyRequestsException",
                }:
                    sleep(2**attempt)
                    continue

                raise

        raise last_err

    def refresh_client(self):
        boto_cfg = Config(max_pool_connections=64)

        info = get_iam_for_bucket(self.bucket)

        if self.force_no_assume or not info.get("assume", True):
            self.client = boto3.client("s3", config=boto_cfg)
            self.expiration = datetime.now(timezone.utc) + timedelta(hours=12)
            return

        creds = self._assume_role(
            role_arn=info["role_arn"],
            external_id=info["external_id"],
            session_name=f"assume-{self.bucket}",
        )

        self.client = boto3.client(
            "s3",
            aws_access_key_id=creds["AccessKeyId"],
            aws_secret_access_key=creds["SecretAccessKey"],
            aws_session_token=creds["SessionToken"],
            config=boto_cfg,
        )

        self.expiration = creds["Expiration"] - timedelta(minutes=10)

    def get_client(self):
        if datetime.now(timezone.utc) >= self.expiration:
            self.refresh_client()
        return self.client
