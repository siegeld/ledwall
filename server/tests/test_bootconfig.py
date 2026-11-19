"""The card's boot config: what a module type adds, and what it must not change.

Pure-function tests on `boot_config()`. The file this renders is the only thing
a cold card knows about itself, so a wrong line here is a card that boots into
the wrong geometry with every counter clean (colorlight E320-BRINGUP.md 4.2).
"""
from __future__ import annotations

from marquee_core.models import PANEL_TYPES, Card, boot_config


def card(**kw):
    base = dict(id=1, name="c", wall_id=1, host="10.0.0.1", mode="stream",
                layout_yaml="grid: 1x1\nJ1: 0,0\n")
    base.update(kw)
    return Card(**base)


def test_no_type_is_byte_identical_to_before():
    c = card(drive="dp3264s")
    assert boot_config(c) == "grid: 1x1\nJ1: 0,0\nmode: stream\ndrive: dp3264s\n"


def test_linsn_type_fills_geometry_arrangement_and_chip():
    c = card(panel_type="linsn-p1.25-256x128-dp3264s")
    cfg = boot_config(c)
    assert "panel_width: 256\n" in cfg
    assert "panel_height: 128\n" in cfg
    assert "group_origins: 128,0 128,64 0,0 0,64\n" in cfg
    assert cfg.endswith("drive: dp3264s\n")
    # The type's lines come AFTER the layout text so they win in the firmware,
    # which keeps the last value of a repeated key.
    assert cfg.index("J1: 0,0") < cfg.index("panel_width: 256")


def test_myled_type_is_stacked_and_names_its_own_bitstream():
    c = card(panel_type="myled-p1.25-128x256-icnd1065")
    cfg = boot_config(c)
    assert "panel_width: 128\n" in cfg and "panel_height: 256\n" in cfg
    # Explicit, so a card that last ran ARGO's 2x2 does not keep it.
    assert "group_origins: 0,0 0,64 0,128 0,192\n" in cfg
    assert cfg.endswith("drive: icnd1065\n")
    # Not ARGO's preset: the row fetch order is baked from the origins.
    assert (PANEL_TYPES["myled-p1.25-128x256-icnd1065"]["bitstream_preset"]
            != PANEL_TYPES["linsn-p1.25-256x128-dp3264s"]["bitstream_preset"])


def test_card_drive_overrides_the_type_drive():
    c = card(panel_type="linsn-p1.25-256x128-dp3264s", drive="dp3364s")
    assert boot_config(c).endswith("drive: dp3364s\n")


def test_hub75_type_adds_no_group_origins_and_no_drive():
    c = card(panel_type="hub75-128x64")
    cfg = boot_config(c)
    assert "panel_width: 128\n" in cfg and "panel_height: 64\n" in cfg
    assert "group_origins" not in cfg
    assert "drive:" not in cfg


def test_every_type_is_complete():
    for key, pt in PANEL_TYPES.items():
        for k in ("label", "board", "connector", "module_w", "module_h",
                  "drive", "group_origins", "bitstream_preset", "notes"):
            assert k in pt, f"{key} lacks {k}"
        assert pt["module_w"] > 0 and pt["module_h"] > 0
        assert pt["bitstream_preset"].startswith(f"{pt['module_w']}x{pt['module_h']}"), key


# --- module types from files ------------------------------------------------

_FILE_TYPE = {
    "label": "Imported P1.25", "board": "e320", "connector": "hub320",
    "module_w": 128, "module_h": 256, "drive": "icnd1065",
    "group_origins": "0,0 0,64 0,128 0,192",
    "bitstream_preset": "128x256-icnd1065l-hub320", "notes": "from a profile",
    "clk_div": 4, "chip_regs": [[0x0c, 0x01, 0x0e, 0x0e], [0x03, 0x3f, 0x3f, 0x3f]],
}


def _write_type(tmp_path, name, pt):
    import json
    (tmp_path / f"{name}.json").write_text(json.dumps(pt))


def test_a_module_type_file_is_loaded_and_served(tmp_path):
    from marquee_core import models
    _write_type(tmp_path, "imported-p125", _FILE_TYPE)
    try:
        assert models.load_module_types([str(tmp_path)]) == {}
        card = Card(name="c", host="h", width=128, height=256, layout_yaml="grid: 1x1\n",
                    panel_type="imported-p125")
        cfg = boot_config(card)
        assert "chip_reg: 0x0c 0x01 0x0e 0x0e\n" in cfg
        assert "chip_reg: 0x03 0x3f 0x3f 0x3f\n" in cfg
        assert "drive: icnd1065\n" in cfg
    finally:
        PANEL_TYPES.pop("imported-p125", None)


