"""The Marquee scene language.

A declarative description of what a panel shows, compiled to frames by
`render.py` and streamed by `stream.py`.

The model is the one the digital-signage industry actually uses: a **playlist of
scenes**, each a stack of **layers** in **boxes**. That is what a Times Square
CMS does (Broadsign, Scala, Daktronics) -- the creative is pre-rendered video and
the "language" is the zone/playlist layout around it. Expressing it as text makes
it diffable and reviewable, which a GUI CMS does not.

Deliberately NOT a general-purpose language: no loops, no expressions, no
scripting. A sign that is edited by several people over years is safer as data.
Dynamic content comes from *bindings* (`{clock:%H:%M}`, `{ha:sensor.x}`) which
are resolved at render time, not from code.

Example:

    display: {width: 256, height: 128, fps: 24}
    scenes:
      - name: welcome
        duration: 10s
        layers:
          - {type: video, src: "plex:Home Movies/ski.mp4", box: [0,0,256,64], fit: cover}
          - {type: text,  text: "{clock:%H:%M}", box: [0,72,256,40], size: 28, align: center}
"""

from __future__ import annotations

import re
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# --------------------------------------------------------------------------
# scalars
# --------------------------------------------------------------------------

_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(ms|s|m|h)?\s*$", re.I)
_UNITS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0, None: 1.0}


def parse_duration(v: str | int | float) -> float:
    """'10s' / '500ms' / '2m' / 10 -> seconds.

    Authors write durations, not float seconds; a bare number means seconds so
    the common case stays terse.
    """
    if isinstance(v, (int, float)):
        return float(v)
    m = _DURATION_RE.match(str(v))
    if not m:
        raise ValueError(f"bad duration {v!r} (try '10s', '500ms', '2m')")
    return float(m.group(1)) * _UNITS[(m.group(2) or "").lower() or None]


Duration = Annotated[float, Field(ge=0)]
Color = Annotated[str, Field(pattern=r"^(#[0-9a-fA-F]{3,8}|[a-zA-Z]+)$")]


# A box edge: pixels, or a fraction of the wall.
#
#   128     -> 128 pixels          (an int is always pixels)
#   0.5     -> half the wall       (a float is always a fraction)
#   "50%"   -> half the wall
#
# int-vs-float is the whole rule, and it is why `1` and `1.0` mean different
# things: one pixel, and the whole wall. YAML preserves the distinction, so
# `128` and `0.5` both read naturally and neither needs quoting.
Dim = int | float | str


def _dim(v: "Dim", span: int) -> int:
    """Resolve one edge against the wall's width or height."""
    if isinstance(v, bool):                      # bool is an int; never a size
        raise ValueError("box edge cannot be a boolean")
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(round(v * span))
    t = str(v).strip()
    if t.endswith("%"):
        return int(round(float(t[:-1]) / 100.0 * span))
    return int(round(float(t)))


class Box(BaseModel):
    """A rectangle, in pixels or as a fraction of the wall: [x, y, w, h].

    Fractions are what let ONE show work on more than one wall. A show whose
    boxes are absolute pixels is written for a single geometry: the player
    renders it at the show's declared size and then resizes the canvas to the
    wall, so a 128x128 show on a 768x384 wall is stretched 6x horizontally and
    3x vertically. With fractional boxes the show is rendered at the wall's own
    size and nothing is stretched at all.
    """

    model_config = ConfigDict(extra="forbid")
    x: Dim = 0
    y: Dim = 0
    w: Dim
    h: Dim

    def resolve(self, width: int, height: int) -> "Box":
        """Concrete pixel box for a wall this size. x/w against width, y/h
        against height -- a fraction means the same thing on either axis."""
        return Box(x=_dim(self.x, width), y=_dim(self.y, height),
                   w=_dim(self.w, width), h=_dim(self.h, height))

    @model_validator(mode="before")
    @classmethod
    def _no_booleans(cls, v):
        # bool IS an int in Python and pydantic accepts it as one, so `true`
        # would quietly become a 1-pixel edge rather than an error.
        if isinstance(v, dict):
            for k in ("x", "y", "w", "h"):
                if isinstance(v.get(k), bool):
                    raise ValueError(f"box {k} cannot be a boolean")
        return v

    @classmethod
    def coerce(cls, v):
        if isinstance(v, (list, tuple)):
            if len(v) != 4:
                raise ValueError("box must be [x, y, w, h]")
            if any(isinstance(e, bool) for e in v):
                raise ValueError("box edges cannot be booleans")
            return cls(x=v[0], y=v[1], w=v[2], h=v[3])
        return v


