"""Fleet + content model.

A *wall* is what a viewer sees: one logical display with one picture on it. A
*card* is one receiver board driving part of that wall. A *show* is a scene
document. An *assignment* points a show at a WALL, and a *schedule* flips
assignments by time of day (dayparting -- the thing every real signage CMS does).

**Why a wall is not a card.** Until v0.11.0 those were the same object, called
`Panel`, and that worked only for as long as one board could drive the whole
display. It cannot, for three independent reasons, all of which arrive together
on a fine-pitch wall:

  - a receiver has a fixed number of output connectors (typically eight), so a
    nine-module wall needs two boards however the pixels are counted;
  - its framebuffer is finite -- 327,680 px on the current hardware, about ten
    256x128 modules -- and past that there is nowhere to put the image;
  - its memory bandwidth sets the refresh rate, so halving the pixels a board
    must read roughly doubles what the wall can do (36.8 Hz to 49.1 Hz on a
    nine-module wall split across two).

So a wall owns a canvas, and each card owns a RECTANGLE of it. The renderer
draws the canvas once and each card is sent its own crop. That is also how
commercial LED systems scale, and for the same reasons.

A card does not know it belongs to a wall. It receives a frame sized to its own
region and displays it, which is why this works identically for the HUB75 boards
running today and for newer S-PWM hardware.

Kept deliberately small: walls, cards, shows, schedules, tokens. Anything richer
(campaigns, proof-of-play, per-advertiser reporting) is DOOH product surface this
estate does not need.
"""

from __future__ import annotations

import logging
import os
import re

import datetime as dt
import enum

from sqlalchemy import (Boolean, DateTime, Enum, Float, ForeignKey, Integer,
                        String, Text, UniqueConstraint)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


log = logging.getLogger("marquee.models")


class Base(DeclarativeBase):
    pass


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


# Driver chips a card can be told to drive, mirroring
# `gateware/spwm_chips.py` in the colorlight repo. Kept as a plain list rather
# than an Enum because the authority is that file, not this one -- marquee
# serves the name through and the firmware rejects one it has no table for,
# leaving the bitstream default in place rather than guessing.
DRIVE_TYPES = ("", "icnd1065", "dp3364s", "dp3264s")

