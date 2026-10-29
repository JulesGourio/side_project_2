"""State of a Vector Search index while its Delta sync runs."""


def index_is_updating(detailed_state: str) -> bool:
    """Whether an index is still provisioning or applying a triggered update.

    ``ONLINE_NO_PENDING_UPDATE`` (nothing to apply) and ``ONLINE_CONTINUOUS_UPDATE`` (a continuous index, always
    reporting an update) are settled states.

    :param detailed_state: ``detailed_state`` of the index status, any case
    :return: True while the sync has not finished
    """
    state = (detailed_state or "").upper()
    if "NO_PENDING_UPDATE" in state or "CONTINUOUS_UPDATE" in state:
        return False
    return any(
        marker in state for marker in ("PROVISIONING", "SYNC", "TRIGGERED_UPDATE", "UPDATING")
    )


def sync_settled(
    ready: bool,
    detailed_state: str,
    indexed_rows: int | None,
    expected_rows: int | None,
    seen_updating: bool,
    waited_s: float,
    grace_s: float,
) -> bool:
    """Whether a triggered sync has finished, and not merely not started yet.

    Right after the trigger an index still reports its previous, settled state for a while: a settled state counts
    only once the update was seen running, or the index holds as many rows as its source table, or ``grace_s`` has
    passed without the update showing (nothing to apply).

    :param ready: ``status.ready`` of the index
    :param detailed_state: ``detailed_state`` of the index status, any case
    :param indexed_rows: ``indexed_row_count`` of the index, None when unknown
    :param expected_rows: rows of the source table when the sync was triggered, None when unknown
    :param seen_updating: whether a poll since the trigger saw the update running
    :param waited_s: seconds since the trigger
    :param grace_s: seconds after which a settled state is trusted even without having seen the update
    :return: True when the sync is over
    """
    if not ready or index_is_updating(detailed_state):
        return False
    if seen_updating:
        return True
    if expected_rows is not None and indexed_rows == expected_rows:
        return True
    return waited_s >= grace_s
