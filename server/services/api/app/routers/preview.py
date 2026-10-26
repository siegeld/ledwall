"""Server-side rendering for the content editor.

The editor is WYSIWYG because it calls the SAME renderer the player uses -- what
an operator previews is literally what a card receives, not an approximation
drawn in the browser.
"""

from __future__ import annotations

import io

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

from marquee_core.render import Renderer
from marquee_core.scene import load_show

from ..config import settings
from ..resolver import build_bindings, build_resolver

router = APIRouter(prefix="/api/v1", tags=["preview"])


class PreviewIn(BaseModel):
    body: str
    t: float = 0.0
    scale: int = 3
    # The WALL's size, when the show is previewed for a wall. The player renders
    # at the wall's own size (Renderer.render(t, (wall_w, wall_h))), and integer
    # boxes are absolute, so a show authored for a smaller wall leaves the rest
    # of that wall black. Rendering at the show's declared size instead hid
    # exactly that: a 256x256 show previewed complete while a 256x512 wall
    # showed it on half its modules. Omit both for the show's own size.
    width: int | None = Field(None, ge=8, le=4096)
    height: int | None = Field(None, ge=8, le=4096)


@router.post("/preview.png")
def preview(req: PreviewIn):
    try:
        show = load_show(req.body)
    except Exception as e:
        raise HTTPException(422, f"invalid scene document: {e}") from e
    r = Renderer(show, resolver=build_resolver(), bindings=build_bindings(settings.site_tz))
    try:
        size = (req.width, req.height) if req.width and req.height else None
        img = r.render(req.t, size)
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
