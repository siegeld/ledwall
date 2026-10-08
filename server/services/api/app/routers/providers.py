"""Data providers — what is loaded, what it publishes, and whether it is working.

A show author's first question is "what can I bind to?", and until now the only
answer was to read the provider's source. `GET /api/v1/providers` answers it
from the running system: every provider found, the topics it has actually
published, how stale each one is, and the last error if it is failing.

`GET /api/v1/providers/{name}/payload` returns the current payload for a
topic, which is the other half -- knowing `mnr.harlem` exists does not tell you
that the path is `departures.0.destination`. Being able to see the document is
what makes writing the binding a lookup rather than a guess.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from ..resolver import LIVE, PROVIDERS

router = APIRouter(prefix="/api/v1", tags=["providers"])


@router.get("/providers")
async def list_providers() -> dict:
    """Every loaded provider, its health, and the topics it publishes."""
    return {
        "providers": PROVIDERS.catalogue(),
        "dirs": PROVIDERS.dirs,
        # Everything in the cache, whoever wrote it. A provider topic and a
        # panel-bus topic are bound to identically, so listing only provider
        # topics here would hide half of what a show can actually read.
        "topics": [
            {"topic": t, "age_s": LIVE.age(t), "owner": LIVE.owner(t)}
            for t in LIVE.topics()
        ],
    }


@router.get("/providers/topics/{topic}")
async def topic_payload(topic: str, path: str = Query("", description="dotted path")) -> dict:
    """The live payload for one topic, optionally dug into.

    `path` takes the same dotted form a binding does, so an author can try
    `departures.0.destination` here and paste the address that worked straight
    into a show.
    """
    from marquee_core.live import dig

    doc = LIVE.get(topic)
    if doc is None:
        raise HTTPException(404, f"no payload cached for topic {topic!r}")
    value = dig(doc, path) if path else doc
    return {
        "topic": topic,
        "path": path,
        "owner": LIVE.owner(topic),
        "age_s": LIVE.age(topic),
        "binding": "{data:" + topic + (f"|{path}" if path else "") + "}",
        "value": value,
    }