# THE MODULE TYPES WE HAVE DRIVEN -- one entry per physical LED module design,
# keyed by a stable slug. This is marquee's panel description: what the stock
# Colorlight firmware reads out of a .rcvbp profile, we keep here and serve in
# the card's boot config. Everything a module needs that is not "which
# connector sits where" belongs in this table, not in a hand-edited YAML:
#
#   module_w/module_h   the glass, in pixels, as ONE connector drives it
#   drive               the S-PWM driver chip (chip table in the firmware);
#                       "" for a HUB75 bit-plane module, which has none
#   group_origins       where each RGB group's strip sits in the module, for a
#                       module whose groups are not stacked (ARGO's P1.25 is
#                       four 128x64 tiles in a 2x2). None = stacked, the
#                       bitstream's default
#   bitstream_preset    the gateware preset (`build.sh --panel`) that drives it
#   board/connector     which receiver card and connector family
#
# Picking a type on a card fills panel_width/panel_height/group_origins/drive
# at serve time, so the layout YAML only has to say grid and J-assignments.
# The authority on each module is docs/PANELS.md in the colorlight repo.
PANEL_TYPES: dict[str, dict] = {
    "hub75-128x64": {
        "label": "HUB75 128x64, 1/32 scan (5A-75E)",
        "board": "5a-75e", "connector": "hub75",
        "module_w": 128, "module_h": 64,
        "drive": "", "group_origins": None,
        "bitstream_preset": "128x64",
        "notes": "The bench railboard and every HUB75 wall. Two RGB groups, stacked.",
    },
    # The key says "linsn" because the module was once misattributed to Linsn;
    # it is ARGO's, maker unidentified. The key is stored on cards, so it stays.
    "linsn-p1.25-256x128-dp3264s": {
        "label": "ARGO P1.25 256x128, DP3264S, HUB320 (E320)",
        "board": "e320", "connector": "hub320",
        "module_w": 256, "module_h": 128,
        "drive": "dp3264s", "group_origins": "128,0 128,64 0,0 0,64",
        "bitstream_preset": "256x128-dp3264s-hub320",
        # OPERATING POINT, measured on glass rather than derived.
        #
        # BOTH MEASURED against the gateware's own underrun counter
        # (`hub75.underruns`, gateware 1.28.0), which counts pixels the
        # serialiser latched before their memory read returned -- every one a
        # wrong pixel on the glass. Swept 2026-10-20, underruns per second:
        #
        #   clk_div 4, throttle 0 ....... 5454     clk_div 6, throttle 0 .. 13.8
        #   clk_div 4, throttle 2 ....... 5319     clk_div 6, throttle 2 .. 17.8
        #                                          clk_div 6, throttle 4 ... 3.0
        #                                          clk_div 6, throttle 8 ... 0.0
        #
        # clk_div is the dominant term: at 8.75 MHz the scan-out starves on its
        # own and spacing the writes barely helps (2%). At 5.83 MHz the reads
        # fit and the remaining losses ARE write contention, which the spacing
        # removes -- 1 underrun in 60 s against 13.8/s, a 700x reduction, with
        # streaming untouched at 19.6 fps and no partial frames.
        #
        # Throttle 8 is one word per nine clocks: a 32,768 px frame still
        # writes in 8.4 ms against a 50 ms frame period at 20 fps.
        #
        # SUPERSEDED BY THE LINE BUFFER (gateware 1.30.0), 2026-10-21.
        #
        # The whole sweep above is a sweep of WORKAROUNDS for one coupling:
        # the scan-out read on demand, so picture correctness depended on
        # memory latency. The line buffer removes the coupling, and with it
        # the reason for the throttle -- so the throttle stops being free and
        # starts costing. It works by deasserting the DMA writer's `ready`
        # after every accepted beat, and the always-ready gate upstream drops
        # a packet whenever `burst_fifo` backs up behind exactly that. Measured
        # on gateware 1.30.0, same show, one writer, ~19.7 fps delivered:
        #
        #   throttle 8 .. underruns 0.00/s, c_dropped 35.0/s, frames_dropped 0.98/s
        #   throttle 0 .. underruns 0.00/s, c_dropped 15.7/s, frames_dropped 0.30/s
        #
        # So the throttle now TRIPLES dropped frames while protecting against
        # starvation that no longer happens. Underruns stayed at exactly zero
        # either way -- the line buffer, not the throttle, is what holds them
        # there.
        #
        # clk_div 4, RE-SWEPT against the line buffer on 2026-10-21 as the note
        # here asked for. It is clean, and the 5454 underruns/s above is a
        # gateware 1.28.0 number that no longer applies:
        #
        #   clk_div 6 ... 44.45 Hz refresh, 0.00 underruns/s
        #   clk_div 4 ... 66.67 Hz refresh, 0.00 underruns/s
        #
        # One variable, 25 s each, two modules streaming at 20 fps, gateware
        # 1.34.0. The buffer removed the read deadline that a faster panel
        # clock used to narrow, which is exactly why the old sweep does not
        # carry over.
        #
        # THIS IS A VISIBLE FIX, not a capacity tweak. 44.45 Hz is BELOW human
        # flicker fusion, and the symptom is selective in a way that points
        # straight at it: dark content beat visibly while bright content sat
        # still. A near-black pixel is lit for a very short pulse each refresh
        # and short pulses at 44 Hz are seen; bright pixels are lit long enough
        # to fuse. On the rail board the near-black header gradient (RGB 5,7,13
        # to 11,21,37) beat while the gold title over it did not -- which is
        # what finally identified this, after content, the sender, the line
        # buffer and the buffer-swap-versus-scan beat had all been ruled out by
        # measurement.
        #
        # It is also 1.5x the panel data capacity, which the three-module and
        # nine-module walls need.
        #
        # write_throttle 0 REQUIRES gateware >= 1.30.0. On an earlier
        # bitstream, with no line buffer, 0 measured 13.8 underruns/s -- snow.
        "clk_div": 4, "write_throttle": 0,
        # HOST-side pacing, not a card register: how long the sender waits
        # between the ~68 chunks of a frame. It lives here because it is a
        # property of this MODULE's write bandwidth, and a new card should not
        # have to rediscover it.
        #
        # Measured 2026-10-21 on gateware 1.30.0, throttle 0, 20 fps:
        #
        #   400us -> 27.2 ms of a 50 ms frame, c_dropped 15.7/s, 0.30 frames/s
        #   600us -> 40.8 ms of a 50 ms frame, c_dropped  0.24/s, 0.00 frames/s
        #
        # The chunks of one frame arrive at line rate and must be drained into
        # DRAM through a 512-beat FIFO; crammed into 27 ms they overflow it and
        # the gate discards the remainder of a packet. Spread across 41 ms they
        # do not. The Card column default of 0.0008 is the opposite problem --
        # 68 x 800us = 54.4 ms exceeds the 50 ms frame period and caps the rate
        # at ~18 fps.
        "packet_delay": 0.0006,
        "notes": "Four 128x64 RGB groups in a 2x2: g0 top-right, g1 bottom-right, "
                 "g2 top-left, g3 bottom-left. Needs gateware 1.18+.",
    },
    # Myled's P1.25: a different module from ARGO's on the same connector.
    # Chipone ICND1065L column drivers, ICND1028AP row drivers, and four
    # 128x64 strips STACKED, so the framebuffer is 128 wide and 256 tall.
    # Bring-up record: docs/PANELS.md in the colorlight repo.
    "myled-p1.25-128x256-icnd1065": {
        "label": "Myled P1.25 128x256, ICND1065L, HUB320 (E320)",
        "board": "e320", "connector": "hub320",
        "module_w": 128, "module_h": 256,
        # Spelled out although stacked is the bitstream default: a card moved
        # here from ARGO's bitstream would otherwise keep ARGO's 2x2.
        "drive": "icnd1065", "group_origins": "0,0 0,64 0,128 0,192",
        # ITS OWN BITSTREAM, not ARGO's. The row fetch is ordered at build
        # time from where the strips sit; on ARGO's bitstream this module
        # filled a row in ~7800 clocks of 8192 and flickered whenever frames
        # arrived (2000-5000 underruns/s while streaming, 0 idle, 2026-11-15).
        "bitstream_preset": "128x256-icnd1065l-hub320",
        "clk_div": 4, "write_throttle": 0,
        "packet_delay": 0.0006,
        # The module hangs LANDSCAPE, 256 wide x 128 tall, the framebuffer
        # turned 90 clockwise. Seen on glass 2026-11-18. The vendor's receiver
        # profile reads as 270 (logical row r, column c of a group at scan
        # line c, chain column 127 - r), and 270 hung upside down: our groups
        # run in the other order from the profile's.
        "rotate": 90,
        # The vendor's per-colour registers, from its receiver profile: a
        # white balance (our chip table drove red ~65% and blue ~75% harder
        # against green) at about 63% of our table's brightness. All but
        # 0x02-0x04, which set the scan and grey range the stage locks with.
        # Seen on glass 2026-11-18 (colorlight docs/PANELS.md).
        "chip_regs": [[0x07, 0x10, 0x10, 0x10], [0x0c, 0x01, 0x0e, 0x0e],
                      [0x0d, 0x01, 0x05, 0x03], [0x0f, 0x01, 0x01, 0x11],
                      [0x19, 0x07, 0x08, 0x0b], [0x1c, 0x25, 0x3d, 0x26],
                      [0x1d, 0x86, 0xc6, 0xaa], [0x1e, 0xa1, 0xb3, 0x85],
                      [0x20, 0x40, 0x40, 0x60], [0x23, 0x40, 0x43, 0x40]],
        "notes": "Four 128x64 RGB groups stacked top to bottom. Needs gateware "
                 "3.5.0+ built for this preset (3.6.2 / firmware 5.8.1 seen on glass). Pins 20/21 carry the module's "
                 "calibration-flash bus: never run it on gateware older than 3.1.0.",
    },
}

