"""FastAPI app — QualiBOT (document compare + chat)."""

import asyncio
import logging
import os
import time
import uuid
from logging import Formatter
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import Response

from server import tracing  # noqa: F401 — sets up MLflow tracking URI
from server.routers import chat, compare, config, exports, feedback, health, history, preview
from server.services.infra.doc_catalog import catalog_refresher
from server.services.infra.doc_references import references_refresher
from server.services.infra.lakebase import get_pool, init_lakebase, shutdown_lakebase, store_error
from server.services.chat.translation_bridge import shutdown_http_client as shutdown_translation_bridge
from server.services.infra.http_clients import close_shared_clients

logging.basicConfig(
  level=logging.INFO,
  format='%(asctime)s UTC - %(name)s - %(levelname)s - %(message)s',
  datefmt='%Y-%m-%d %H:%M:%S',
  handlers=[logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

env_local_loaded = load_dotenv(dotenv_path='.env.local')
env = os.getenv('ENV', 'development' if env_local_loaded else 'production')
logger.info(f'Starting in {env} mode')

# Log critical env vars at startup to detect misconfiguration early
_volume_path = os.getenv('COMPARE_VOLUME_PATH', '')
_lakebase_id = os.getenv('LAKEBASE_PROJECT_ID', '')
_app_version  = os.getenv('APP_VERSION', '1.4.1')
logger.info(f'COMPARE_VOLUME_PATH = {_volume_path!r}')
logger.info(f'LAKEBASE_PROJECT_ID  = {_lakebase_id!r}')
logger.info(f'APP_VERSION          = {_app_version!r}')
if not _volume_path:
    logger.warning('COMPARE_VOLUME_PATH is not set — file save/restore disabled. '
                   'Set it via databricks.yml (resources.apps.doc-compare.env) or app.yaml.')


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
  """Open the Lakebase pool and start the document catalog and references refreshers, then close them on shutdown.

  :param app: the FastAPI application, unused here
  """
  await init_lakebase()
  # Document catalog (REF links, titles) from Lakebase, reloaded every 30 min.
  catalog_task = asyncio.create_task(catalog_refresher(get_pool))
  # Documents citing each reference code ("which documents cite X?"), same cadence.
  references_task = asyncio.create_task(references_refresher(get_pool))
  yield
  catalog_task.cancel()
  references_task.cancel()
  await shutdown_lakebase()
  await shutdown_translation_bridge()
  await close_shared_clients()


_API_DOCS = os.getenv('ENABLE_API_DOCS', '').lower() == 'true'
app = FastAPI(title='QualiBOT', version='1.0.0', lifespan=lifespan,
              docs_url='/docs' if _API_DOCS else None, redoc_url='/redoc' if _API_DOCS else None,
              openapi_url='/openapi.json' if _API_DOCS else None)

# CORS: allow localhost in dev, same-origin only in production
allowed_origins = ['http://localhost:3000'] if env == 'development' else []
app.add_middleware(
  CORSMiddleware,
  allow_origins=allowed_origins,
  allow_credentials=True,
  allow_methods=['*'],
  allow_headers=['*'],
)


SECURITY_HEADERS = {
  'Content-Security-Policy': (
    "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; "
    "font-src 'self' data:; script-src 'self'; connect-src 'self'; frame-src 'self' blob:; "
    "object-src 'none'; base-uri 'self'; frame-ancestors 'self'"
  ),
  'X-Content-Type-Options': 'nosniff',
  'Referrer-Policy': 'same-origin',
}


@app.middleware('http')
async def add_security_headers(request: Request, call_next) -> Response:
  """Set the browser security headers (CSP, nosniff, referrer policy) on every HTTP response.

  :param request: the incoming request
  :param call_next: the next handler in the middleware chain
  :return: the downstream response with ``SECURITY_HEADERS`` added
  """
  response = await call_next(request)
  for name, value in SECURITY_HEADERS.items():
    response.headers[name] = value
  return response


@app.middleware('http')
async def log_requests(request: Request, call_next) -> Response:
  """Log each request and its outcome, and tag the response with a short request id.

  :param request: the incoming request
  :param call_next: the next handler in the middleware chain
  :return: the response of the downstream handler, with the ``X-Request-ID`` header set
  """
  req_id = str(uuid.uuid4())[:8]
  start = time.monotonic()
  logger.info(f'[{req_id}] --> {request.method} {request.url.path}')
  try:
    response = await call_next(request)
    elapsed = time.monotonic() - start
    logger.info(f'[{req_id}] <-- {response.status_code} ({elapsed:.2f}s)')
    response.headers['X-Request-ID'] = req_id
    return response
  except Exception as exc:
    elapsed = time.monotonic() - start
    logger.error(f'[{req_id}] !! unhandled exception after {elapsed:.2f}s: {exc}', exc_info=True)
    raise


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
  """Log an unhandled exception, record it in the error store, and answer with a generic 500.

  :param request: the request that raised
  :param exc: the unhandled exception
  :return: a JSON response with a generic error body and status 500
  """
  logger.error(f'Unhandled exception on {request.method} {request.url.path}: {exc}', exc_info=True)
  store_error(
      endpoint=f'{request.method} {request.url.path}',
      error_type=type(exc).__name__,
      error_msg=str(exc),
      stage='unhandled',
      exc=exc,
      user_message='Internal server error',
  )
  # The server-error layer answers outside the middleware stack, so the headers are set here too.
  return JSONResponse(status_code=500, content={'error': 'Internal server error'}, headers=SECURITY_HEADERS)

API_PREFIX = '/api'
app.include_router(health.router, prefix=API_PREFIX, tags=['health'])
app.include_router(config.router, prefix=API_PREFIX, tags=['config'])
app.include_router(compare.router, prefix=API_PREFIX, tags=['compare'])
app.include_router(exports.router, prefix=API_PREFIX, tags=['exports'])
app.include_router(chat.router, prefix=API_PREFIX, tags=['chat'])
app.include_router(history.router, prefix=API_PREFIX, tags=['history'])
app.include_router(feedback.router, prefix=API_PREFIX, tags=['feedback'])
app.include_router(preview.router, prefix=API_PREFIX, tags=['preview'])

build_path = Path('.') / 'client/out'
if build_path.exists():
  logger.info(f'Serving static files from {build_path}')
  app.mount('/assets', StaticFiles(directory=str(build_path / 'assets')), name='assets')

  for static_dir in ['images', 'logos', 'videos', 'content']:
    dir_path = build_path / static_dir
    if dir_path.exists():
      app.mount(f'/{static_dir}', StaticFiles(directory=str(dir_path)), name=static_dir)

  @app.get('/{full_path:path}', response_model=None)
  async def serve_spa(request: Request, full_path: str) -> FileResponse:
    """SPA catch-all: serve index.html for any non-API route.

    :param request: the incoming request, unused here
    :param full_path: path after the host, without the leading slash
    :return: the file at that path when it exists, otherwise index.html
    """
    file_path = build_path / full_path
    if full_path and file_path.is_file():
      return FileResponse(str(file_path))
    return FileResponse(
      str(build_path / 'index.html'),
      headers={'Cache-Control': 'no-cache, no-store, must-revalidate'},
    )
else:
  logger.warning(
    f'Build directory {build_path} not found. '
    'Run: cd client && bun run build'
  )