# --------------------------------------------------------------------------
# layers
# --------------------------------------------------------------------------


class LayerBase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    box: Box
    opacity: float = Field(1.0, ge=0, le=1)
    # Higher z draws later (on top). Ties break by declaration order, so the
    # common case needs no z at all.
    z: int = 0

    @field_validator("box", mode="before")
    @classmethod
    def _box(cls, v):
        return Box.coerce(v)


class SolidLayer(LayerBase):
    type: Literal["solid"]
    color: Color = "#000000"


class GradientLayer(LayerBase):
    type: Literal["gradient"]
    start: Color = "#000000"
    end: Color = "#ffffff"
    angle: Literal["h", "v"] = "v"


class ImageLayer(LayerBase):
    type: Literal["image"]
    src: str
    fit: Literal["smart", "cover", "contain", "stretch", "none"] = "contain"


class VideoLayer(LayerBase):
    type: Literal["video"]
    src: str
    fit: Literal["smart", "cover", "contain", "stretch"] = "cover"
    loop: bool = True
    mute: bool = True          # accepted and ignored; a panel has no audio
    start: Duration = 0.0


class TextLayer(LayerBase):
    type: Literal["text"]
    text: str
    size: int = Field(16, ge=4, le=200)
    color: Color = "#ffffff"
    font: str | None = None
    align: Literal["left", "center", "right"] = "left"
    valign: Literal["top", "middle", "bottom"] = "middle"
    # Scrolling is the signature of a real sign; 'auto' scrolls only when the
    # text does not fit, which is what an operator means most of the time.
    scroll: Literal["none", "left", "auto"] = "none"
    speed: float = Field(30.0, gt=0, description="pixels per second when scrolling")
    bold: bool = False


class RowColumn(BaseModel):
    """One column of a `rows` layer.

    `field` is a path INTO each row, not into the topic: the layer already
    addressed the list, so a column says `time`, not
    `mnr.harlem|departures.0.time`. Dotted paths work for nested rows.

    Everything else is the text styling for that column, and omitting it
    inherits the layer's. A departures board is three columns that differ only
    in width, colour and alignment, so inheritance is what keeps the common
    case to one line each.
    """

    model_config = ConfigDict(extra="forbid")
    field: str
    w: Dim
    size: int | None = Field(None, ge=4, le=200)
    color: Color | None = None
    font: str | None = None
    align: Literal["left", "center", "right"] = "left"
    valign: Literal["top", "middle", "bottom"] = "middle"
    scroll: Literal["none", "left", "auto"] = "none"
    speed: float = Field(30.0, gt=0)