# --- module types from files ------------------------------------------------
#
# A module type is DATA: geometry, group arrangement, chip, operating point and
# (optionally) the chip registers a vendor's receiver profile carries. The ones
# above are the modules this project has driven itself. Any other is a JSON
# file -- one type per file, the filename is the key -- which is what
# `tools/rcvbp_import.py` in the colorlight repo writes from a `.rcvbp`.
#
#   rotate      how the module is meant to be MOUNTED, degrees clockwise from
#               its framebuffer (0/90/180/270). A new card of this type takes
#               it as its own `rotate` unless told otherwise.
#   chip_regs   [[addr, r, g, b], ...]  overrides of the chip's register table,
#               by register address, per colour line. Served as `chip_reg:`
#               lines; needs card firmware 5.6.0+ (older firmware ignores them).
#   group_columns, group_scan
#               one RGB group's size, columns (a multiple of 16) and scan
#               lines (even), for a card on a runtime-geometry bitstream
#               (colorlight gateware 3.6.x, firmware 5.8.0+): one bitstream
#               then serves every module no bigger than it was built for.
#               Served as `group_columns:` / `group_scan:`; other firmware
#               ignores them, and the card refuses a size it cannot scan
#               (`geom_refused` in its status).
MODULE_TYPE_KEYS = ("label", "board", "connector", "module_w", "module_h",
                    "drive", "group_origins", "bitstream_preset", "notes")


def module_type_dirs() -> list[str]:
    """Where module-type files are looked for, lowest precedence first.

    The same three places as data providers, for the same reasons: the
    reviewed home under `custom/<site>/`, and `/data/modules` as the drop-in
    that needs no image rebuild.
    """
    override = os.environ.get("MARQUEE_MODULE_DIRS", "")
    if override:
        return [p.strip() for p in override.split(":") if p.strip()]
    site = os.environ.get("MARQUEE_SITE", "dashboard")
    return ["/custom/example/modules", f"/custom/{site}/modules", "/data/modules"]