def test_a_module_types_group_size_is_served(tmp_path):
    """Runtime geometry: one RGB group's size reaches the card as config."""
    from marquee_core import models
    small = {**_FILE_TYPE, "module_w": 96, "module_h": 192,
             "group_origins": "0,0 0,48 0,96 0,144", "group_columns": 96, "group_scan": 48}
    _write_type(tmp_path, "small-p2", small)
    try:
        assert models.load_module_types([str(tmp_path)]) == {}
        card = Card(name="c", host="h", width=96, height=192, layout_yaml="grid: 1x1\n",
                    panel_type="small-p2")
        cfg = boot_config(card)
        assert "group_columns: 96\n" in cfg and "group_scan: 48\n" in cfg
    finally:
        PANEL_TYPES.pop("small-p2", None)
    # A type without them serves neither line.
    card = Card(name="c", host="h", width=128, height=256, layout_yaml="grid: 1x1\n",
                panel_type="myled-p1.25-128x256-icnd1065")
    assert "group_columns" not in boot_config(card)
    for bad in ({"group_columns": 72}, {"group_scan": 33}, {"group_columns": 0},
                {"group_scan": True}, {"group_columns": "128"}, {"group_columns": 272},
                {"group_scan": None},                      # one without the other
                {"group_origins": None},                   # a size, no arrangement
                {"drive": "", "chip_regs": []},            # nothing to tell the scan to
                {"module_h": 384},                         # not the groups' pixels
                {"group_columns": 64, "group_scan": 72},   # same pixels, strips overlap
                {"group_origins": "0,0 0,48 0,96 0,150"}): # hangs off the module
        _write_type(tmp_path, "bad-size", {**small, **bad})
        assert "bad-size" in models.load_module_types([str(tmp_path)]), bad
        assert "bad-size" not in PANEL_TYPES


def test_a_file_never_replaces_a_built_in_type(tmp_path):
    from marquee_core import models
    before = dict(PANEL_TYPES["hub75-128x64"])
    _write_type(tmp_path, "hub75-128x64", _FILE_TYPE)
    problems = models.load_module_types([str(tmp_path)])
    assert "hub75-128x64" in problems
    assert PANEL_TYPES["hub75-128x64"] == before


def test_a_bad_module_type_file_is_refused_whole(tmp_path):
    from marquee_core import models
    bad = [
        {**_FILE_TYPE, "chip_regs": [[0x0c, 0x01, 0x0e]]},             # short row
        {**_FILE_TYPE, "chip_regs": [[0x0c, 1, 2, 300]]},              # not a byte
        {**_FILE_TYPE, "chip_regs": [[0x0c, 1, 2, 3], [0x0c, 4, 5, 6]]},  # twice
        {**_FILE_TYPE, "drive": "", },                                 # regs, no chip
        {**_FILE_TYPE, "label": "x\nmode: program"},                   # config injection
        {**_FILE_TYPE, "group_origins": "0,0\nmode: program"},
        {k: v for k, v in _FILE_TYPE.items() if k != "notes"},         # missing key
        {**_FILE_TYPE, "clk_div": 1},
    ]
    for i, pt in enumerate(bad):
        _write_type(tmp_path, f"bad{i}", pt)
    (tmp_path / "notjson.json").write_text("{")
    problems = models.load_module_types([str(tmp_path)])
    assert len(problems) == len(bad) + 1, problems
    assert not any(k.startswith("bad") or k == "notjson" for k in PANEL_TYPES)


# --- modules mounted turned ---------------------------------------------------

def test_a_quarter_turn_swaps_the_cards_place_on_the_wall():
    c = Card(name="c", host="h", width=128, height=256, x=256, y=0, rotate=270)
    assert c.footprint == (256, 128)
    assert c.box == (256, 0, 512, 128)
    c.rotate = 180
    assert c.box == (256, 0, 384, 256)


def test_to_framebuffer_matches_the_vendor_pixel_map():
    """Myled's profile: logical (row r, column c) of a 64x128 group is
    physical (scan line c, chain column 127 - r). In framebuffer terms x is
    the chain column and y the scan line, so fb(x, y) = wall(row 127 - x,
    column y) -- a module mounted 270 clockwise."""
    from PIL import Image
    from marquee_core.models import to_framebuffer
    wall = Image.new("RGB", (64, 128))
    px = wall.load()
    for r in range(128):
        for c in range(64):
            px[c, r] = (r, c, 7)
    fb = to_framebuffer(wall, 270)
    assert fb.size == (128, 64)
    f = fb.load()
    for x in (0, 1, 63, 127):
        for y in (0, 5, 63):
            assert f[x, y] == (127 - x, y, 7)


def test_every_rotation_is_a_bijection_back_to_upright():
    from PIL import Image
    from marquee_core.models import to_framebuffer
    img = Image.new("RGB", (6, 4))
    img.putdata([(i, 0, 0) for i in range(24)])
    assert to_framebuffer(img, 0) is img
    assert to_framebuffer(to_framebuffer(img, 90), 270).tobytes() == img.tobytes()
    assert to_framebuffer(to_framebuffer(img, 180), 180).tobytes() == img.tobytes()
    assert to_framebuffer(img, 90).size == (4, 6)
    # Mounted 90 clockwise: the wall's top-left pixel is the framebuffer's
    # bottom-left (content turned 90 counter-clockwise).
    assert to_framebuffer(img, 90).getpixel((0, 5)) == img.getpixel((0, 0))


def test_rotate_reaches_the_card_only_when_turned():
    c = Card(name="c", host="h", width=128, height=256, layout_yaml="grid: 1x1\n")
    assert "rotate" not in boot_config(c)
    c.rotate = 270
    assert "rotate: 270\n" in boot_config(c)