class RowsLayer(LayerBase):
    """A table: one row per entry of a list in the live cache.

    ```yaml
    - type: rows
      data: "mnr.harlem|departures"
      box: [1, 17, 126, 90]
      row_height: 15
      columns:
        - {field: time,        w: 31}
        - {field: destination, w: 64, scroll: auto, color: "#C9A84C"}
        - {field: countdown,   w: 29, align: right, color: "#8899aa"}
    ```

    **The row count comes from the DATA.** That is the whole point, and it is
    what a page of hand-written text layers cannot do. Spelling a six-row board
    out by hand bakes the six in: eight departures means writing six more
    layers, moving a column means editing it once per row, and -- worst --
    every row draws whether or not there is a train for it, so a quiet hour
    renders a column of no-value glyphs that look like data. Here, three trains
    draw three rows and the rest of the box stays dark, which is what a station
    board actually does.

    A row that exists but is missing one field still renders that cell's
    no-value glyph: the row is real, the value is not, and hiding that would be
    a lie rather than a blank.

    Rows are capped by what FITS -- `box.h // row_height` -- so the layer can
    never draw outside its own box however long the list grows. `limit` caps it
    lower when you want fewer than fit.

    Columns flow left to right from the accumulated widths, so changing one
    width shifts the rest instead of needing every x recomputed. Give a column
    a fractional `w` and it scales with the wall like any other box edge.
    """

    type: Literal["rows"]
    # "topic|dotted.path" pointing at a LIST -- the same address {data:...}
    # takes. `bus` is the older spelling, as on `sparkline`.
    data: str | None = None
    bus: str | None = None
    columns: list[RowColumn] = Field(default_factory=list)
    row_height: int = Field(14, ge=4, le=400)
    # Horizontal space between columns, in pixels. Vertical spacing is
    # row_height, which INCLUDES the text -- a separate gap would be a second
    # way to say the same thing.
    col_gap: int = Field(2, ge=0, le=64)
    limit: int | None = Field(None, ge=1, le=200)
    # Defaults every column inherits.
    size: int = Field(12, ge=4, le=200)
    color: Color = "#ffffff"
    font: str | None = None

    @model_validator(mode="after")
    def _check(self):
        if bool(self.data) == bool(self.bus):
            raise ValueError("rows needs exactly one of 'data' or 'bus'")
        if not self.columns:
            raise ValueError("rows needs at least one column")
        return self

    @property
    def address(self) -> str:
        """The `topic|path` to read, whichever name it was written under."""
        return self.data or self.bus or ""


class GraphLayer(LayerBase):
    """A chart of one Home Assistant entity's recent history.

    Deliberately minimal. At 128x64 a chart with axes, gridlines and tick labels
    spends most of its pixels on furniture and leaves almost none for the
    signal -- the same lesson HUB75.md records about small text, where the fix
    was an icon rather than a smaller font. So: a line, optionally filled, with
    an optional baseline rule. Label it with a separate text layer if the number
    matters as well as the shape.
    """

    type: Literal["graph"]
    entity: str
    hours: float = Field(24.0, gt=0, le=720)
    color: Color = "#40d080"
    # Filling under the line reads better than a bare 1px trace on an LED wall
    # seen from across a room, where a single lit pixel row is easy to lose.
    fill: bool = False
    # Fixed scale when given. Autoscale is the sensible default for an unknown
    # sensor, but it means a flat signal fills the box with noise -- pin the
    # range when you know it (a thermostat, a percentage).
    min: float | None = None
    max: float | None = None
    baseline: bool = True


class SparklineLayer(LayerBase):
    """A sparkline of a numeric series that is already on the live bus.

    `graph` asks Home Assistant for history over HTTP; this plots a list the
    panel-bus has already pushed -- `{bus:home.status|power.history}` and the
    like. Same drawing code, different source, and three things follow from
    that difference:

      * it needs no Home Assistant credentials, so it works on an install
        where `ha.token` is unset and every `{ha:...}` renders the no-value
        glyph;
      * the render path makes no network call at all, which is the house rule
        for a sign redrawing 20 times a second;
      * the series is whatever the publisher chose to send, so `hours` has no
        meaning here -- the window is the feed's, not ours.

    Non-numeric entries are dropped rather than plotted as zero, for the same
    reason `graph` drops `unavailable`: a cliff to the floor looks like data.
    """

    type: Literal["sparkline"]
    # "topic|dotted.path", the same address a {data:...} binding takes.
    #
    # `data` and `bus` are the same field under two names. A topic in the live
    # cache may be published by the panel-bus OR by a data provider, and once
    # cached the two are indistinguishable -- so naming the field after one
    # transport was wrong. `data` is the spelling to use; `bus` still works
    # because shows in the field are written with it.
    data: str | None = None
    bus: str | None = None
    color: Color = "#40d080"
    fill: bool = False
    min: float | None = None
    max: float | None = None
    baseline: bool = True

    @model_validator(mode="after")
    def _one_address(self):
        if bool(self.data) == bool(self.bus):
            raise ValueError("sparkline needs exactly one of 'data' or 'bus'")
        return self

    @property
    def address(self) -> str:
        """The `topic|path` to read, whichever name it was written under."""
        return self.data or self.bus or ""


