"""Compare API: document diff, impact search, single-document summary and volume sessions."""

from fastapi import APIRouter

from . import analyze, impact, summarize, volume

router = APIRouter()
for _module in (analyze, impact, summarize, volume):
    router.include_router(_module.router)
