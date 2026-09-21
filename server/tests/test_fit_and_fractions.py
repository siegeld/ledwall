"""Fractional boxes and the `smart` fit — what makes one show fit many walls."""

import math

import pytest
from PIL import Image

from marquee_core.render import _fit, _smart_scale
from marquee_core.scene import Box, load_show

WALLS = {
    "bench 128x128 (square)": (128, 128),
    "one panel 128x64":       (128, 64),
    "3x3 768x384":            (768, 384),
    "side by side 256x64":    (256, 64),
}


# ---- fractional boxes ----------------------------------------------------

def test_ints_are_pixels_and_floats_are_fractions():
    b = Box.coerce([0, 0.5, 128, 0.25]).resolve(256, 128)
    assert (b.x, b.y, b.w, b.h) == (0, 64, 128, 32)


def test_percent_strings_work_too():
    b = Box.coerce(["25%", "0%", "50%", "100%"]).resolve(800, 400)
    assert (b.x, b.y, b.w, b.h) == (200, 0, 400, 400)


def test_one_pixel_and_one_whole_wall_are_different():
    """`1` and `1.0` must not mean the same thing."""
    assert Box.coerce([0, 0, 1, 1]).resolve(500, 500).w == 1
    assert Box.coerce([0, 0, 1.0, 1.0]).resolve(500, 500).w == 500


def test_a_boolean_is_not_a_size():
    with pytest.raises(ValueError):
        Box.coerce([0, 0, True, 10]).resolve(100, 100)


def test_fractions_track_the_wall():
    half = Box.coerce([0, 0, 1.0, 0.5])
    for (w, h) in WALLS.values():
        r = half.resolve(w, h)
        assert (r.w, r.h) == (w, h // 2)


def test_absolute_boxes_are_unchanged_by_resolve():
    """Every existing show must render exactly as before."""
    b = Box.coerce([4, 8, 120, 47])
    r = b.resolve(128, 128)
    assert (r.x, r.y, r.w, r.h) == (4, 8, 120, 47)


def test_a_show_may_omit_its_display_size():
    sh = load_show("display: {fps: 20}\n"
                   "scenes:\n  - name: s\n    duration: 10s\n    layers:\n"
                   '      - {type: solid, color: "#fff", box: [0, 0, 1.0, 1.0]}\n')
    assert sh.display.width is None and sh.display.height is None


# ---- smart fit -----------------------------------------------------------

def _crop_and_black(sw, sh, tw, th):
    s = _smart_scale(sw, sh, tw, th)
    w, h = sw * s, sh * s
    crop = max(1 - tw / w if w > tw else 0.0, 1 - th / h if h > th else 0.0)
    black = max(1 - w / tw if w < tw else 0.0, 1 - h / th if h < th else 0.0)
    return crop, black


def test_a_close_aspect_fills_the_box():
    """16:9 into 2:1 is nearly right -- fill it, do not leave a black edge."""
    crop, black = _crop_and_black(1920, 1080, 128, 64)
    assert black == 0
    assert crop < 0.15


def test_a_bad_mismatch_splits_crop_and_letterbox_equally():
    """16:9 on a square wall: 25% each, rather than 44% of either."""
    crop, black = _crop_and_black(1920, 1080, 128, 128)
    assert crop == pytest.approx(black, abs=0.001)
    assert 0.2 < crop < 0.3


def test_smart_never_does_worse_than_cover_or_contain():
    for sw, sh in ((1920, 1080), (1920, 1440), (640, 480)):
        for tw, th in WALLS.values():
            crop, black = _crop_and_black(sw, sh, tw, th)
            cover_crop = 1 - min(tw / (sw * max(tw / sw, th / sh)),
                                 th / (sh * max(tw / sw, th / sh)))
            contain_black = 1 - min(sw * min(tw / sw, th / sh) / tw,
                                    sh * min(tw / sw, th / sh) / th)
            assert crop <= cover_crop + 1e-9
            assert black <= contain_black + 1e-9


def test_smart_output_is_exactly_the_box():
    """Whatever it decides, the tile must still be the size asked for."""
    src = Image.new("RGB", (1920, 1080), (200, 30, 30))
    for tw, th in WALLS.values():
        out = _fit(src, Box(x=0, y=0, w=tw, h=th), "smart")
        assert out.size == (tw, th)


def test_smart_on_a_square_source_and_square_box_is_lossless():
    src = Image.new("RGB", (512, 512), (10, 200, 10))
    out = _fit(src, Box(x=0, y=0, w=128, h=128), "smart")
    assert out.size == (128, 128)
    assert out.convert("L").getextrema()[0] > 0     # no black bars at all


# ---- fit must actually reach the decoder ---------------------------------
#
# It did not, for a long time: VideoDecoder's filter chain was hard-coded to
# scale-up-and-crop, so every video was cover-cropped whatever the show said.
# `contain` and `smart` were accepted and silently ignored. These pin it.

from marquee_core.render import VideoDecoder


def _chain(fit, w=128, h=128, src=(1920, 1080)):
    d = VideoDecoder.__new__(VideoDecoder)
    d.path, d.w, d.h, d.fit = "x.mp4", w, h, fit
    d._src = src                      # skip ffprobe
    return d._filter()


def test_cover_crops():
    f = _chain("cover")
    assert "increase" in f and "crop=128:128" in f


def test_contain_pads_and_never_crops():
    f = _chain("contain")
    assert "decrease" in f and "pad=128:128" in f and "crop" not in f


def test_stretch_just_scales():
    assert _chain("stretch") == "scale=128:128"


def test_smart_differs_from_cover_on_a_mismatched_wall():
    assert _chain("smart", 128, 128) != _chain("cover", 128, 128)


def test_smart_matches_cover_when_the_aspect_is_close():
    """16:9 into 2:1 -- smart should fill, i.e. not letterbox."""
    f = _chain("smart", 128, 64)
    assert "pad=128:64" in f          # pad is a no-op here, crop does the work
    assert "crop=128:64" in f


def test_smart_falls_back_to_cover_when_the_source_cannot_be_probed():
    d = VideoDecoder.__new__(VideoDecoder)
    d.path, d.w, d.h, d.fit = "x.mp4", 128, 128, "smart"
    d._src = None
    f = d._filter()
    assert "increase" in f and "crop=128:128" in f
