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
