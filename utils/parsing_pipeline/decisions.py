"""Pure decisions of the parsing pipeline (no Spark): parse status, checkpoint row choice, scope guard."""

PENDING_OCR_PREFIX = "pending_llm_ocr"


def parse_status(
    text_length: int,
    size_bytes: int,
    parser_error: str | None,
    parser_strategy: str | None,
    small_file_bytes: int,
) -> str:
    """Status of a freshly parsed file.

    An error always wins over empty text, and a scanned PDF queued for LLM OCR is never a skipped small file.

    :param text_length: length of the extracted text once trimmed
    :param size_bytes: size of the source file in bytes
    :param parser_error: error raised by the parser, None when it ended normally
    :param parser_strategy: strategy that produced the text, None when unknown
    :param small_file_bytes: size up to which an empty result is a terminal skip
    :return: ``ERROR``, ``SKIPPED_EMPTY_SMALL_FILE``, ``EMPTY_TEXT`` or ``SUCCESS``
    """
    if parser_error is not None:
        return "ERROR"
    if text_length == 0:
        pending_ocr = (parser_strategy or "").startswith(PENDING_OCR_PREFIX)
        if size_bytes <= small_file_bytes and not pending_ocr:
            return "SKIPPED_EMPTY_SMALL_FILE"
        return "EMPTY_TEXT"
    return "SUCCESS"


def pick_checkpoint_row(rows: list[dict]) -> dict | None:
    """The checkpoint row that represents a document.

    The sha of the latest attempt is the one of the bytes now selected; a row of other bytes never wins over a row
    of those bytes. Within the same bytes a SUCCESS wins, then the latest attempt.

    :param rows: checkpoint rows of one IDDOC, with ``document_sha256``, ``parse_status`` and ``ingestion_timestamp``
    :return: the row to use, None when there are no rows
    """
    if not rows:
        return None
    latest_sha = max(rows, key=lambda r: r["ingestion_timestamp"])["document_sha256"]
    return min(
        rows,
        key=lambda r: (
            r["document_sha256"] != latest_sha,
            r["parse_status"] != "SUCCESS",
            -r["ingestion_timestamp"],
        ),
    )


def exclude_statuses(target_iddocs: set, iddoc_status: dict, excluded: tuple | set) -> set:
    """Target IDDOCs without those whose recorded status is excluded.

    :param target_iddocs: IDDOCs selected for the run
    :param iddoc_status: recorded ``parse_status`` by IDDOC
    :param excluded: statuses that are never a target
    :return: the IDDOCs that stay targets
    """
    return {i for i in target_iddocs if iddoc_status.get(i) not in excluded}


def scope_dropped_too_much(previous: int, current: int, max_drop_pct: int) -> bool:
    """Whether the scope shrank more than the allowed share compared with the previous manifest.

    :param previous: documents in the previous manifest
    :param current: documents in the new scope
    :param max_drop_pct: allowed drop in percent, 0 turns the guard off
    :return: True when the guard must stop the task; never for a previous manifest under 100 documents
    """
    if max_drop_pct <= 0 or previous < 100:
        return False
    return (previous - current) * 100 > previous * max_drop_pct
