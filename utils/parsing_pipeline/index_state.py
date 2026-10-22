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
