"""Health check endpoint."""

import logging
import time

from fastapi import APIRouter

from server.services.infra.lakebase import connection

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get('/health', response_model=None)
async def health_check() -> dict:
  """Returns application health status.

  The status stays ``healthy`` while Lakebase is unreachable (the app still answers); ``lakebase`` tells which state it
  is in: ``ready``, ``connecting`` (history and logging are off until it connects) or ``disabled``.
  """
  return {
    'status': 'healthy',
    'lakebase': connection.status(),
    'timestamp': int(time.time() * 1000),
  }
