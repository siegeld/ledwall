# Third-party components

This project is BSD-2-Clause ([LICENSE](LICENSE)). **Nothing third-party is
vendored here** — every dependency is installed from its own source at image
build time and keeps its own terms.

## Python

| Component | Licence |
|---|---|
| FastAPI, Starlette, Pydantic | MIT |
| SQLAlchemy, Alembic | MIT |
| Pillow | MIT-CMU |
| tftpy | MIT |
| websockets, httpx, uvicorn | BSD-3-Clause / MIT |

## Frontend

React, Vite, TanStack Query, Tailwind, lucide-react — all MIT. See
`services/frontend/package.json` for the resolved set.

## ffmpeg — worth reading if you redistribute a build

Marquee **invokes `ffmpeg` as a subprocess** and does not link against it, so
this project's licence is unaffected by which ffmpeg you have.

That is not true of the *image*. A distributed container bundles whatever
ffmpeg build it installed, and ffmpeg is LGPL-2.1+ or GPL-2.0+ **depending on
how it was compiled** — `--enable-gpl` or `--enable-libx264` makes the result
GPL. If you publish images built from this repository, check what your base
image's ffmpeg was configured with and comply with that.
