from fastapi import APIRouter

from ..version import get_version

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok", "version": get_version()}
