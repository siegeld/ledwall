from fastapi import FastAPI

from .db import init_db, SessionLocal
from .resolver import start_bus, start_providers
from .routers import admin, auth, fleet, health, preview, providers, ws
from .version import get_version

app = FastAPI(title="Marquee", version=get_version(),
              description="LED wall + receiver-card fleet, and content control")
app.include_router(health.router)
app.include_router(auth.router)
app.include_router(fleet.router)
app.include_router(preview.router)
app.include_router(admin.router)
app.include_router(providers.router)
app.include_router(ws.router)


@app.on_event("startup")
def _startup():
    init_db()
    start_bus()
    start_providers()
    with SessionLocal() as db:
        auth.ensure_admin(db)