def check_module_type(key: str, pt: dict) -> dict:
    """Validate one module type; return it normalised or raise ValueError.

    Strict on purpose. A type is served to a card as its boot config, and a
    card given a wrong register drives real LEDs with it: a file that is not
    exactly right is refused whole, never half-applied.
    """
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{1,63}", key):
        raise ValueError(f"bad module type key {key!r}")
    if not isinstance(pt, dict):
        raise ValueError("a module type is a JSON object")
    for k in MODULE_TYPE_KEYS:
        if k not in pt:
            raise ValueError(f"missing {k!r}")
    for k in ("module_w", "module_h"):
        if not isinstance(pt[k], int) or isinstance(pt[k], bool) or not 0 < pt[k] <= 4096:
            raise ValueError(f"{k} must be a whole number of pixels")
    for k in ("label", "board", "connector", "drive", "bitstream_preset", "notes"):
        if not isinstance(pt[k], str) or "\n" in pt[k] or "\r" in pt[k]:
            raise ValueError(f"{k} must be one line of text")
    go = pt["group_origins"]
    if go is not None and not (isinstance(go, str)
                               and re.fullmatch(r"\d+,\d+( \d+,\d+)*", go)):
        raise ValueError("group_origins is 'x,y x,y ...' or null")
    if not re.fullmatch(r"[a-z0-9]*", pt["drive"]):
        raise ValueError("drive is a chip name")
    if pt.get("rotate") is not None and pt["rotate"] not in (0, 90, 180, 270):
        raise ValueError("rotate is 0, 90, 180 or 270")
    # The card's own limits (colorlight firmware `set_geometry`): both fit an
    # 8-bit register, so 256 at most.
    for k, step in (("group_columns", 16), ("group_scan", 2)):
        v = pt.get(k)
        if v is not None and (not isinstance(v, int) or isinstance(v, bool)
                              or not 0 < v <= 256 or v % step):
            raise ValueError(f"{k} must be a multiple of {step}, up to 256")
    gc, gs = pt.get("group_columns"), pt.get("group_scan")
    if gc is not None or gs is not None:
        # A size without the arrangement leaves the card on its bitstream's
        # origins, laid out for another size; and the card places a strip in
        # 16-pixel steps, so anything else overlaps its neighbour silently.
        if gc is None or gs is None or not go:
            raise ValueError("group_columns, group_scan and group_origins go together")
        if not pt["drive"]:
            raise ValueError("group_scan needs a drive: the chip is told the scan count")
        origins = [tuple(int(n) for n in o.split(",")) for o in go.split()]
        if gc * gs * len(origins) != pt["module_w"] * pt["module_h"]:
            raise ValueError("group_columns x group_scan x groups is not module_w x module_h")
        for x, y in origins:
            if x % 16 or y % 16 or x + gc > pt["module_w"] or y + gs > pt["module_h"]:
                raise ValueError(f"group origin {x},{y} is off the 16-pixel grid or outside the module")
    for k, lo, hi in (("clk_div", 2, 255), ("write_throttle", 0, 15)):
        v = pt.get(k)
        if v is not None and (not isinstance(v, int) or isinstance(v, bool)
                              or not lo <= v <= hi):
            raise ValueError(f"{k} must be {lo}..{hi}")
    pd = pt.get("packet_delay")
    if pd is not None and (not isinstance(pd, (int, float)) or isinstance(pd, bool)
                           or not 0 <= pd <= 0.1):
        raise ValueError("packet_delay is seconds, 0..0.1")
    regs = pt.get("chip_regs")
    if regs is not None:
        # 48 is the firmware's MAX_CHIP_REGS; more would be dropped silently.
        if not isinstance(regs, list) or len(regs) > 48:
            raise ValueError("chip_regs is a list of at most 48 [addr, r, g, b]")
        seen = set()
        for row in regs:
            if (not isinstance(row, list) or len(row) != 4
                    or any(not isinstance(v, int) or isinstance(v, bool)
                           or not 0 <= v <= 255 for v in row)):
                raise ValueError(f"chip_regs row {row!r} is not four bytes")
            if row[0] in seen:
                raise ValueError(f"chip_regs names register 0x{row[0]:02x} twice")
            seen.add(row[0])
        if regs and not pt["drive"]:
            raise ValueError("chip_regs needs a drive (the chip whose table they patch)")
    return pt


def load_module_types(dirs: list[str] | None = None) -> dict[str, str]:
    """Merge module-type files into PANEL_TYPES. Returns {key: problem} for
    the files that were refused, so a caller can show them.

    A file never replaces a built-in type: the built-ins are the measured
    ones, and a card already using one must not change under it because a
    file with the same name appeared.
    """
    import json
    problems: dict[str, str] = {}
    for d in (module_type_dirs() if dirs is None else dirs):
        try:
            names = sorted(n for n in os.listdir(d) if n.endswith(".json"))
        except OSError:
            continue
        for n in names:
            key = n[:-5]
            try:
                if key in _BUILTIN_TYPES:
                    raise ValueError("a built-in type has this name")
                with open(os.path.join(d, n), encoding="utf-8") as f:
                    pt = check_module_type(key, json.load(f))
                PANEL_TYPES[key] = {**pt, "source": os.path.join(d, n)}
                problems.pop(key, None)
            except (ValueError, OSError) as e:
                problems[key] = str(e)
                log.warning("module type %s/%s refused: %s", d, n, e)
    return problems


