"""Which checkpoint snapshots to drop."""

import re
from datetime import datetime

_SNAPSHOT = re.compile(r"_pipeline_checkpoint_(\d{8}_\d{4})")


def snapshot_age_days(table_name: str, now: datetime) -> int | None:
    """Age of a checkpoint snapshot, read from the timestamp in its name.

    :param table_name: table name, ``_pipeline_checkpoint_<YYYYMMDD_HHMM>`` for a snapshot
    :param now: current time
    :return: whole days since the snapshot was taken, None when the name is not a snapshot name
    """
    match = _SNAPSHOT.fullmatch(table_name)
    if not match:
        return None
    try:
        return (now - datetime.strptime(match.group(1), "%Y%m%d_%H%M")).days
    except ValueError:
        return None


def snapshots_to_drop(table_names: list[str], now: datetime, keep_days: int) -> list[str]:
    """Snapshots at least ``keep_days`` old; the most recent snapshot is always kept.

    :param table_names: names of the tables of the schema
    :param now: current time
    :param keep_days: age in days from which a snapshot is dropped
    :return: the snapshot names to drop, oldest first
    """
    dated = sorted(n for n in table_names if snapshot_age_days(n, now) is not None)
    return [n for n in dated[:-1] if snapshot_age_days(n, now) >= keep_days]
