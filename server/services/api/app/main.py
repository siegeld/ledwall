from fastapi import FastAPI

from .db import init_db, SessionLocal
from .resolver import start_bus, start_providers
from .routers import (admin, chain, controller, bitstream, auth, firmware, fleet, health, preview,
                      providers, ws)
from .version import get_version

app = FastAPI(title="Marquee", version=get_version(),
              description="LED wall + receiver-card fleet, and content control")
app.include_router(health.router)
app.include_router(auth.router)
app.include_router(fleet.router)
app.include_router(controller.router)
app.include_router(chain.router)
app.include_router(bitstream.router)
app.include_router(firmware.router)
app.include_router(preview.router)
app.include_router(admin.router)
app.include_router(providers.router)
app.include_router(ws.router)


@app.on_event("startup")
async def _start_push():
    import asyncio
    asyncio.get_running_loop().create_task(ws.watch())


@app.on_event("startup")
def _startup():
    init_db()
    start_bus()
    start_providers()
    with SessionLocal() as db:
        auth.ensure_admin(db)
    # Keep every card on its assigned image and its flash survival copy equal
    # to it (app/fwsync.py). One per fleet: MARQUEE_FIRMWARE_SYNC=false on a
    # second marquee against a copy of the database.
    from .fwsync import SYNC
    SYNC.start()