_BUILTIN_TYPES = frozenset(PANEL_TYPES)
MODULE_TYPE_PROBLEMS = load_module_types()


# Which logical colour each HUB75 pin group carries, mirroring RGB_ORDERS in
# the firmware's layout.rs and the gateware's rgb_order CSR (same order, the
# index IS the CSR value). Named the way LED parts are: the value is what the
# PANEL expects, so "RGB" is standard and "GRB" means the first pin group
# carries green.
#
# Per CARD and not per module: all of a card's outputs run in lockstep off one
# engine, so there is one order for the board. Modules on a card that disagree
# cannot both be right, which is worth knowing before buying them.
RGB_ORDERS = ("RGB", "RBG", "GRB", "GBR", "BRG", "BGR")


class CardState(str, enum.Enum):
    unknown = "unknown"
    online = "online"
    offline = "offline"


# The DB enum is still named panelstate: renaming a SQLite enum means rebuilding
# every table that references it, for no behavioural gain.
PanelState = CardState


class Wall(Base):
    """One logical display: a canvas, and the cards that cover it.

    Width and height are the WALL's pixel dimensions, which may be larger than
    any single card can drive. Cards are placed into it by their `x`/`y` origin.
    """

    __tablename__ = "walls"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    width: Mapped[int] = mapped_column(Integer, default=256)
    height: Mapped[int] = mapped_column(Integer, default=128)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str | None] = mapped_column(Text)
    created: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)

    cards: Mapped[list["Card"]] = relationship(
        back_populates="wall", cascade="all, delete-orphan",
        order_by="(Card.y, Card.x)")
    assignments: Mapped[list["Assignment"]] = relationship(
        back_populates="wall", cascade="all, delete-orphan")

    def covers(self) -> int:
        """Pixels covered by cards. Less than width*height means a gap."""
        return sum(c.width * c.height for c in self.cards if c.enabled)


