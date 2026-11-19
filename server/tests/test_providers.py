"""The data-provider plane: discovery, config, failure handling, bindings.

The bar these tests set is the one the plane promises: a provider that is
broken -- syntax error, missing dependency, raising on every poll -- must be
skipped or retried, and must never take the player down or blank a sign.
"""
from __future__ import annotations

import textwrap
import threading
import time

import pytest

from marquee_core.bindings import Bindings
from marquee_core.live import LiveState
from marquee_core.providers import (Provider, ProviderHost, ProviderRunner,
                                    ProviderHealth, discover, load_file)


def write(tmp_path, name, body):
    p = tmp_path / name
    p.write_text(textwrap.dedent(body))
    return str(p)


GOOD = '''
    from marquee_core.providers import Provider

    class Weather(Provider):
        name = "weather"
        interval_s = 5.0
        DEFAULTS = {"city": "London"}

        def topics(self):
            return ["weather.now"]

        def poll(self):
            return {"weather.now": {"city": self.config["city"], "temp": 11}}
'''


# ----- discovery -----------------------------------------------------------

def test_load_file_finds_the_provider(tmp_path):
    path = write(tmp_path, "weather.py", GOOD)
    classes = load_file(path)
    assert [c.name for c in classes] == ["weather"]


def test_base_class_is_not_itself_discovered(tmp_path):
    """`from ... import Provider` must not register Provider as a provider."""
    path = write(tmp_path, "weather.py", GOOD)
    classes = load_file(path)
    assert Provider not in classes


def test_explicit_PROVIDER_wins_over_a_shared_base(tmp_path):
    write(tmp_path, "two.py", '''
        from marquee_core.providers import Provider

        class Base(Provider):
            def poll(self):
                return {}

        class Real(Base):
            name = "real"
            def poll(self):
                return {"real.x": 1}

        PROVIDER = Real
    ''')
    assert [c.name for c in load_file(str(tmp_path / "two.py"))] == ["real"]


def test_underscore_files_are_helpers_not_providers(tmp_path):
    write(tmp_path, "_helper.py", GOOD)
    assert discover([str(tmp_path)]) == []


def test_a_broken_plugin_is_skipped_not_fatal(tmp_path):
    """A syntax error in one file must not stop the others loading."""
    write(tmp_path, "broken.py", "this is not python(")
    write(tmp_path, "weather.py", GOOD)
    found = discover([str(tmp_path)])
    assert [c.name for c, _ in found] == ["weather"]


def test_a_missing_dependency_is_skipped_not_fatal(tmp_path):
    write(tmp_path, "needsdep.py", "import a_package_that_is_not_installed\n")
    write(tmp_path, "weather.py", GOOD)
    found = discover([str(tmp_path)])
    assert [c.name for c, _ in found] == ["weather"]


