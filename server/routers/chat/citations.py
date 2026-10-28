"""GET /chat/citations/{code}.csv — the full list of the documents citing a reference code.

The chat answers "which documents cite X?" from ``doc_references``; past ``references.LIST_ALL_MAX`` documents the answer
gives the count and the most recent ones, with a link to this download for the complete list.
"""

import csv
import io

from fastapi import APIRouter, Depends
from fastapi.responses import Response

from server.services.chat.engine.config import normalize_division
from server.services.infra.doc_references import citing_documents
from server.services.infra.user import require_chat
from utils.parsing_pipeline.code_normalization import canonical

router = APIRouter()

_COLUMNS = ('ref', 'title', 'division', 'revision', 'doc_date', 'passages')


@router.get('/chat/citations/{code}.csv', dependencies=[Depends(require_chat)], response_model=None)
async def citations_csv(code: str, division: str = 'ALL') -> Response:
    """The documents citing ``code`` as a CSV file (semicolon-separated, UTF-8 with BOM, for Excel).

    :param code: reference code, any spelling (``SF-1027``, ``SF1027``)
    :param division: ``ALL``, ``AS`` or ``IS``
    :return: the CSV download, one row per citing REF, most recently published first
    """
    canon = canonical(code)
    out = io.StringIO()
    writer = csv.writer(out, delimiter=';')
    writer.writerow(_COLUMNS)
    for doc in citing_documents(canon, normalize_division(division)):
        writer.writerow([doc.get(c) if doc.get(c) is not None else '' for c in _COLUMNS])
    return Response(content='﻿' + out.getvalue(), media_type='text/csv; charset=utf-8',
                    headers={'Content-Disposition': f'attachment; filename="documents_citing_{canon}.csv"'})