class Card(Base):
    """One receiver board. Addressed by IP; identified for TFTP by MAC.

    `x`/`y` place this card's top-left corner within its wall. `width`/`height`
    are the card's own output size -- what it is actually driving -- so the
    region it is sent is exactly `(x, y, x + width, y + height)` of the wall
    canvas.
    """

    __tablename__ = "cards"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    wall_id: Mapped[int] = mapped_column(
        ForeignKey("walls.id", ondelete="CASCADE"), index=True)
    host: Mapped[str] = mapped_column(String(64))                    # ip or dns name
    port: Mapped[int] = mapped_column(Integer, default=7000)
    mac: Mapped[str | None] = mapped_column(String(32), unique=True) # for TFTP boot
    # Position in a card chain. 0 is a card on the switch: a lone card or the
    # HEAD of a chain. h >= 1 is the h-th card behind the head, which has no IP
    # of its own: `host` is the head's address and the card answers on its
    # own ports there -- pixels on 7000 + h, HTTP on 8000 + h. Use `api` and
    # `pixel_port` rather than host/port so a chained card is reached right.
    chain_hop: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    width: Mapped[int] = mapped_column(Integer, default=256)
    height: Mapped[int] = mapped_column(Integer, default=128)
    # Origin within the wall canvas. A single-card wall is (0, 0).
    x: Mapped[int] = mapped_column(Integer, default=0)
    y: Mapped[int] = mapped_column(Integer, default=0)
    # Card-to-grid mapping, served to the card over TFTP as its <mac>.yml.
    layout_yaml: Mapped[str | None] = mapped_column(Text)
    firmware: Mapped[str | None] = mapped_column(String(128))        # boot.bin to serve
    # The FPGA image pushed into this card's SPI flash. NOT the same thing as
    # `firmware`: firmware is refetched over TFTP on every boot and never stored
    # on the card, while the bitstream lives in flash and is live only after a
    # power cycle. One column could not answer "what is this card running?".
    bitstream: Mapped[str | None] = mapped_column(String(128))       # .bit pushed
    # What drives this card. "stream" (default) receives frames; "program" runs
    # an on-card program and must NOT be streamed to -- the two fight over the
    # framebuffer. Served to the card in its TFTP config so it survives a
    # reboot, and read here so the player knows to stay off it.
    mode: Mapped[str] = mapped_column(String(16), default="stream", server_default="stream")
    program: Mapped[str | None] = mapped_column(String(64))
    # Which LED driver chip the modules on this card carry. Served in the boot
    # config so the card loads the matching register table.
    #
    # It is NOT a property marquee can change on its own: a card running a
    # HUB75 bit-plane bitstream has no chip table at all, and switching between
    # HUB75 and S-PWM is a different bitstream, not a setting. What this selects
    # is which S-PWM chip an S-PWM bitstream is talking to. Empty means "use
    # whatever the bitstream was built with", which lights rather than showing
    # nothing.
    drive: Mapped[str | None] = mapped_column(String(32))
    # Channel order the MODULES on this card expect, one of RGB_ORDERS. Empty
    # or "RGB" is standard and is not sent to the card at all. Served in the
    # boot config so it survives a reboot, exactly like mode, program and
    # drive. The card also takes it live over /api/rgborder, which is how you
    # FIND the right one; this is where the answer is recorded.
    rgb_order: Mapped[str | None] = mapped_column(String(8))
    # Which MODULE type is on this card's connectors -- a key of PANEL_TYPES.
    # Fills the geometry, group arrangement and driver chip in the boot config
    # from the table, so the YAML carries only the connector assignments and
    # the same module never has its numbers typed twice. Empty keeps the old
    # behaviour: whatever the YAML says, byte for byte.
    panel_type: Mapped[str | None] = mapped_column(String(64))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # Packet spacing in seconds. Per-card because it depends on whether that
    # board runs the gateware pixel DMA or the slower CPU path.
    packet_delay: Mapped[float] = mapped_column(Float, default=0.0008)
    # How the card's modules are MOUNTED: degrees clockwise from the way the
    # card's framebuffer lies (0, 90, 180, 270). `width` x `height` stay the
    # framebuffer's -- what the card and its layout know -- and the card's
    # place on the wall is that rectangle turned (`footprint`). The sender
    # turns each frame the other way, so the picture is upright on the wall.
    # Streamed frames are turned by the sender; what the card draws itself
    # (programs, test patterns) is turned by its firmware, which reads the
    # same number from the boot config (`rotate:`, firmware 5.7.0+).
    rotate: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    state: Mapped[CardState] = mapped_column(Enum(CardState), default=CardState.unknown)
    last_seen: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    created: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)

    wall: Mapped[Wall] = relationship(back_populates="cards")

    @property
    def api(self) -> str:
        """host[:port] of this card's HTTP API -- unique per card, so it is
        also the key for anything tracked per card. Chained cards share `host`."""
        return self.host if not self.chain_hop else f"{self.host}:{8000 + self.chain_hop}"

    @property
    def pixel_port(self) -> int:
        """UDP port this card takes pixels on. Fixed by the hop for a chained
        card; a card on the switch keeps whatever `port` says."""
        return 7000 + self.chain_hop if self.chain_hop else self.port

    @property
    def footprint(self) -> tuple[int, int]:
        """(width, height) this card covers ON THE WALL: its framebuffer's,
        swapped when the modules are mounted a quarter turn."""
        if (self.rotate or 0) in (90, 270):
            return (self.height, self.width)
        return (self.width, self.height)

    @property
    def box(self) -> tuple[int, int, int, int]:
        """This card's crop rectangle in wall coordinates, for PIL."""
        fw, fh = self.footprint
        return (self.x, self.y, self.x + fw, self.y + fh)


