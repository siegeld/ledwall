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

import datetime as dt
import enum

from sqlalchemy import (Boolean, DateTime, Enum, Float, ForeignKey, Integer,
                        String, Text, UniqueConstraint)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


# Driver chips a card can be told to drive, mirroring
# `gateware/spwm_chips.py` in the colorlight repo. Kept as a plain list rather
# than an Enum because the authority is that file, not this one -- marquee
# serves the name through and the firmware rejects one it has no table for,
# leaving the bitstream default in place rather than guessing.
DRIVE_TYPES = ("", "icnd1065", "dp3364s")

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
    width: Mapped[int] = mapped_column(Integer, default=256)
    height: Mapped[int] = mapped_column(Integer, default=128)
    # Origin within the wall canvas. A single-card wall is (0, 0).
    x: Mapped[int] = mapped_column(Integer, default=0)
    y: Mapped[int] = mapped_column(Integer, default=0)
    # Card-to-grid mapping, served to the card over TFTP as its <mac>.yml.
    layout_yaml: Mapped[str | None] = mapped_column(Text)
    firmware: Mapped[str | None] = mapped_column(String(128))        # boot.bin to serve
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
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # Packet spacing in seconds. Per-card because it depends on whether that
    # board runs the gateware pixel DMA or the slower CPU path.
    packet_delay: Mapped[float] = mapped_column(Float, default=0.0008)
    state: Mapped[CardState] = mapped_column(Enum(CardState), default=CardState.unknown)
    last_seen: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    created: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)

    wall: Mapped[Wall] = relationship(back_populates="cards")

    @property
    def box(self) -> tuple[int, int, int, int]:
        """This card's crop rectangle in wall coordinates, for PIL."""
        return (self.x, self.y, self.x + self.width, self.y + self.height)


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
    cfg += f"mode: {card.mode or 'stream'}\n"
    if card.program:
        cfg += f"program: {card.program}\n"
    if card.drive:
        cfg += f"drive: {card.drive}\n"
    # Only when it is not the standard order: every existing card omits this,
    # so their configs are byte-identical to before and nothing reboots into a
    # different colour map by surprise.
    if card.rgb_order and card.rgb_order.upper() != "RGB":
        cfg += f"rgb_order: {card.rgb_order}\n"
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
