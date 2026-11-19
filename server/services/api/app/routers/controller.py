"""Reverse proxy to a card's own web page (its "controller").

A card lives on the panel bus, a private segment that only the sender host is
on, so a browser elsewhere cannot reach http://<card>/ directly. This API runs
on the sender (host networking), so it relays: GET/POST
/api/v1/cards/{id}/controller/<path> -> http://<card.api>/<path> -- `api`, not
`host`: a chained card is its head's host on port 8000+hop.

The firmware's page uses absolute paths (fetch('/api/reboot'), href=/api/status),
which would escape the proxy prefix. Rewriting them to RELATIVE paths keeps it
path-agnostic: the page is served at .../controller/, so 'api/x' resolves to
.../controller/api/x wherever Marquee is mounted.
"""

from __future__ import annotations

import re
import urllib.error
import urllib.request

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from marquee_core.models import Card

from ..db import get_db

router = APIRouter(prefix="/api/v1", tags=["controller"])

# A quote or '=' followed by /api/ -- covers fetch('/api/..'), href=/api/..
_ABS_API = re.compile(rb"""(['"=])/api/""")


@router.get("/cards/{cid}/controller", include_in_schema=False)
def controller_slash(cid: int):
    # Relative redirect, so the mount prefix is preserved. The trailing slash
    # matters: it is what makes the page's relative links resolve under it.
    return RedirectResponse("controller/", status_code=307)


@router.api_route("/cards/{cid}/controller/{path:path}", methods=["GET", "POST"])
async def controller(cid: int, path: str, request: Request,
                     db: Session = Depends(get_db)):
    c = db.get(Card, cid)
    if c is None:
        raise HTTPException(404, "card not found")
    url = f"http://{c.api}/{path}"
    if request.url.query:
        url += f"?{request.url.query}"
    body = await request.body() if request.method == "POST" else None
    ctype = request.headers.get("content-type")
    req = urllib.request.Request(url, data=body, method=request.method,
                                 headers={"Content-Type": ctype} if ctype else {})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            status, data, rtype = r.status, r.read(), r.headers.get("content-type", "")
    except urllib.error.HTTPError as e:
        status, data, rtype = e.code, e.read(), e.headers.get("content-type", "")
    except (urllib.error.URLError, OSError) as e:
        raise HTTPException(502, f"card {c.name} at {c.api} unreachable: {e}")
    if "text/html" in rtype:
        data = _ABS_API.sub(rb"\1api/", data)
    return Response(content=data, status_code=status, media_type=rtype or None)