Layer = Annotated[
    Union[SolidLayer, GradientLayer, ImageLayer, VideoLayer, TextLayer, GraphLayer,
          SparklineLayer, RowsLayer],
    Field(discriminator="type"),
]


# --------------------------------------------------------------------------
# scenes / show
# --------------------------------------------------------------------------


class Transition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["cut", "crossfade"] = "cut"
    duration: Duration = 0.5

    @field_validator("duration", mode="before")
    @classmethod
    def _dur(cls, v):
        return parse_duration(v)


class Scene(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    duration: Duration = 10.0
    layers: list[Layer] = Field(default_factory=list)
    transition: Transition | None = None

    @field_validator("duration", mode="before")
    @classmethod
    def _dur(cls, v):
        return parse_duration(v)


class Display(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Omit both to render at the WALL's own size. That is what makes a show
    # with fractional boxes portable: nothing is rendered at one size and
    # resized to another, so nothing is stretched. Give them only when a show
    # is deliberately tied to one geometry.
    width: int | None = Field(None, ge=8, le=4096)
    height: int | None = Field(None, ge=8, le=4096)
    fps: float = Field(24.0, gt=0, le=120)
    # Wire format. "rgb" is RGB888, three bytes a pixel, and is the compatible
    # default. "indexed" quantises to 256 colours and sends one byte a pixel,
    # which is ~3x fewer packets and therefore ~3x the frame rate, because the
    # panel is bound by per-interrupt cost rather than by pixels.
    #
    # Deliberately per-SCENE rather than per-panel: it is a property of the
    # CONTENT. Text, charts and flat graphics quantise to 256 colours with no
    # visible loss; photographs and video do not. The same panel can play both.
    #
    # Needs panel firmware that speaks 'B','I' (colorlight >= 2026-09-14).
    format: Literal["rgb", "indexed"] = "rgb"


class Show(BaseModel):
    """A complete, self-contained thing a panel can play."""

    model_config = ConfigDict(extra="forbid")
    display: Display = Field(default_factory=Display)
    transition: Transition = Field(default_factory=Transition)
    scenes: list[Scene]

    @model_validator(mode="after")
    def _check(self):
        if not self.scenes:
            raise ValueError("a show needs at least one scene")
        seen = set()
        for s in self.scenes:
            if s.name in seen:
                raise ValueError(f"duplicate scene name {s.name!r}")
            seen.add(s.name)
            W, H = self.display.width, self.display.height
            for i, l in enumerate(s.layers):
                # An empty box renders nothing at ANY size -- a zero or
                # negative width is zero or negative whether it is pixels or a
                # fraction -- so check it even when the wall size is unknown.
                # 1000x1000 is an arbitrary stand-in; only the sign matters.
                probe = l.box.resolve(W or 1000, H or 1000)
                if probe.w <= 0 or probe.h <= 0:
                    raise ValueError(f"scene {s.name!r} layer {i} has empty box")
                # Catch the mistake that silently renders nothing: a layer
                # entirely outside the canvas. Only checkable when the show
                # pins its own size -- a size-agnostic show is laid out against
                # whatever wall it lands on, and a fraction cannot start
                # outside it.
                if W and H and (probe.x >= W or probe.y >= H):
                    raise ValueError(
                        f"scene {s.name!r} layer {i} box starts outside the "
                        f"{W}x{H} display"
                    )
        return self

    @property
    def total_duration(self) -> float:
        return sum(s.duration for s in self.scenes)


def load_show(text: str) -> Show:
    """Parse YAML (or JSON) into a validated Show."""
    import yaml

    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError("a show must be a mapping with 'scenes'")
    return Show.model_validate(data)