class Show(Base):
    """A scene document. `body` is the YAML the scene language parses."""

    __tablename__ = "shows"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    body: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    updated: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now)
    created: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Assignment(Base):
    """What a WALL plays when nothing scheduled overrides it.

    On the wall, not on the card: two cards showing halves of one picture must
    play the same show, and making that a property of the wall removes the
    possibility of them disagreeing.
    """

    __tablename__ = "assignments"
    __table_args__ = (UniqueConstraint("wall_id", name="uq_assignment_wall"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    wall_id: Mapped[int] = mapped_column(ForeignKey("walls.id", ondelete="CASCADE"))
    show_id: Mapped[int] = mapped_column(ForeignKey("shows.id", ondelete="CASCADE"))
    wall: Mapped[Wall] = relationship(back_populates="assignments")
    show: Mapped[Show] = relationship()


class Schedule(Base):
    """Dayparting: play `show` on `wall` between start and end, on given days.

    Times are LOCAL wall-clock (HH:MM) in the configured site zone, stored as
    minutes past midnight. Wall-clock is what an operator means -- "the lobby
    switches to the evening loop at 18:00" must stay at 18:00 across a DST
    change, which a stored UTC instant would not.
    """

    __tablename__ = "schedules"
    id: Mapped[int] = mapped_column(primary_key=True)
    wall_id: Mapped[int] = mapped_column(ForeignKey("walls.id", ondelete="CASCADE"))
    show_id: Mapped[int] = mapped_column(ForeignKey("shows.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(64))
    start_min: Mapped[int] = mapped_column(Integer)   # 0..1439
    end_min: Mapped[int] = mapped_column(Integer)     # exclusive; may wrap past midnight
    days: Mapped[str] = mapped_column(String(16), default="0123456")  # 0=Mon
    priority: Mapped[int] = mapped_column(Integer, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    wall: Mapped[Wall] = relationship()
    show: Mapped[Show] = relationship()

    def covers(self, when: dt.datetime) -> bool:
        if not self.enabled:
            return False
        if str(when.weekday()) not in self.days:
            return False
        m = when.hour * 60 + when.minute
        if self.start_min <= self.end_min:
            return self.start_min <= m < self.end_min
        # Wraps midnight (e.g. 22:00 -> 06:00).
        return m >= self.start_min or m < self.end_min


class CardStat(Base):
    """A sample of a card's counters, for historical performance analysis.

    The card exposes monotonic counters (isr_count, refresh_count, dma_pixels,
    mac_overflow, crc errors). Storing raw counters rather than rates keeps the
    sample honest across reboots -- a counter going BACKWARDS is itself the
    signal that the board restarted, which a pre-computed rate would hide.

    Per CARD, not per wall: these are properties of a board, and on a two-card
    wall it is precisely the difference between them that tells you which board
    is struggling.
    """

    __tablename__ = "card_stats"
    id: Mapped[int] = mapped_column(primary_key=True)
    card_id: Mapped[int] = mapped_column(
        ForeignKey("cards.id", ondelete="CASCADE"), index=True)
    at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_now, index=True)
    reachable: Mapped[bool] = mapped_column(Boolean, default=True)
    isr_count: Mapped[int | None] = mapped_column(Integer)
    refresh_count: Mapped[int | None] = mapped_column(Integer)
    bitmap_frames: Mapped[int | None] = mapped_column(Integer)
    dma_pixels: Mapped[int | None] = mapped_column(Integer)
    dma_chunks: Mapped[int | None] = mapped_column(Integer)
    mac_overflow: Mapped[int | None] = mapped_column(Integer)
    mac_crc_errors: Mapped[int | None] = mapped_column(Integer)
    # Derived at sample time so the UI never has to diff in the browser.
    fps: Mapped[float | None] = mapped_column(Float)
    refresh_hz: Mapped[float | None] = mapped_column(Float)
    rebooted: Mapped[bool] = mapped_column(Boolean, default=False)


class ApiToken(Base):
    """Scoped, revocable token for machine callers (homelab-app-standard §15)."""

    __tablename__ = "api_tokens"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    prefix: Mapped[str] = mapped_column(String(24))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    scopes: Mapped[str] = mapped_column(String(128), default="read")
    created_by: Mapped[str | None] = mapped_column(String(64))
    created: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    last_used: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    expires: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)


class User(Base):
    """Local + AD-provisioned users (AD is the normal path; local is break-glass)."""

    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    email: Mapped[str | None] = mapped_column(String(128))
    display_name: Mapped[str | None] = mapped_column(String(128))
    password_hash: Mapped[str | None] = mapped_column(String(200))
    auth_source: Mapped[str] = mapped_column(String(16), default="local")
    role: Mapped[str] = mapped_column(String(16), default="viewer")
    last_login: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text)


def normalize_mac(mac: str | None) -> str | None:
    """`aa:bb:cc:dd:ee:ff`, lower case -- the one form a MAC is stored in.

    The card's TFTP request names it lower case with dashes, its own status
    page prints it in capitals, and an operator pastes whichever they saw. A
    MAC stored in any other form misses the TFTP lookup, and a chained card
    then used to be served its HEAD's config.
    """
    if mac is None:
        return None
    m = mac.strip().lower().replace("-", ":").replace(".", "")
    if not m:
        return None
    # Bare hex (`02aabbccddee`) or Cisco dotted (`02aa.bbcc.ddee`): split it,
    # or it is stored as typed and never matches a TFTP request.
    if ":" not in m and len(m) == 12:
        m = ":".join(m[i:i + 2] for i in range(0, 12, 2))
    if not re.fullmatch(r"[0-9a-f]{2}(:[0-9a-f]{2}){5}", m):
        raise ValueError(f"not a MAC address: {mac!r}")
    return m


ROTATIONS = (0, 90, 180, 270)


def to_framebuffer(img, rotate: int):
    """Turn a card's slice of the wall into what its framebuffer must hold.

    `rotate` is how the modules are mounted, degrees clockwise. The content
    goes the other way by the same angle: mounted 90 clockwise, a picture that
    is upright on the wall is 90 COUNTER-clockwise in the framebuffer. PIL's
    ROTATE_n transposes are counter-clockwise, so the names line up. An exact
    transpose -- no resampling, every pixel moves to one place.
    """
    from PIL import Image
    if rotate == 90:
        return img.transpose(Image.ROTATE_90)
    if rotate == 180:
        return img.transpose(Image.ROTATE_180)
    if rotate == 270:
        return img.transpose(Image.ROTATE_270)
    return img


def boot_config(card: Card) -> str:
    """Exactly what the TFTP server serves this card as its `<mac>.yml`.

    ONE implementation, used by the player that serves it and by the API that
    displays it. Two copies of this would drift, and the symptom would be an
    operator reading a config the card never received -- which is the worst
    kind of wrong, because it looks like confirmation.

    `mode`, `program` and `drive` are appended at serve time rather than stored
    in the layout text, so there is one source of truth for each and editing
    the layout cannot accidentally strand a card in program mode.
    """
    cfg = card.layout_yaml or ""
    if cfg and not cfg.endswith("\n"):
        cfg += "\n"
    # The module type's facts come AFTER the layout text: the firmware takes
    # the last value of a repeated key, so a type set on the card wins over a
    # stale number left in the YAML, and a card with no type is unchanged.
    pt = PANEL_TYPES.get(card.panel_type or "")
    if pt:
        cfg += f"# module: {card.panel_type} -- {pt['label']}\n"
        cfg += f"panel_width: {pt['module_w']}\n"
        cfg += f"panel_height: {pt['module_h']}\n"
        if pt["group_origins"]:
            cfg += f"group_origins: {pt['group_origins']}\n"
        # One RGB group's size, for a runtime-geometry bitstream.
        for k in ("group_columns", "group_scan"):
            if pt.get(k) is not None:
                cfg += f"{k}: {pt[k]}\n"
        # Operating point. A per-module measurement, so it travels with the
        # module type rather than being retyped into every card's YAML -- and
        # rather than being a live-only knob that every reboot throws away.
        # `is not None`, NOT truthiness: write_throttle 0 is a real, measured
        # setting (the line buffer made the throttle harmful -- see
        # PANEL_TYPES above), and `if pt.get(...)` silently omits it. That
        # happened to behave correctly only because the gateware CSR resets to
        # 0, i.e. the right value for the wrong reason, and it would have
        # quietly dropped any future "explicit 0 over a non-zero default".
        if pt.get("clk_div") is not None:
            cfg += f"clk_div: {pt['clk_div']}\n"
        if pt.get("write_throttle") is not None:
            cfg += f"write_throttle: {pt['write_throttle']}\n"
        # The module's own driver-chip registers, by address and per colour
        # line, over the chip's built-in table.
        for a, r, g, b in pt.get("chip_regs") or ():
            cfg += f"chip_reg: 0x{a:02x} 0x{r:02x} 0x{g:02x} 0x{b:02x}\n"
    cfg += f"mode: {card.mode or 'stream'}\n"
    if card.program:
        cfg += f"program: {card.program}\n"
    # Where this card sits in its wall, for on-card PROGRAMS: a program draws
    # in wall coordinates and each card renders its own rectangle, so one
    # ticker runs across all the cards instead of restarting on each. Only
    # when the wall is more than this card, so a single-card wall's config is
    # byte-identical to before. (Streaming does not need it: marquee crops.)
    wall = card.wall
    x, y = card.x or 0, card.y or 0
    if wall is not None and ((wall.width, wall.height) != card.footprint
                             or (x, y) != (0, 0)):
        cfg += f"wall: {wall.width}x{wall.height}\n"
        cfg += f"origin: {x},{y}\n"
    # The card's own drive setting overrides the type's; the type's is what a
    # card gets when nobody has said otherwise.
    drive = card.drive or (pt["drive"] if pt else "")
    if drive:
        cfg += f"drive: {drive}\n"
    # Only when it is not the standard order: every existing card omits this,
    # so their configs are byte-identical to before and nothing reboots into a
    # different colour map by surprise.
    if card.rgb_order and card.rgb_order.upper() != "RGB":
        cfg += f"rgb_order: {card.rgb_order}\n"
    # How the modules are mounted, for what the CARD draws itself (programs,
    # test patterns, the version banner): firmware 5.7.0+ turns those to
    # match. Streamed frames are turned here, by the sender. Only when turned,
    # so every other card's config is byte-identical to before.
    if card.rotate:
        cfg += f"rotate: {card.rotate}\n"
    return cfg


def resolve_show_for(wall: Wall, schedules: list[Schedule], when: dt.datetime) -> int | None:
    """Which show should this wall be playing right now?

    Highest-priority covering schedule wins; ties break on the later start so a
    narrower window layered inside a broad one takes effect. Falls back to the
    wall's standing assignment.
    """
    best: Schedule | None = None
    for sc in schedules:
        if not sc.covers(when):
            continue
        if best is None or (sc.priority, sc.start_min) > (best.priority, best.start_min):
            best = sc
    if best:
        return best.show_id
    return wall.assignments[0].show_id if wall.assignments else None
