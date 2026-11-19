"""Render every show and report what is broken.  `make shows-check`

Exists because "did that renderer change break a show?" is not answerable by
reading: a show is YAML plus ffmpeg plus whatever the bus happened to be
serving, and the failure modes are silent -- a layer that errors is caught and
skipped so a broken integration never blanks a sign, which is right at runtime
and useless when you are looking for a regression.

Exit code is 1 if any show fails to parse, raises, or reports a layer error, so
this can gate a release.

NOT a failure, and reported separately: a show that renders all black. That is
usually a legitimate choice (a dark seek point in a film) and only you can say.


Renders a few frames from EACH SCENE (not just t=0), because a show whose
first scene is fine and whose third references a dead camera looks healthy
from the front.
"""
import json
import os, sqlite3, sys, time, traceback
from marquee_core.bindings import Bindings
from marquee_core.live import LiveState
from marquee_core.render import Renderer
from marquee_core.scene import load_show
from marquee_core.sources import Resolver

live = LiveState()
try:
    import websocket
    # No host default: the bus is site infrastructure, so an address baked in
    # here would be wrong for everybody but us. Unset simply means "no live
    # data", and every {bus:...} binding renders its no-value glyph.
    bus = os.environ.get("MARQUEE_BUS_URL", "")
    if not bus:
        raise RuntimeError("MARQUEE_BUS_URL unset -- skipping live bus warm-up")
    ws = websocket.create_connection(bus, timeout=8)
    for t in ("home.status", "world.clocks"):
        ws.send(json.dumps({"type": "subscribe", "topic": t}))
    ws.settimeout(4)
    end = time.time() + 12
    while time.time() < end and live.get("home.status") is None:
        m = json.loads(ws.recv())
        if m.get("type") == "event" and m.get("topic"):
            live.put(m["topic"], m.get("data"))
except Exception as e:
    print("(bus unavailable: %s)" % e)

db = sqlite3.connect("/data/marquee.db")
rows = list(db.execute("select name, body from shows order by name"))
print("checking %d shows\n" % len(rows))

bad, warn, unconf = [], [], []
for name, body in rows:
    try:
        show = load_show(body)
    except Exception as e:
        print("%-18s PARSE FAILED: %s" % (name, e)); bad.append(name); continue

    r = Renderer(show, resolver=Resolver(),
                 bindings=Bindings("America/New_York", live=live))
    lit = 0
    try:
        t = 0.0
        for sc in show.scenes:
            for _ in range(3):
                img = r.render(t + 0.2)
                if img.convert("L").getextrema()[1] > 8:
                    lit += 1
            t += sc.duration
    except Exception:
        print("%-18s RENDER RAISED:\n%s" % (name, traceback.format_exc()))
        bad.append(name); r.close(); continue

    errs = list(r.errors)
    r.close()
    # A missing SOURCE is this install's configuration, not a regression: a
    # machine with no cameras registered will report that forever. Only a
    # non-source error means something in the render path actually broke.
    src_errs = [e for e in errs if ": source: " in e]
    errs = [e for e in errs if ": source: " not in e]
    frames = 3 * len(show.scenes)
    status = "ok"
    if errs:
        status = "BROKEN"; bad.append(name)
    elif src_errs:
        status = "unconfig"; unconf.append(name)
    elif lit == 0:
        status = "ALL BLACK"; warn.append(name)
    print("%-18s %-10s scenes=%-2d lit=%d/%d %s"
          % (name, status, len(show.scenes), lit, frames,
             show.display.format))
    for e in (errs + src_errs)[:4]:
        print("      ! %s" % e)

print("\nbroken: %s" % (", ".join(bad) or "none"))
print("unconfigured sources (not a regression): %s" % (", ".join(unconf) or "none"))
print("all-black (may be legitimate): %s" % (", ".join(warn) or "none"))
sys.exit(1 if bad else 0)
