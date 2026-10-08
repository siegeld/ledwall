"""Server-side rendering for the content editor.

The editor is WYSIWYG because it calls the SAME renderer the player uses -- what
an operator previews is literally what a card receives, not an approximation
drawn in the browser.
"""

from __future__ import annotations

import io

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel

from marquee_core.render import Renderer
from marquee_core.scene import load_show

from ..config import settings
from ..resolver import build_bindings, build_resolver

router = APIRouter(prefix="/api/v1", tags=["preview"])


class PreviewIn(BaseModel):
    body: str
    t: float = 0.0
    scale: int = 3


@router.post("/preview.png")
def preview(req: PreviewIn):
    try:
        show = load_show(req.body)
    except Exception as e:
        raise HTTPException(422, f"invalid scene document: {e}") from e
    r = Renderer(show, resolver=build_resolver(), bindings=build_bindings(settings.site_tz))
    try:
        img = r.render(req.t)
        if req.scale > 1:
            # Nearest-neighbour: an operator needs to see the actual pixel grid,
            # not a smoothed impression of it.
            from PIL import Image
            img = img.resize((img.width * req.scale, img.height * req.scale),
                             Image.NEAREST)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return Response(
            content=buf.getvalue(), media_type="image/png",
            headers={"X-Render-Errors": "; ".join(r.errors)[:400] or "none",
                     "Cache-Control": "no-store"},
        )
    finally:
        r.close()
