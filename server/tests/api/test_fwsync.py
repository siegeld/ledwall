"""The firmware sync's rules (app/fwsync.py, ARCH.md §2c), decided without a
card: `plan()` is pure, so every rule is checked here directly."""
from __future__ import annotations

from app import fwsync
from app.fwsync import Track, View, plan

IMG = "e320-spwm-v5.0.5.bin"
HDR = (594248, 0xBFFD231B)


def st(version="5.0.5", isr=100):
    return {"version": version, "isr_count": isr}


def v(hop=0, status=None, flash=None, want=HDR, assigned=IMG):
    return View(id=1, name="c", hop=hop, assigned=assigned,
                status=status, flash=flash, want_flash=want)


def test_a_head_on_the_wrong_image_is_rebooted_and_netboots_it():
    t = Track()
    assert plan(v(hop=0, status=st("5.0.4")), t, 1000.0) == "reboot"


def test_a_member_is_netbooted_or_on_old_firmware_flashed():
    assert plan(v(hop=1, status=st("5.0.5", 1), assigned="e320-spwm-v5.0.6.bin"),
                Track(), 1000.0) == "netboot"
    assert plan(v(hop=1, status=st("5.0.4")), Track(), 1000.0) == "flash-and-reboot"


def test_retries_are_spaced_and_bounded():
    t = Track()
    assert plan(v(status=st("5.0.4")), t, 1000.0) == "reboot"
    assert plan(v(status=st("5.0.4")), t, 1001.0) == "none", "not again at once"
    now = 1000.0
    for _ in range(fwsync.MAX_ATTEMPTS - 1):
        now += fwsync.RETRY_S + 1
        assert plan(v(status=st("5.0.4")), t, now) == "reboot"
    now += fwsync.RETRY_S + 1
    assert plan(v(status=st("5.0.4")), t, now) == "none"
    assert "gave up" in t.state
    # a new assignment is a new history
    assert plan(v(status=st("5.0.4"), assigned="e320-spwm-v5.0.6.bin"), t, now + 1) == "reboot"


def test_an_operators_push_is_never_raced():
    t = Track(touched=1000.0)
    assert plan(v(status=st("5.0.4")), t, 1010.0) == "none"
    assert plan(v(status=st("5.0.4")), t, 1000.0 + fwsync.QUIET_AFTER_TOUCH_S + 1) == "reboot"


def test_survival_copy_waits_for_the_settle_time_then_is_written_once():
    t = Track()
    now = 1000.0
    assert plan(v(status=st()), t, now) == "none"
    assert plan(v(status=st(isr=200)), t, now + fwsync.SETTLE_S - 1) == "none"
    later = now + fwsync.SETTLE_S + 1
    assert plan(v(status=st(isr=300)), t, later) == "read-flash"
    assert plan(v(status=st(isr=300), flash=(1, 2)), t, later) == "survival"
    # in sync: nothing more, ever, until something changes
    assert plan(v(status=st(isr=400), flash=HDR), t, later + 30) == "none"
    assert t.survival == "in sync"


def test_a_restart_restarts_the_settle_clock():
    t = Track()
    plan(v(status=st(isr=500)), t, 1000.0)
    plan(v(status=st(isr=5)), t, 1000.0 + fwsync.SETTLE_S + 1)   # isr went DOWN: it rebooted
    assert plan(v(status=st(isr=50)), t, 1000.0 + fwsync.SETTLE_S + 2) == "none"


def test_offline_or_unversioned_cards_are_left_alone():
    assert plan(v(status=None), Track(), 1000.0) == "none"
    t = Track()
    assert plan(v(status=st("5.0.4"), assigned="boot.bin"), t, 1000.0) == "none"
    assert "no version" in t.state


def test_flash_header_parsing():
    assert fwsync.flash_header("481109001b23fdbf" + "00" * 8) == (0x00091148, 0xBFFD231B)
    assert fwsync.flash_header("00070e15") is None
    assert fwsync.has_netboot("5.0.5") and not fwsync.has_netboot("5.0.4")
    assert fwsync.image_version("e320-spwm-v5.0.5.bin") == "5.0.5"
