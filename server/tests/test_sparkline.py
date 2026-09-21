"""The `sparkline` layer: plot a series the live bus already holds.

Covers what separates it from `graph` -- it needs no Home Assistant
credentials, it makes no network call, and it has to survive a feed sending
junk without taking the sign down.
"""

from marquee_core.bindings import Bindings
from marquee_core.live import LiveState
from marquee_core.render import Renderer
from marquee_core.scene import load_show

SHOW = """
display: {width: 64, height: 32, fps: 10}
scenes:
  - name: s
    duration: 10s
    layers:
      - {type: sparkline, bus: "t|v", box: [0, 0, 64, 32], color: "#40d080"}
"""


def _render(live: LiveState):
    return Renderer(load_show(SHOW), bindings=Bindings("UTC", live=live)).render(0.0)


def test_plots_a_bus_series():
    live = LiveState()
    live.put("t", {"v": [0, 5, 10, 5, 0]})
    assert any(px[1] > 40 for px in _render(live).convert("RGB").getdata())


def test_missing_topic_draws_baseline_not_a_crash():
    # A sign must never go down because a feed is late.
    assert _render(LiveState()).size == (64, 32)


def test_non_numeric_entries_are_dropped_not_zeroed():
    """`null` in a feed must not draw a cliff to the floor."""
    flat = LiveState()
    flat.put("t", {"v": [10, 10, 10, 10]})
    holed = LiveState()
    holed.put("t", {"v": [10, None, 10, "unavailable", 10]})
    assert list(_render(flat).convert("RGB").getdata()) == \
           list(_render(holed).convert("RGB").getdata())


def test_a_path_that_is_not_a_list_is_empty_not_an_error():
    live = LiveState()
    live.put("t", {"v": {"not": "a series"}})
    assert _render(live).size == (64, 32)


def test_booleans_are_not_plotted_as_numbers():
    """bool is an int in Python; a feed of flags is not a series."""
    live = LiveState()
    live.put("t", {"v": [True, False, True]})
    empty = LiveState()
    empty.put("t", {"v": []})
    assert list(_render(live).convert("RGB").getdata()) == \
           list(_render(empty).convert("RGB").getdata())
