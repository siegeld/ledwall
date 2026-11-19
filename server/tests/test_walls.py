"""Wall geometry: how one picture is cut up across several receiver cards.

Pure-function tests -- no cards, no network, no DB writes. They exist for the
same reason the wire tests do: every bug this arithmetic produces looks fine in
review and shows up on the glass as something nobody can explain. A crop that is
off by one column shears the seam between two boards; a card whose origin is
wrong shows the wrong half of the picture, which reads as "the left panel is
broken" rather than as a coordinate error.
"""
from __future__ import annotations

import datetime as dt

import pytest
from PIL import Image

from marquee_core.models import Card, Wall


def wall(w=512, h=128, boxes=((0, 0, 256, 128), (256, 0, 256, 128))):
    """A wall with one card per (x, y, width, height)."""
    wl = Wall(id=1, name="test", width=w, height=h, enabled=True)
    wl.cards = [
        Card(id=i + 1, name=f"c{i}", wall_id=1, host=f"10.0.0.{i + 1}",
             x=x, y=y, width=cw, height=ch, enabled=True, mode="stream")
        for i, (x, y, cw, ch) in enumerate(boxes)
    ]
    return wl


def test_box_is_a_pil_crop_rectangle():
    """PIL wants (left, top, right, bottom) -- NOT (left, top, width, height).

    Getting this wrong does not raise: PIL happily crops a differently-sized
    region, so the card receives a frame of the wrong dimensions and the
    firmware renders whatever arrives. Asserting the shape is cheaper than
    recognising it on a wall.
    """
    c = Card(name="c", wall_id=1, host="h", x=256, y=64, width=128, height=32)
    assert c.box == (256, 64, 384, 96)
    left, top, right, bottom = c.box
    assert right - left == c.width
    assert bottom - top == c.height


def test_two_cards_tile_a_wall_exactly():
    w = wall()
    assert w.covers() == w.width * w.height


def test_disabled_card_leaves_a_gap():
    """A disabled card is a hole in the picture, not a smaller picture."""
    w = wall()
    w.cards[1].enabled = False
    assert w.covers() == 256 * 128
    assert w.covers() < w.width * w.height


def test_crops_reassemble_into_the_original():
    """The whole point: cut the canvas up, and nothing is lost or duplicated.

    Built from a gradient rather than a flat fill so that a swapped or shifted
    crop cannot coincidentally match.
    """
    w = wall()
    canvas = Image.new("RGB", (w.width, w.height))
    px = canvas.load()
    for y in range(w.height):
        for x in range(w.width):
            px[x, y] = (x % 256, y % 256, (x * y) % 256)

    rebuilt = Image.new("RGB", (w.width, w.height))
    for c in w.cards:
        piece = canvas.crop(c.box)
        assert piece.size == (c.width, c.height)
        rebuilt.paste(piece, (c.x, c.y))
    assert list(rebuilt.getdata()) == list(canvas.getdata())


def test_each_card_gets_its_own_half_not_the_same_one():
    """The regression that matters: both cards showing the left half.

    A single wrong variable in the render loop -- cropping to `cards[0].box`
    every time -- produces exactly this, and on a wall of identical modules
    showing similar content it is genuinely hard to see.
    """
    w = wall()
    canvas = Image.new("RGB", (w.width, w.height))
    px = canvas.load()
    for y in range(w.height):
        for x in range(w.width):
            px[x, y] = (255, 0, 0) if x < 256 else (0, 0, 255)

    left, right = (canvas.crop(c.box) for c in w.cards)
    assert left.getpixel((0, 0)) == (255, 0, 0)
    assert right.getpixel((0, 0)) == (0, 0, 255)
    assert list(left.getdata()) != list(right.getdata())


def test_vertical_and_grid_layouts():
    """A 3x3 wall of 256x128 modules across two cards, 5 and 4 -- the real case."""
    boxes = []
    for row in range(3):
        for col in range(3):
            boxes.append((col * 256, row * 128, 256, 128))
    w = wall(w=768, h=384, boxes=boxes)
    assert w.covers() == 768 * 384 == 294_912
    # Every module lands inside the canvas.
    for c in w.cards:
        assert c.box[2] <= w.width and c.box[3] <= w.height


def test_single_card_wall_is_the_whole_canvas():
    """The shape every pre-v0.11.0 install migrates into, and it must be a no-op."""
    w = wall(w=128, h=128, boxes=((0, 0, 128, 128),))
    c = w.cards[0]
    assert c.box == (0, 0, 128, 128)
    assert w.covers() == 128 * 128


@pytest.mark.parametrize("boxes,overlaps", [
    (((0, 0, 256, 128), (256, 0, 256, 128)), False),
    (((0, 0, 256, 128), (128, 0, 256, 128)), True),    # horizontal overlap
    (((0, 0, 256, 128), (0, 64, 256, 128)), True),     # vertical overlap
    (((0, 0, 256, 64), (0, 64, 256, 64)), False),      # stacked, touching
])
def test_overlap_detection(boxes, overlaps):
    """Touching is fine; overlapping means two boards render the same pixels."""
    w = wall(w=512, h=192, boxes=boxes)
    a, b = w.cards
    ax0, ay0, ax1, ay1 = a.box
    bx0, by0, bx1, by1 = b.box
    hit = ax0 < bx1 and bx0 < ax1 and ay0 < by1 and by0 < ay1
    assert hit is overlaps


def test_show_is_resolved_on_the_wall_not_the_card():
    """Content belongs to the wall, so both cards cannot disagree about it."""
    from marquee_core.models import Assignment, Schedule, resolve_show_for

    w = wall()
    w.assignments = [Assignment(id=1, wall_id=1, show_id=7)]
    assert resolve_show_for(w, [], dt.datetime(2026, 9, 19, 12, 0)) == 7

    # A covering schedule outranks the standing assignment.
    sc = Schedule(id=1, wall_id=1, show_id=9, name="evening",
                  start_min=18 * 60, end_min=23 * 60, days="0123456",
                  priority=0, enabled=True)
    assert resolve_show_for(w, [sc], dt.datetime(2026, 9, 19, 19, 0)) == 9
    assert resolve_show_for(w, [sc], dt.datetime(2026, 9, 19, 12, 0)) == 7
