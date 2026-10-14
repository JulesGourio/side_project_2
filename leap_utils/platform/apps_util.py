"""
This module centralizes functions related to managing Databricks Apps
"""

import requests
import time
from databricks.sdk.runtime import dbutils
from leap_utils.common import logger
from typing import List, Optional

LOGGER = logger.get_logger(module_name=__name__)

# === Databricks credentials (from notebook context) ===
TOKEN = (
    dbutils.notebook.entry_point.getDbutils().notebook().getContext().apiToken().get()
)
LOGGER.info("Databricks Token retrieved.")

DATABRICKS_INSTANCE = (
    dbutils.notebook.entry_point.getDbutils().notebook().getContext().apiUrl().get()
)
LOGGER.info(f"Databricks Host: {DATABRICKS_INSTANCE}")

# Headers for authentication
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}


# === PROTECTED APPS (never stopped, regardless of action) ===
PROTECTED_APPS: List[str] = ["qualibot"]


# === FUNCTIONS ===
def start_app(app_name: str) -> bool:
    """
    Start a Databricks App.

    :param app_name: Name of the Databricks App to start.
    :type app_name: str
    :return: True if the app started successfully, False otherwise.
    :rtype: bool
    """
    url = f"{DATABRICKS_INSTANCE}/api/2.0/apps/{app_name}/start"
    response = requests.post(url, headers=HEADERS)

    if response.status_code == 200:
        LOGGER.info(f"✅ App '{app_name}' started successfully.")
        LOGGER.debug(response.text)
        return True
    else:
        LOGGER.error(
            f"❌ Error while starting ({response.status_code}): {response.text}"
        )
        return False


def stop_app(app_name: str) -> bool:
    """
    Stop a Databricks App.

    :param app_name: Name of the Databricks App to stop.
    :type app_name: str
    :return: True if the app stopped successfully, False otherwise.
    :rtype: bool
    """
    if app_name in PROTECTED_APPS:
        LOGGER.warning(
            f"⚠️ App '{app_name}' is protected and cannot be stopped. Skipping."
        )
        return False

    url = f"{DATABRICKS_INSTANCE}/api/2.0/apps/{app_name}/stop"
    response = requests.post(url, headers=HEADERS)

    if response.status_code == 200:
        LOGGER.info(f"🛑 App '{app_name}' stopped successfully.")
        LOGGER.debug(response.text)
        return True
    else:
        LOGGER.error(
            f"❌ Error while stopping ({response.status_code}): {response.text}"
        )
        return False


def get_app_status(app_name: str) -> Optional[str]:
    """
    Retrieve the current status of a specific Databricks App.

    :param app_name: Name of the Databricks App to check.
    :type app_name: str
    :return: Current state of the app. Possible values:
             ``RUNNING``, ``CRASHED``, ``DEPLOYING``, ``UNAVAILABLE``,
             ``RATE_LIMITED`` or None if unable to fetch.
    :rtype: Optional[str]
    """
    url = f"{DATABRICKS_INSTANCE}/api/2.0/apps/{app_name}"
    response = requests.get(url, headers=HEADERS)

    if response.status_code == 200:
        data = response.json()
        app_status = data.get("app_status", {})
        state = app_status.get("state", "UNAVAILABLE")
        message = app_status.get("message", "")
        LOGGER.info(f"ℹ️ Current status of app '{app_name}': {state} - {message}")
        LOGGER.debug(response.text)
        return state
    elif response.status_code == 429:
        LOGGER.info("⚠️ Too many requests (HTTP 429). Waiting before retrying...")
        LOGGER.debug(response.text)
        return "RATE_LIMITED"
    else:
        LOGGER.error(f"❌ Error {response.status_code}: {response.text}")
        return None


