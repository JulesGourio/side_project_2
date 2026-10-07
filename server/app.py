"""FastAPI app — QualiBOT (document compare + knowledge assistant chat)."""

import asyncio
import logging
import os
import time
import traceback
import uuid
from logging import Formatter
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.cors import CORSMiddleware

from . import tracing  # noqa: F401 — sets up MLflow tracking URI
from .routers import chat, compare, config, exports, feedback, health, history, preview
from .services.lakebase import get_pool, init_lakebase, shutdown_lakebase, store_error
from .services.translation_bridge import shutdown_http_client as shutdown_translation_bridge

logging.basicConfig(
  level=logging.INFO,
  format='%(asctime)s UTC - %(name)s - %(levelname)s - %(message)s',
  datefmt='%Y-%m-%d %H:%M:%S',
  handlers=[logging.StreamHandler()],
)
Formatter.converter = time.gmtime  # force all log timestamps to UTC
logger = logging.getLogger(__name__)

# Detect environment
env_local_loaded = load_dotenv(dotenv_path='.env.local')
env = os.getenv('ENV', 'development' if env_local_loaded else 'production')
logger.info(f'Starting in {env} mode')

# Log critical env vars at startup to detect misconfiguration early
_volume_path = os.getenv('COMPARE_VOLUME_PATH', '')
_lakebase_id = os.getenv('LAKEBASE_PROJECT_ID', '')
_app_version  = os.getenv('APP_VERSION', '1')
logger.info(f'COMPARE_VOLUME_PATH = {_volume_path!r}')
logger.info(f'LAKEBASE_PROJECT_ID  = {_lakebase_id!r}')
logger.info(f'APP_VERSION          = {_app_version!r}')
if not _volume_path:
    logger.warning('COMPARE_VOLUME_PATH is not set — file save/restore disabled. '
                   'Set it via databricks.yml (resources.apps.doc-compare.env) or app.yaml.')


@asynccontextmanager
async def lifespan(app: FastAPI):
  await init_lakebase()
  yield
  await shutdown_lakebase()
  await shutdown_translation_bridge()


app = FastAPI(title='QualiBOT', version='1.0.0', lifespan=lifespan)

# CORS: allow localhost in dev, same-origin only in production
allowed_origins = ['http://localhost:3000'] if env == 'development' else []
app.add_middleware(
  CORSMiddleware,
  allow_origins=allowed_origins,
  allow_credentials=True,
  allow_methods=['*'],
  allow_headers=['*'],
)


@app.middleware('http')
async def log_requests(request: Request, call_next):
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
async def global_exception_handler(request: Request, exc: Exception):
  logger.error(f'Unhandled exception on {request.method} {request.url.path}: {exc}', exc_info=True)
  asyncio.create_task(store_error(
      endpoint=f'{request.method} {request.url.path}',
      error_type=type(exc).__name__,
      error_msg=str(exc),
      stack_trace=traceback.format_exc(),
  ))
  return JSONResponse(status_code=500, content={'error': 'Internal server error'})

API_PREFIX = '/api'
app.include_router(health.router, prefix=API_PREFIX, tags=['health'])
app.include_router(config.router, prefix=API_PREFIX, tags=['config'])
app.include_router(compare.router, prefix=API_PREFIX, tags=['compare'])
app.include_router(exports.router, prefix=API_PREFIX, tags=['exports'])
app.include_router(chat.router, prefix=API_PREFIX, tags=['chat'])
app.include_router(history.router, prefix=API_PREFIX, tags=['history'])
app.include_router(feedback.router, prefix=API_PREFIX, tags=['feedback'])
app.include_router(preview.router, prefix=API_PREFIX, tags=['preview'])

# Serve Vite static build in production
build_path = Path('.') / 'client/out'
if build_path.exists():
  logger.info(f'Serving static files from {build_path}')
  app.mount('/assets', StaticFiles(directory=str(build_path / 'assets')), name='assets')

  for static_dir in ['images', 'logos', 'videos', 'content']:
    dir_path = build_path / static_dir
    if dir_path.exists():
      app.mount(f'/{static_dir}', StaticFiles(directory=str(dir_path)), name=static_dir)

  @app.get('/{full_path:path}')
  async def serve_spa(request: Request, full_path: str):
    """SPA catch-all: serve index.html for any non-API route."""
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