def test_later_directory_overrides_by_name(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(); b.mkdir()
    write(a, "weather.py", GOOD)
    write(b, "weather.py", GOOD.replace('"temp": 11', '"temp": 99'))
    found = discover([str(a), str(b)])
    assert len(found) == 1
    assert found[0][1].startswith(str(b))


def test_a_missing_directory_is_not_an_error(tmp_path):
    assert discover([str(tmp_path / "nope"), ""]) == []


# ----- config --------------------------------------------------------------

def test_defaults_apply_with_no_settings(tmp_path):
    write(tmp_path, "weather.py", GOOD)
    cls, _ = discover([str(tmp_path)])[0]
    assert cls({}).config["city"] == "London"


def test_settings_override_defaults(tmp_path):
    write(tmp_path, "weather.py", GOOD)
    live = LiveState()
    host = ProviderHost(live, settings={
        "provider.weather.config": '{"city": "Oslo"}'}, dirs=[str(tmp_path)],
        cache_dir=str(tmp_path / "cache"))
    host.start()
    _wait_for(lambda: live.get("weather.now"))
    host.stop()
    assert live.get("weather.now")["city"] == "Oslo"


def test_malformed_config_json_is_ignored_not_fatal(tmp_path):
    write(tmp_path, "weather.py", GOOD)
    live = LiveState()
    host = ProviderHost(live, settings={"provider.weather.config": "{not json"},
                        dirs=[str(tmp_path)], cache_dir=str(tmp_path / "cache"))
    host.start()
    _wait_for(lambda: live.get("weather.now"))
    host.stop()
    assert live.get("weather.now")["city"] == "London"


def test_enabled_false_does_not_run_it(tmp_path):
    write(tmp_path, "weather.py", GOOD)
    live = LiveState()
    host = ProviderHost(live, settings={"provider.weather.enabled": "false"},
                        dirs=[str(tmp_path)], cache_dir=str(tmp_path / "cache"))
    assert host.start() == []
    time.sleep(0.2)
    assert live.get("weather.now") is None
    assert host.health["weather"].running is False


def test_providers_are_opt_out_not_opt_in(tmp_path):
    """Present with no setting at all means running."""
    write(tmp_path, "weather.py", GOOD)
    host = ProviderHost(LiveState(), settings={}, dirs=[str(tmp_path)],
                        cache_dir=str(tmp_path / "cache"))
    assert host.start() == ["weather"]
    host.stop()


# ----- running and failing -------------------------------------------------

def _wait_for(pred, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def test_payload_reaches_the_cache_and_a_binding(tmp_path):
    write(tmp_path, "weather.py", GOOD)
    live = LiveState()
    host = ProviderHost(live, dirs=[str(tmp_path)], cache_dir=str(tmp_path / "cache"))
    host.start()
    assert _wait_for(lambda: live.get("weather.now"))
    host.stop()

    b = Bindings(live=live)
    assert b.resolve("{data:weather.now|temp}") == "11"
    # The legacy spelling reads the same cache -- shows in the field use it.
    assert b.resolve("{bus:weather.now|temp}") == "11"


def test_a_provider_that_always_raises_never_escapes(tmp_path):
    write(tmp_path, "bad.py", '''
        from marquee_core.providers import Provider

        class Bad(Provider):
            name = "bad"
            interval_s = 0.05
            def poll(self):
                raise RuntimeError("upstream on fire")
    ''')
    live = LiveState()
    host = ProviderHost(live, dirs=[str(tmp_path)], cache_dir=str(tmp_path / "cache"))
    host.start()
    health = host.health["bad"]
    assert _wait_for(lambda: health.failures > 0)
    host.stop()
    assert "upstream on fire" in health.last_error
    # and nothing was published
    assert live.topics() == []


def test_a_failing_provider_leaves_the_last_good_payload(tmp_path):
    """A stale train time beats no train time. This is the whole failure policy."""
    write(tmp_path, "flappy.py", '''
        from marquee_core.providers import Provider

        class Flappy(Provider):
            name = "flappy"
            interval_s = 0.05
            n = 0
            def poll(self):
                Flappy.n += 1
                if Flappy.n == 1:
                    return {"flappy.v": {"x": "good"}}
                raise RuntimeError("gone")
    ''')
    live = LiveState()
    host = ProviderHost(live, dirs=[str(tmp_path)], cache_dir=str(tmp_path / "cache"))
    host.start()
    assert _wait_for(lambda: host.health["flappy"].failures > 0)
    host.stop()
    assert live.get("flappy.v") == {"x": "good"}
    assert Bindings(live=live).resolve("{data:flappy.v|x}") == "good"


def test_backoff_grows_with_consecutive_failures(tmp_path):
    p = Provider()
    p.name, p.interval_s = "x", 10.0
    health = ProviderHealth("x")
    r = ProviderRunner(p, LiveState(), health, max_backoff_s=300.0)
    delays = []
    for n in (1, 2, 3, 8):
        health.consecutive_failures = n
        delays.append(min(r.max_backoff_s, p.interval_s * (2 ** min(n, 6))))
    assert delays == [20.0, 40.0, 80.0, 300.0]


def test_a_successful_poll_clears_the_error(tmp_path):
    write(tmp_path, "weather.py", GOOD)
    live = LiveState()
    host = ProviderHost(live, dirs=[str(tmp_path)], cache_dir=str(tmp_path / "cache"))
    host.start()
    assert _wait_for(lambda: live.get("weather.now"))
    host.stop()
    h = host.health["weather"]
    assert h.last_error is None and h.consecutive_failures == 0 and h.polls > 0


# ----- the cache contract --------------------------------------------------

def test_owner_is_recorded_so_two_writers_are_diagnosable():
    live = LiveState()
    live.put("t", 1, owner="bus")
    assert live.owner("t") == "bus"
    live.put("t", 2, owner="provider:x")
    assert live.owner("t") == "provider:x"


def test_bus_remains_the_default_owner():
    live = LiveState()
    live.put("home.status", {"a": 1})
    assert live.owner("home.status") == "bus"


def test_catalogue_reports_topics_and_staleness(tmp_path):
    write(tmp_path, "weather.py", GOOD)
    live = LiveState()
    host = ProviderHost(live, dirs=[str(tmp_path)], cache_dir=str(tmp_path / "cache"))
    host.start()
    assert _wait_for(lambda: live.get("weather.now"))
    host.stop()
    row = next(r for r in host.catalogue() if r["name"] == "weather")
    assert row["published"] == ["weather.now"]
    assert row["topics"][0]["topic"] == "weather.now"
    assert row["topics"][0]["age_s"] is not None
    assert row["source"].endswith("weather.py")


# ----- the sparkline alias -------------------------------------------------

def test_sparkline_accepts_data_and_bus_but_not_both():
    from marquee_core.scene import load_show

    def show(layer):
        return load_show(f"""
            display: {{width: 64, height: 32}}
            scenes:
              - name: s
                layers:
                  - {layer}
        """)

    a = show('{type: sparkline, data: "t|series", box: [0,0,64,32]}')
    assert a.scenes[0].layers[0].address == "t|series"
    b = show('{type: sparkline, bus: "t|series", box: [0,0,64,32]}')
    assert b.scenes[0].layers[0].address == "t|series"
    with pytest.raises(Exception):
        show('{type: sparkline, data: "a|b", bus: "c|d", box: [0,0,64,32]}')
    with pytest.raises(Exception):
        show('{type: sparkline, box: [0,0,64,32]}')


# ----- the rows layer ------------------------------------------------------
#
# The reason this layer exists is the LAST test in this block: a hand-written
# table draws every row whether or not there is data for it, so a quiet hour
# renders a column of no-value glyphs that read as trains.

from marquee_core.render import Renderer
from marquee_core.scene import load_show


def _rows_show(extra="", data="t.list|rows"):
    return load_show(f"""
        display: {{width: 100, height: 60}}
        scenes:
          - name: s
            layers:
              - type: rows
                data: "{data}"
                box: [0, 0, 100, 60]
                row_height: 10
                size: 8
                col_gap: 0
                {extra}
                columns:
                  - {{field: a, w: 50}}
                  - {{field: b, w: 50, align: right}}
    """)


def _live_with(rows):
    live = LiveState()
    live.put("t.list", {"rows": rows}, owner="provider:test")
    return live


def _render(show, live):
    return Renderer(show=show, bindings=Bindings(live=live)).render(0.0)


def _nonblack_bands(img, row_height):
    """Which row slots actually have pixels drawn in them."""
    out = []
    for r in range(img.height // row_height):
        band = img.crop((0, r * row_height, img.width, (r + 1) * row_height))
        out.append(any(p != (0, 0, 0) for p in band.getdata()))
    return out


def test_rows_needs_exactly_one_address():
    with pytest.raises(Exception):
        load_show("""
            display: {width: 50, height: 20}
            scenes:
              - name: s
                layers:
                  - {type: rows, box: [0,0,50,20], columns: [{field: a, w: 10}]}
        """)


def test_rows_needs_a_column():
    with pytest.raises(Exception):
        load_show("""
            display: {width: 50, height: 20}
            scenes:
              - name: s
                layers:
                  - {type: rows, data: "t|r", box: [0,0,50,20], columns: []}
        """)


def test_bus_is_accepted_as_the_older_spelling():
    show = load_show("""
        display: {width: 50, height: 20}
        scenes:
          - name: s
            layers:
              - {type: rows, bus: "t|r", box: [0,0,50,20], columns: [{field: a, w: 10}]}
    """)
    assert show.scenes[0].layers[0].address == "t|r"


def test_row_count_comes_from_the_data():
    """THE point of the layer: three entries draw three rows, not six."""
    live = _live_with([{"a": "one", "b": "1"}, {"a": "two", "b": "2"}, {"a": "three", "b": "3"}])
    img = _render(_rows_show(), live)
    assert _nonblack_bands(img, 10) == [True, True, True, False, False, False]


def test_a_short_list_leaves_the_rest_dark_not_glyphed():
    """The bug the hand-written version had: empty rows that look like data."""
    live = _live_with([{"a": "only", "b": "1"}])
    img = _render(_rows_show(), live)
    assert _nonblack_bands(img, 10) == [True] + [False] * 5


def test_an_empty_list_draws_nothing_and_does_not_raise():
    img = _render(_rows_show(), _live_with([]))
    assert not any(_nonblack_bands(img, 10))


def test_a_topic_that_never_arrived_draws_nothing():
    img = _render(_rows_show(), LiveState())
    assert not any(_nonblack_bands(img, 10))


def test_a_path_that_is_not_a_list_draws_nothing():
    live = LiveState()
    live.put("t.list", {"rows": {"not": "a list"}})
    assert not any(_nonblack_bands(_render(_rows_show(), live), 10))


def test_rows_are_capped_by_what_fits_in_the_box():
    """However long the feed gets, the layer cannot draw outside itself."""
    live = _live_with([{"a": str(i), "b": str(i)} for i in range(50)])
    img = _render(_rows_show(), live)          # 60px tall, row_height 10
    assert _nonblack_bands(img, 10) == [True] * 6
    assert img.size == (100, 60)


def test_limit_caps_lower_than_what_fits():
    live = _live_with([{"a": str(i), "b": str(i)} for i in range(50)])
    img = _render(_rows_show(extra="limit: 2"), live)
    assert _nonblack_bands(img, 10) == [True, True, False, False, False, False]


def test_a_present_row_with_a_missing_field_still_shows_the_glyph():
    """The row is real and the value is not -- that is not the same as no row."""
    from marquee_core.bindings import _FAIL
    live = _live_with([{"a": "here"}])         # no "b"
    img = _render(_rows_show(), live)
    # the right-hand column drew something
    right = img.crop((50, 0, 100, 10))
    assert any(p != (0, 0, 0) for p in right.getdata()), f"expected {_FAIL!r} in column b"


def test_a_dotted_field_reaches_into_a_nested_row():
    live = _live_with([{"a": "x", "b": {"deep": "yes"}}])
    show = load_show("""
        display: {width: 100, height: 20}
        scenes:
          - name: s
            layers:
              - type: rows
                data: "t.list|rows"
                box: [0, 0, 100, 20]
                row_height: 10
                columns:
                  - {field: b.deep, w: 100}
    """)
    assert any(p != (0, 0, 0) for p in _render(show, live).getdata())


def test_columns_flow_from_accumulated_widths():
    """Column 2 starts after column 1's width, so one edit shifts the rest."""
    live = _live_with([{"a": "", "b": "R"}])   # only the right column has ink
    img = _render(_rows_show(), live)
    left_half = img.crop((0, 0, 50, 10))
    assert not any(p != (0, 0, 0) for p in left_half.getdata())
    assert any(p != (0, 0, 0) for p in img.crop((50, 0, 100, 10)).getdata())


def test_numbers_and_booleans_render_readably():
    live = _live_with([{"a": 3.0, "b": True}])
    img = _render(_rows_show(), live)
    assert any(p != (0, 0, 0) for p in img.getdata())


def test_the_shipped_board_is_valid_and_uses_rows():
    import os
    here = os.path.dirname(__file__)
    # In the container `custom/` is mounted at /custom, not beside tests/.
    candidates = [
        "/custom/example/shows/metro-north.yaml",
        os.path.join(here, "..", "custom", "example", "shows", "metro-north.yaml"),
    ]
    path = next((c for c in candidates if os.path.exists(c)), None)
    assert path, f"example show not found in any of {candidates}"
    show = load_show(open(path).read())
    kinds = [l.type for sc in show.scenes for l in sc.layers]
    assert kinds.count("rows") == 3
    # The whole board, three scenes, in fewer layers than one hand-written
    # scene used to take.
    assert sum(len(sc.layers) for sc in show.scenes) < 20


# ----- helper modules beside a provider ------------------------------------
#
# `discover` skips `_`-prefixed files so a provider can keep shared machinery
# beside it. That was documented before it worked: the import failed with
# ModuleNotFoundError because the plugin's own directory was not on sys.path,
# so two providers over one upstream format had to duplicate the parsing.

def test_a_provider_can_import_a_helper_beside_it(tmp_path):
    write(tmp_path, "_shared.py", '''
        GREETING = "from the helper"
    ''')
    write(tmp_path, "uses_helper.py", '''
        from marquee_core.providers import Provider
        from _shared import GREETING

        class Uses(Provider):
            name = "uses"
            interval_s = 0.05
            def poll(self):
                return {"uses.v": GREETING}
    ''')
    live = LiveState()
    host = ProviderHost(live, dirs=[str(tmp_path)], cache_dir=str(tmp_path / "c"))
    assert host.start() == ["uses"]
    assert _wait_for(lambda: live.get("uses.v"))
    host.stop()
    assert live.get("uses.v") == "from the helper"


def test_the_helper_itself_is_not_started_as_a_provider(tmp_path):
    """A shared base class in a `_` file must not run as a provider of its own."""
    write(tmp_path, "_base.py", '''
        from marquee_core.providers import Provider
        class Base(Provider):
            name = "base-should-not-run"
            def poll(self):
                return {"base.v": 1}
    ''')
    write(tmp_path, "real.py", '''
        from _base import Base
        class Real(Base):
            name = "real"
            def poll(self):
                return {"real.v": 2}
    ''')
    host = ProviderHost(LiveState(), dirs=[str(tmp_path)], cache_dir=str(tmp_path / "c"))
    assert host.start() == ["real"]
    host.stop()


def test_the_plugin_dir_does_not_shadow_the_standard_library(tmp_path):
    """Appended to sys.path, not prepended -- a plugin's json.py is not `json`."""
    write(tmp_path, "json.py", "raise RuntimeError('this must never be imported as json')")
    write(tmp_path, "weather.py", GOOD)
    live = LiveState()
    host = ProviderHost(live, dirs=[str(tmp_path)], cache_dir=str(tmp_path / "c"))
    host.start()
    assert _wait_for(lambda: live.get("weather.now"))
    host.stop()
    import json as real_json
    assert real_json.dumps({"a": 1}) == '{"a": 1}'


def test_sys_path_is_restored_after_loading(tmp_path):
    import sys
    write(tmp_path, "weather.py", GOOD)
    before = list(sys.path)
    discover([str(tmp_path)])
    assert sys.path == before


# ----- the shipped rail providers ------------------------------------------
#
# These assert the SHAPE the two examples are meant to demonstrate, without
# touching the network: that both exist, that they share their machinery
# rather than copying it, and that the identity join handles both of the
# opposite conventions the two MTA feeds use.

def _providers_dir():
    import os
    for c in ("/custom/example/providers",
              os.path.join(os.path.dirname(__file__), "..", "custom", "example", "providers")):
        if os.path.isdir(c):
            return c
    return None


def test_all_three_shipped_providers_load():
    d = _providers_dir()
    assert d, "example providers not found"
    names = sorted(c.name for c, _ in discover([d]))
    assert names == ["amtrak", "lirr", "metro-north"]


def test_the_rail_providers_share_one_machinery_module():
    """Two providers over one feed format must not carry two parsers."""
    import os
    d = _providers_dir()
    assert os.path.exists(os.path.join(d, "_gtfs_rail.py"))
    for fn in ("metro_north.py", "lirr.py"):
        body = open(os.path.join(d, fn)).read()
        assert "_gtfs_rail" in body, f"{fn} should import the shared module"
        assert "def _walk" not in body, f"{fn} has its own protobuf reader"
        assert "zipfile" not in body, f"{fn} has its own static-feed loader"


def test_the_two_systems_differ_where_they_are_meant_to():
    d = _providers_dir()
    by = {c.name: c for c, _ in discover([d])}
    mnr, lirr = by["metro-north"], by["lirr"]
    assert mnr.SYSTEM == "mnr" and lirr.SYSTEM == "lirr"
    assert mnr.FEED_URL != lirr.FEED_URL
    # trips.txt resolves 107/107 on LIRR and 0/201 on MNR, and costs 1750 kB
    # there -- so it is per system, not always on.
    assert lirr.TRIP_NAMES is True
    assert mnr.TRIP_NAMES is False


def test_topics_are_namespaced_per_system():
    d = _providers_dir()
    by = {c.name: c for c, _ in discover([d])}
    assert by["metro-north"]({}).topic == "mnr.harlem"
    assert by["lirr"]({}).topic == "lirr.babylon-branch"
    assert by["amtrak"]({}).topic == "amtrak.nyp-bos"


def _load_rail_module():
    import importlib.util, os, sys
    d = _providers_dir()
    sys.path.append(d)
    try:
        spec = importlib.util.spec_from_file_location(
            "gtfs_rail_under_test", os.path.join(d, "_gtfs_rail.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.path.remove(d)


def _pb_varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _pb(fields):
    """Minimal protobuf encoder: {field_no: [bytes|int]} -> wire bytes.

    Real bytes rather than a stubbed parser, so these exercise `_walk` and the
    join together -- which is the pairing that actually broke in the field.
    """
    out = bytearray()
    for num, values in fields.items():
        for v in values:
            if isinstance(v, int):
                out += _pb_varint(num << 3 | 0) + _pb_varint(v)
            else:
                b = v.encode() if isinstance(v, str) else v
                out += _pb_varint(num << 3 | 2) + _pb_varint(len(b)) + b
    return bytes(out)


def test_vehicles_are_indexed_under_both_identity_conventions():
    """The finding that justifies shipping a second example.

    Metro-North joins trip updates to vehicles on FeedEntity.id (203/203, and
    0/203 on trip_id); LIRR is exactly inverted (0/53 and 53/53). Indexing
    under every identifier a vehicle offers is correct on both, where picking
    one reports every train on the other system as "scheduled".
    """
    m = _load_rail_module()

    class Fake(m.RailBoard):
        name = "fake"
        SYSTEM = "fake"

    p = Fake({"line": "x", "station": "y"})

    def vehicle_entity(entity_id, trip_id, label):
        vp = _pb({
            m.VP_TRIP: [_pb({m.TD_TRIP_ID: [trip_id]})],
            m.VP_VEHICLE: [_pb({m.VD_LABEL: [label]})],
            m.VP_CURRENT_STATUS: [1],          # STOPPED_AT
        })
        from marquee_core.providers import Provider  # noqa: F401  (keeps import order clear)
        return m._walk(_pb({m.E_ID: [entity_id], m.E_VEHICLE: [vp]}))

    # Metro-North shape: FeedEntity.id IS the train number; trip_id differs.
    v = p._vehicles([vehicle_entity("3038", "3209015", "3038")], {})
    assert "3038" in v, "must index under the entity id (the Metro-North join)"
    assert "3209015" in v, "must index under the trip id too (the LIRR join)"
    assert v["3038"]["status"] == "stopped"

    # LIRR shape: the entity id carries a _V suffix; trip_id is the shared key.
    v = p._vehicles([vehicle_entity("GO202_26_7713_V", "GO202_26_7713", "7081")], {})
    assert "GO202_26_7713" in v, "the _V suffix must be stripped for the join"
    assert v["GO202_26_7713"]["label"] == "7081", "label is the timetable number"


def test_an_absent_current_status_means_moving_not_incoming():
    """GTFS-realtime defaults current_status to IN_TRANSIT_TO (2), not 0.

    Defaulting it to 0 made a tenth of the fleet look like it was pulling into
    a station it had already left.
    """
    m = _load_rail_module()

    class Fake(m.RailBoard):
        name = "fake2"
        SYSTEM = "fake2"

    vp = _pb({m.VP_TRIP: [_pb({m.TD_TRIP_ID: ["t1"]})]})   # no current_status
    ent = m._walk(_pb({m.E_ID: ["e1"], m.E_VEHICLE: [vp]}))
    v = Fake({"line": "x", "station": "y"})._vehicles([ent], {})
    assert v["t1"]["status"] == "moving"


def test_the_protobuf_walker_round_trips_nested_messages():
    m = _load_rail_module()
    blob = _pb({1: ["hello"], 2: [7], 3: [_pb({1: ["nested"], 2: [42]})]})
    got = m._walk(blob)
    assert got[1][0] == b"hello"
    assert got[2][0] == 7
    inner = m._walk(got[3][0])
    assert inner[1][0] == b"nested" and inner[2][0] == 42


def test_the_shipped_lirr_board_is_valid_and_uses_rows():
    import os
    here = os.path.dirname(__file__)
    candidates = ["/custom/example/shows/lirr.yaml",
                  os.path.join(here, "..", "custom", "example", "shows", "lirr.yaml")]
    path = next((c for c in candidates if os.path.exists(c)), None)
    assert path, f"lirr show not found in any of {candidates}"
    show = load_show(open(path).read())
    kinds = [l.type for sc in show.scenes for l in sc.layers]
    assert kinds.count("rows") == 3
    body = open(path).read()
    assert "lirr.babylon-branch" in body and "mnr.harlem" not in body


# ----- the Amtrak board: a different upstream, the same payload ------------
#
# metro_north and lirr are two configs of one machinery module because they are
# one feed format. Amtrak is not: it publishes no GTFS feed at all, so its
# provider reads a JSON API and shares only the presentation base. These assert
# that split holds, because the temptation to unify parsing is what would turn
# the base class into a pile of flags.

def _load_amtrak():
    import importlib.util, os, sys
    d = _providers_dir()
    sys.path.append(d)
    try:
        spec = importlib.util.spec_from_file_location("amtrak_under_test",
                                                      os.path.join(d, "amtrak.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.path.remove(d)


def _imports(path):
    """Module names a file actually imports — not names its prose mentions."""
    import ast
    names = set()
    for node in ast.walk(ast.parse(open(path).read())):
        if isinstance(node, ast.Import):
            names |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


def test_amtrak_shares_presentation_but_not_parsing():
    import os
    d = _providers_dir()
    imp = _imports(os.path.join(d, "amtrak.py"))
    assert "_board" in imp, "should share the presentation base"
    assert "_gtfs_rail" not in imp, "must NOT drag in the GTFS machinery"
    assert "zipfile" not in imp
    body = open(os.path.join(d, "amtrak.py")).read()
    assert "def _walk" not in body, "must not carry its own protobuf reader"


def test_the_gtfs_providers_share_machinery_and_the_base():
    import os
    d = _providers_dir()
    assert _imports(os.path.join(d, "_gtfs_rail.py")) >= {"_board"}
    for fn in ("metro_north.py", "lirr.py"):
        assert "_gtfs_rail" in _imports(os.path.join(d, fn)), fn


def test_every_board_writes_a_time_the_same_way():
    """The rule that justifies a shared base: a viewer sees these side by side.

    One board reading "Now" while the next reads "0m" is the sort of thing
    nobody files a bug for and everybody notices.
    """
    d = _providers_dir()
    by = {c.name: c for c, _ in discover([d])}
    made = [by[n]({"line": "x", "station": "y"}) for n in ("metro-north", "lirr", "amtrak")]
    for board in made:
        assert board.countdown(0) == "Now"
        assert board.countdown(7) == "7m"
        assert board.countdown(61) == ""
        assert board.hhmm(1789924620) == made[0].hhmm(1789924620)


def test_lateness_is_only_reported_when_it_matters():
    d = _providers_dir()
    cls = {c.name: c for c, _ in discover([d])}["amtrak"]
    b = cls({})
    assert b.lateness(0) == "" and b.lateness(4) == "", "under 5 min is feed noise"
    assert b.lateness(12) == "+12m"


def test_a_long_route_name_is_shortened_for_the_sign():
    m = _load_amtrak()
    assert m._short_route("Northeast Regional") == "Regional"
    assert m._short_route("Acela") == "Acela"
    # An unlisted route still renders something rather than overflowing.
    assert m._short_route("Pacific Surfliner") == "Pacific"
    assert m._short_route("") == ""


def test_amtrak_direction_comes_from_call_order_not_a_headsign():
    """A train calling here BEFORE Boston is leaving for Boston.

    Built from a stub feed so this runs with no network: the shape is the one
    api-v3.amtraker.com returns, keyed by train number.
    """
    m = _load_amtrak()
    import datetime as dt

    now = dt.datetime.now(dt.timezone.utc).astimezone()

    def stop(code, name, mins):
        t = (now + dt.timedelta(minutes=mins)).isoformat()
        return {"code": code, "name": name, "schArr": t, "schDep": t,
                "arr": t, "dep": t, "status": "Enroute", "platform": ""}

    feed = {
        # Southbound: Boston first, so NYP is an ARRIVAL from Boston.
        "99": [{"routeName": "Northeast Regional", "trainNum": "99",
                "stations": [stop("BOS", "Boston South", 5), stop("NYP", "New York Penn", 60)]}],
        # Northbound: NYP first, so it is a DEPARTURE to Boston.
        "150": [{"routeName": "Northeast Regional", "trainNum": "150",
                 "stations": [stop("NYP", "New York Penn", 10), stop("BOS", "Boston South", 70)]}],
    }

    class Stub(m.AmtrakProvider):
        def _fetch(self):
            return [run for runs in feed.values() for run in runs]

    payload = Stub({})._fetch() and Stub({}).poll()["amtrak.nyp-bos"]
    assert [r["train"] for r in payload["departures"]] == ["150"]
    assert [r["train"] for r in payload["arrivals"]] == ["99"]
    assert payload["departures"][0]["destination"] == "Boston South"
    assert payload["arrivals"][0]["from"] == "Boston South"
    assert payload["departures"][0]["service"] == "Regional"


def test_amtrak_skips_trains_that_do_not_touch_both_ends():
    m = _load_amtrak()
    import datetime as dt
    now = dt.datetime.now(dt.timezone.utc).astimezone()
    t = (now + dt.timedelta(minutes=20)).isoformat()

    def stop(code, name):
        return {"code": code, "name": name, "schArr": t, "schDep": t,
                "arr": t, "dep": t, "status": "Enroute", "platform": ""}

    class Stub(m.AmtrakProvider):
        def _fetch(self):
            return [{"routeName": "Northeast Regional", "trainNum": "500",
                     "stations": [stop("NYP", "New York Penn"), stop("WAS", "Washington")]}]

    p = Stub({}).poll()["amtrak.nyp-bos"]
    assert p["counts"] == {"departures": 0, "arrivals": 0, "trains": 0}


def test_the_shipped_amtrak_board_is_valid_and_matches_the_others():
    import os
    here = os.path.dirname(__file__)
    def find(name):
        for c in (f"/custom/example/shows/{name}", os.path.join(here, "..", "custom", "example", "shows", name)):
            if os.path.exists(c):
                return c
        return None
    paths = {n: find(f"{n}.yaml") for n in ("metro-north", "lirr", "amtrak")}
    assert all(paths.values()), paths
    shows = {n: load_show(open(p).read()) for n, p in paths.items()}
    for n, sh in shows.items():
        assert [s.name for s in sh.scenes] == ["departures", "arrivals", "on-the-line"], n
        assert sum(1 for s in sh.scenes for l in s.layers if l.type == "rows") == 3, n
    # The geometry is deliberately identical across systems that share no
    # upstream -- that is what makes a show portable between them.
    boxes = {n: [tuple((l.box.x, l.box.y, l.box.w, l.box.h))
                 for s in sh.scenes for l in s.layers if l.type == "rows"]
             for n, sh in shows.items()}
    assert boxes["metro-north"] == boxes["lirr"] == boxes["amtrak"], boxes


def test_every_shipped_rows_layer_addresses_a_list():
    """A `rows` layer needs `topic|path`, not a bare topic.

    Caught in the field: the rotation board was generated with
    `data: "mnr.harlem"` instead of `data: "mnr.harlem|departures"`. The layer
    then reads the whole payload -- a dict, not a list -- and draws NOTHING,
    which is the documented behaviour for a path that is not a list and gives a
    board that is silently, entirely blank. Nothing raises and nothing logs.
    """
    import glob, os
    here = os.path.dirname(__file__)
    root = next((c for c in ("/custom/example/shows",
                             os.path.join(here, "..", "custom", "example", "shows"))
                 if os.path.isdir(c)), None)
    assert root, "example shows not found"
    lists = {"departures", "arrivals", "trains"}
    checked = 0
    for path in sorted(glob.glob(os.path.join(root, "*.yaml"))):
        show = load_show(open(path).read())
        for sc in show.scenes:
            for layer in sc.layers:
                if layer.type != "rows":
                    continue
                checked += 1
                addr = layer.address
                assert "|" in addr, f"{os.path.basename(path)}/{sc.name}: {addr!r} has no path"
                assert addr.split("|", 1)[1] in lists, \
                    f"{os.path.basename(path)}/{sc.name}: {addr!r} is not a known list"
    assert checked >= 12, f"expected the shipped boards to be covered, saw {checked}"


def test_the_rotation_board_covers_every_system():
    import os
    here = os.path.dirname(__file__)
    path = next((c for c in ("/custom/example/shows/rail-rotation.yaml",
                             os.path.join(here, "..", "custom", "example",
                                          "shows", "rail-rotation.yaml"))
                 if os.path.exists(c)), None)
    assert path, "rotation show not found"
    show = load_show(open(path).read())
    assert len(show.scenes) == 9
    topics = {l.address.split("|")[0] for sc in show.scenes
              for l in sc.layers if l.type == "rows"}
    assert topics == {"mnr.harlem", "lirr.babylon-branch", "amtrak.nyp-bos"}
    # 90 seconds: half a minute per railroad.
    assert show.total_duration == 90


# ----- values for program-mode cards ---------------------------------------
#
# A card in `mode: program` is not streamed to: it composes its own pixels from
# named values. The on-card table holds 24 keys and `set()` IGNORES the 25th
# rather than evicting, so these assert the budget as much as the behaviour.

from player.valuepush import provider_values

MAX_VALUES = 24          # sw_rust/barsign_disp/src/values.rs
KEY_LEN = 16
VAL_LEN = 64


def _rail_providers():
    d = _providers_dir()
    return [cls({}) for cls, _ in discover([d])]


def _live_with_boards():
    """A live cache holding one plausible payload per rail provider."""
    live = LiveState()
    def rows(n, **extra):
        return [dict({"time": f"1{i}:0{i}", "next_time": f"1{i}:0{i}",
                      "countdown": f"{i}m", "destination": "North White Plains",
                      "from": "Crestwood", "service": "Northeast Regional",
                      "next_stop": "Harlem Valley-Wingdale"}, **extra)
                for i in range(n)]
    for topic, station, line in (("mnr.harlem", "Grand Central", "Harlem"),
                                 ("lirr.babylon-branch", "Penn Station", "Babylon Branch"),
                                 ("amtrak.nyp-bos", "New York Penn", "Northeast Corridor")):
        live.put(topic, {"station": station, "line": line,
                         "departures": rows(9), "arrivals": rows(9), "trains": rows(9)},
                 owner="provider:test")
    return live


def test_every_board_page_fits_the_cards_value_table():
    """The ceiling that made this four rows and not six.

    `set()` ignores a key once the table is full rather than evicting, so going
    over does not raise anywhere -- one column simply stays blank on the glass
    with nothing in any log to say why.
    """
    live = _live_with_boards()
    pages = [pg for p in _rail_providers() for pg in p.board_pages(live)]
    assert pages, "expected the rail providers to offer pages"
    for pg in pages:
        assert len(pg) + 1 <= MAX_VALUES, f"{len(pg)}+hhmm over {MAX_VALUES}: {sorted(pg)}"
        for k, v in pg.items():
            assert len(k) <= KEY_LEN, f"key {k!r} over {KEY_LEN}"
            assert len(v) <= VAL_LEN, f"{k} value {len(v)} over {VAL_LEN}"


def test_a_page_fills_every_row_slot_even_when_the_feed_is_short():
    """A key left OUT keeps its previous value on the card.

    On a short list that leaves the last page's train sitting under this page's
    heading, which is the worst thing a departure board can do.
    """
    live = LiveState()
    live.put("mnr.harlem", {"station": "Grand Central", "line": "Harlem",
                            "departures": [{"time": "14:14", "destination": "Southeast",
                                            "countdown": "5m"}],
                            "arrivals": [], "trains": []}, owner="provider:test")
    mnr = {c.name: c for c, _ in discover([_providers_dir()])}["metro-north"]({})
    page = mnr.board_pages(live)[0]
    for i in range(mnr.BOARD_ROWS):
        for col in "abc":
            assert f"r{i}{col}" in page, f"r{i}{col} missing -- it would keep a stale value"


def test_empty_cells_are_a_space_not_an_empty_string():
    """The card cannot tell "" from a key that never arrived.

    `values.rs::text()` returns `-` for both, deliberately: a program must never
    have to decide what ABSENT looks like. But an empty row is not absent, it is
    "no train" -- an assertion the host is making.
    """
    live = LiveState()
    live.put("mnr.harlem", {"station": "X", "line": "Y", "departures": [],
                            "arrivals": [], "trains": []}, owner="provider:test")
    mnr = {c.name: c for c, _ in discover([_providers_dir()])}["metro-north"]({})
    page = mnr.board_pages(live)[0]
    blanks = [v for k, v in page.items() if k.startswith("r")]
    assert blanks and all(v == " " for v in blanks), blanks


def test_a_long_destination_is_padded_so_the_card_scrolls_it():
    """Padding IS the instruction to scroll: the program measures what it gets.

    And it has to carry its own gap -- `cover_scroll` wraps the string, so an
    unpadded name runs into itself with no space between end and start.
    """
    live = _live_with_boards()
    mnr = {c.name: c for c, _ in discover([_providers_dir()])}["metro-north"]({})
    page = mnr.board_pages(live)[0]
    assert page["r0b"] == "North White Plains   ", repr(page["r0b"])

    short = LiveState()
    short.put("mnr.harlem", {"station": "X", "line": "Y",
                             "departures": [{"time": "1", "destination": "Babylon",
                                             "countdown": "2m"}],
                             "arrivals": [], "trains": []}, owner="provider:test")
    assert mnr.board_pages(short)[0]["r0b"] == "Babylon", "a name that fits must not scroll"


def test_values_are_selected_by_program_never_merged():
    """Merging every source would blow the 24-key table and lose keys silently."""
    live = _live_with_boards()
    provs = _rail_providers()
    v = provider_values(provs, live, "railboard")
    assert v and len(v) <= MAX_VALUES
    assert "hhmm" in v, "the card has no RTC; the clock is pushed like any value"
    # A program nothing serves falls through to the bus values, not to a merge.
    assert provider_values(provs, live, "dashboard-home") is None
    assert provider_values(provs, live, "") is None


def test_the_rotation_is_derived_from_the_clock_not_counted():
    """Stateless, so a pusher restart does not jump the sequence."""
    import datetime as dt
    live = _live_with_boards()
    provs = _rail_providers()
    seen = {}
    for sec in range(0, 120, 10):
        v = provider_values(provs, live, "railboard", now=dt.datetime.fromtimestamp(sec))
        seen[sec] = (v["sys"], v["hdr"])
    assert len(set(seen.values())) == 9, f"expected 9 distinct pages, got {set(seen.values())}"
    # Same clock in, same page out -- no hidden counter.
    again = provider_values(provs, live, "railboard", now=dt.datetime.fromtimestamp(40))
    assert (again["sys"], again["hdr"]) == seen[40]


def test_providers_declare_which_programs_they_feed():
    for p in _rail_providers():
        assert "railboard" in p.PROGRAMS, p.name


def test_the_pushed_clock_matches_the_zone_of_the_times_beside_it():
    """The containers run UTC; the trains do not.

    A card showed 21:07 in its corner beside a train leaving at 17:08, because
    the clock came from the pusher's naive `datetime.now()` while every train
    time went through the board's own `ZoneInfo`. The clock a card displays has
    to come from the same zone as everything else on that card.
    """
    live = _live_with_boards()
    for prov in _rail_providers():
        for pg in prov.board_pages(live):
            assert "hhmm" in pg, f"{prov.name} must supply its own clock"
            assert pg["hhmm"] == prov.hhmm(int(__import__("time").time())), prov.name
    # and the pusher must not overwrite it
    v = provider_values(_rail_providers(), live, "railboard")
    pages = [pg for p in _rail_providers() for pg in p.board_pages(live)]
    assert v["hhmm"] in {pg["hhmm"] for pg in pages}


def test_the_documented_examples_are_real_python():
    """Every ```python block in doc/providers.md must actually import.

    Written after an audit found one that could not: it elided a dict with a
    bare `...`, which reads fine and is a SyntaxError when pasted. A reference
    whose examples do not run teaches the wrong thing twice -- once when they
    fail, and once when the reader assumes the rest is equally approximate.
    """
    import os, re, tempfile
    here = os.path.dirname(__file__)
    path = next((c for c in ("/app/doc/providers.md",
                             os.path.join(here, "..", "doc", "providers.md"))
                 if os.path.exists(c)), None)
    assert path, "doc/providers.md not found"
    blocks = re.findall(r"```python\n(.*?)```", open(path).read(), re.S)
    assert blocks, "expected python examples in the contract doc"
    d = tempfile.mkdtemp()
    for i, body in enumerate(blocks):
        if "class " not in body:
            continue
        # The first line may be the import; later blocks assume it.
        src = body if "import" in body else "from marquee_core.providers import Provider\n" + body
        f = os.path.join(d, f"docblock{i}.py")
        open(f, "w").write(src)
        classes = load_file(f)          # raises on a SyntaxError
        assert classes, f"block {i} defines no Provider subclass"