def wait_for_app_running(app_name: str, timeout: int = 600, interval: int = 30) -> bool:
    """
    Wait for the app to reach the ``RUNNING`` state.

    Handles transient states such as ``UNAVAILABLE`` and API throttling (HTTP 429).

    :param app_name: Name of the Databricks App.
    :type app_name: str
    :param timeout: Maximum time to wait in seconds. Default is 600 seconds.
    :type timeout: int
    :param interval: Interval in seconds between checks. Default is 30 seconds.
    :type interval: int
    :return: True if the app reached RUNNING state before timeout, False otherwise.
    :rtype: bool
    """
    start_time = time.time()
    backoff = interval  # dynamic backoff

    while True:
        state = get_app_status(app_name)

        if state == "RUNNING":
            LOGGER.info(f"✅ App '{app_name}' is now RUNNING!")
            return True
        elif state == "CRASHED":
            LOGGER.error(f"❌ App '{app_name}' has crashed!")
            return False
        elif state == "UNAVAILABLE":
            LOGGER.info(f"⚠️ App '{app_name}' is temporarily UNAVAILABLE, waiting...")
        elif state == "RATE_LIMITED":
            # Increase wait time on throttling
            backoff = min(backoff * 2, 60)  # max 1 minute
            LOGGER.info(f"⏳ Backoff activated: next check in {backoff}s...")
        elif state is None:
            LOGGER.error("❌ Unable to retrieve state, retrying after a delay.")
        else:
            LOGGER.info(f"⏳ App is in state {state}, waiting...")

        # Global timeout
        if time.time() - start_time > timeout:
            LOGGER.error(f"⚠️ Timeout reached ({timeout}s), app not RUNNING")
            return False

        time.sleep(backoff)


def wait_for_app_stopped(app_name: str, timeout: int = 300, interval: int = 5) -> bool:
    """
    Wait for the Databricks App to be completely stopped.

    Considered as stopped if:
      - State is ``UNAVAILABLE``
      - The app cannot be found (404 response)

    Handles throttling (HTTP 429) with exponential backoff.

    :param app_name: Name of the Databricks App.
    :type app_name: str
    :param timeout: Maximum time to wait in seconds. Default is 300 seconds.
    :type timeout: int
    :param interval: Interval in seconds between checks. Default is 5 seconds.
    :type interval: int
    :return: True if the app is stopped, False if timeout is reached or error occurred.
    :rtype: bool
    """
    start_time = time.time()
    backoff = interval

    LOGGER.info(f"⏳ Waiting for the app '{app_name}' to be stopped...")

    while True:
        state = get_app_status(app_name)

        if state == "RUNNING":
            LOGGER.info(f"⚠️ App '{app_name}' is still RUNNING. Waiting...")
        elif state == "CRASHED":
            LOGGER.info(f"⚠️ App '{app_name}' crashed. Considering it as stopped.")
            return True
        elif state == "UNAVAILABLE":
            LOGGER.info(f"✅ App '{app_name}' is now stopped (state=UNAVAILABLE).")
            return True
        elif state == "RATE_LIMITED":
            # Increase wait time on throttling
            backoff = min(backoff * 2, 60)  # max 1 minute
            LOGGER.info(f"⏳ Backoff activated: next check in {backoff}s...")
        elif state == "DEPLOYING":
            LOGGER.error(
                f"ℹ️ App '{app_name}' is currently DEPLOYING. It cannot be stopped right now..."
            )
            return False
        elif state is None:
            LOGGER.error("❌ Unable to retrieve state, retrying after a delay.")
        else:
            LOGGER.info(f"⏳ Current state: {state}")

        # Global timeout
        if time.time() - start_time > timeout:
            LOGGER.error(f"⚠️ Timeout reached ({timeout}s), the app is not yet stopped.")
            return False

        time.sleep(backoff)


def list_all_apps() -> List[str]:
    """
    Retrieve the list of all Databricks Apps.

    :return: List of app names detected in the Databricks workspace.
    :rtype: List[str]
    """
    url = f"{DATABRICKS_INSTANCE}/api/2.0/apps"
    response = requests.get(url, headers=HEADERS)

    if response.status_code == 200:
        apps = response.json().get("apps", [])
        app_names = [app["name"] for app in apps]
        LOGGER.info(f"📋 Detected apps: {app_names}")
        LOGGER.debug(response.text)
        return app_names
    else:
        LOGGER.error(
            f"❌ Error while retrieving apps ({response.status_code}): {response.text}"
        )
        return []


def stop_all_apps() -> None:
    """
    Stop all Databricks Apps detected in the workspace.

    Iterates over all apps and calls :func:`stop_app` and :func:`wait_for_app_stopped` for each.

    :return: None
    """
    app_names = list_all_apps()
    if not app_names:
        LOGGER.info("ℹ️ No apps to stop.")
        return

    LOGGER.info("🛑 Attempting to stop all apps...")
    for app_name in app_names:
        stop_app(app_name)
        wait_for_app_stopped(app_name)
