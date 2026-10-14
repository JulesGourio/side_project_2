"""Config endpoint — exposes app configuration to the frontend."""

import logging
import os
from functools import lru_cache

import yaml
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..services.infra.user import get_capabilities, get_current_user

logger = logging.getLogger(__name__)
router = APIRouter()

_PROCESSORS_YML = os.path.join(
    os.path.dirname(__file__), '..', 'services', 'compare', 'processors', 'versions.yml'
)


@lru_cache(maxsize=1)
def _load_processors_config() -> dict:
    """Parse the processor version mapping once and cache the result."""
    path = os.path.normpath(_PROCESSORS_YML)
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f) or {}
    except Exception as e:
        logger.warning(f'Could not load the processor versions: {e}')
        return {}


@router.get('/config/app')
async def get_app_config():
    """Return application configuration (branding, compare settings)."""
    try:
        safe = {
            'branding': {
                'logo': '/logos/LOGO_LATECOERE.png',
            },
            'compare': {
                'volume_path': os.getenv('COMPARE_VOLUME_PATH', ''),
                'impact_index': os.getenv('COMPARE_IMPACT_INDEX', ''),
            },
            'chat': {
                'enabled': os.getenv('CHAT_ENABLED', 'true').lower() == 'true',
            },
        }
        return safe
    except Exception as e:
        logger.error(f'Error loading app config: {e}')
        return JSONResponse({'error': str(e)}, status_code=500)


@router.get('/config/processors')
async def get_processors_config():
    """Return the processor version mapping (from server/services/compare/processors/versions.yml)."""
    try:
        data = _load_processors_config()
        return data
    except Exception as e:
        logger.error(f'Error loading processors config: {e}')
        return JSONResponse({'error': str(e)}, status_code=500)


@router.get('/config/knowledge-base-date')
async def get_knowledge_base_date():
    """Return the knowledge base documents_as_of date (null when not set)."""
    from ..services.infra.lakebase import get_pool
    pool = get_pool()
    if not pool:
        return {'date': None}
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT documents_as_of FROM catalog_metadata WHERE id = 1'
            )
        date_val = row['documents_as_of'] if row else None
        return {'date': date_val.isoformat() if date_val else None}
    except Exception as e:
        logger.warning(f'Could not fetch knowledge base date: {e}')
        return {'date': None}


@router.get('/me')
async def get_me(request: Request):
    """Return current user info with feature permissions."""
    try:
        await get_current_user(request)  # no identity in the headers: the request fails
        caps = await get_capabilities(request)
        return {
            'can_compare': caps['can_compare'],
            'can_chat': caps['can_chat'],
        }
    except Exception as e:
        logger.error(f'Error getting user info: {e}')
        return JSONResponse({'error': str(e)}, status_code=500)
