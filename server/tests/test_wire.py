"""The panel wire protocol: chunk arithmetic, packet shapes, quantiser.

These are pure-function tests -- no panel, no network, no DB. They exist because
every bug this protocol has produced was an arithmetic one that looked fine in
review: an off-by-one chunk stride shows on the wall as shearing, and a packet
one byte over the MTU shows as intermittent loss under load. Both are cheap to
assert and expensive to diagnose.
"""
from __future__ import annotations

import pytest
from PIL import Image

from marquee_core import stream as S
from marquee_core.render import frame_to_indexed, frame_to_rgb
from marquee_core.scene import Display

W, H = 256, 192
MTU_BUDGET = 1500 - 20 - 8  # IP + UDP headers


@pytest.fixture
def img() -> Image.Image:
    im = Image.new("RGB", (W, H))
    px = im.load()
    for y in range(H):
        for x in range(W):
            px[x, y] = ((x * 5) % 256, (y * 7) % 256, ((x + y) * 3) % 256)
    return im


class FakeSock:
    """Captures datagrams instead of sending them."""

    def __init__(self) -> None:
        self.sent: list[bytes] = []

    def sendto(self, data: bytes, addr) -> None:
        self.sent.append(data)

    def setsockopt(self, *a) -> None:
        pass

    def close(self) -> None:
        pass


@pytest.fixture
def stream():
    st = S.CardStream("127.0.0.1", packet_delay=0)
    st.sock = FakeSock()
    return st


# --- chunk arithmetic ------------------------------------------------------

def test_indexed_chunk_is_exactly_three_rgb_chunks():
    """1461, not 1462.

    The gateware derives its stride as `3 * pixels_per_chunk`. MAX_PAYLOAD is
    1462, which is NOT divisible by three -- using it would place every chunk
    after the first one pixel left of where the hardware wrote it, which
    presents as shearing rather than as an error.
    """
    assert S.PIXELS_PER_CHUNK_INDEXED == 3 * S.PIXELS_PER_CHUNK
    assert S.PIXELS_PER_CHUNK_INDEXED == 1461


def test_indexed_uses_about_a_third_the_packets():
    """The entire point: the panel is bound by packets, not pixels."""
    rgb = (W * H * 3 + S.BYTES_PER_CHUNK - 1) // S.BYTES_PER_CHUNK
    idx = (W * H + S.PIXELS_PER_CHUNK_INDEXED - 1) // S.PIXELS_PER_CHUNK_INDEXED
    assert rgb == 101 and idx == 34
    assert 2.9 < rgb / idx < 3.1


def test_chunk_index_fits_a_u8():
    """chunk_index is one byte on the wire; 256 chunks would wrap to 0."""
    for fmt_px_per_chunk in (S.PIXELS_PER_CHUNK, S.PIXELS_PER_CHUNK_INDEXED):
        assert (W * H + fmt_px_per_chunk - 1) // fmt_px_per_chunk <= 255


# --- packets on the wire ---------------------------------------------------

def test_indexed_frame_packets(stream, img):
    indices, _ = frame_to_indexed(img)
    stream.send_frame_indexed(indices, W, H)
    sent = stream.sock.sent

    assert len(sent) == 34
    assert all(p[:2] == b"BI" for p in sent)
    assert all(len(p) <= MTU_BUDGET for p in sent)
    # Every pixel sent exactly once: no gap, no overlap.
    assert sum(len(p) - 10 for p in sent) == W * H


def test_rgb_frame_is_unchanged(stream, img):
    """The compatible path must not move -- old firmware still speaks only this."""
    stream.send_frame(frame_to_rgb(img), W, H)
    sent = stream.sock.sent
    assert len(sent) == 101
    assert all(p[:2] == b"BM" for p in sent)
    assert sum(len(p) - 10 for p in sent) == W * H * 3


def test_every_rgb_packet_fits_the_mtu(stream, img):
    """The narrow path fills the MTU to within a byte; it must not cross it."""
    stream.send_frame(frame_to_rgb(img), W, H)
    assert all(len(p) <= MTU_BUDGET for p in stream.sock.sent)


# A frame over 255 chunks: four P1.25 modules on one card, 256x512.
WIDE_W, WIDE_H = 256, 512


def test_wide_frame_packets_fit_the_mtu(stream):
    """The test that would have caught the wide header's 1501-byte datagram.

    487 px behind the 12-byte header is 1473 bytes of UDP payload, one over the
    budget. The kernel fragments it, the card parses only the first fragment,
    and the last pixel of every chunk never lands -- a fixed lattice of stale
    pixels with every card counter clean (E320, 2026-10-26).
    """
    stream.send_frame(bytes(WIDE_W * WIDE_H * 3), WIDE_W, WIDE_H)
    sent = stream.sock.sent
    assert all(p[:2] == b"BW" for p in sent)
    assert max(len(p) for p in sent) <= MTU_BUDGET


def test_wide_frame_places_every_pixel_once(stream):
    """Chunk N starts at pixel N * PIXELS_PER_CHUNK_WIDE, and nothing is lost."""
    body = bytes((i * 7) % 251 for i in range(WIDE_W * WIDE_H * 3))
    stream.send_frame(body, WIDE_W, WIDE_H)
    sent = stream.sock.sent
    hdr = S.HEADER_WIDE.size
    total = -(-WIDE_W * WIDE_H // S.PIXELS_PER_CHUNK_WIDE)
    assert len(sent) == total == 270
    rebuilt = bytearray(len(body))
    for p in sent:
        _, _, idx, tot, w, h = S.HEADER_WIDE.unpack(p[:hdr])
        assert (tot, w, h) == (total, WIDE_W, WIDE_H)
        off = idx * S.PIXELS_PER_CHUNK_WIDE * 3
        rebuilt[off:off + len(p) - hdr] = p[hdr:]
    assert sum(len(p) - hdr for p in sent) == len(body)
    assert bytes(rebuilt) == body


def test_palette_is_one_packet(stream, img):
    _, palette = frame_to_indexed(img)
    stream.send_palette(palette)
    sent = stream.sock.sent
    assert len(sent) == 1
    assert sent[0][:2] == b"BP"
    assert len(sent[0]) == 10 + 768
    assert sent[0][4] == 0  # first entry


def test_palette_does_not_advance_frame_id(stream, img):
    """A palette is not a frame.

    Bumping frame_id would make the panel treat the next real frame as a repeat
    of one it has already seen.
    """
    _, palette = frame_to_indexed(img)
    before = stream._frame_id
    stream.send_palette(palette)
    assert stream._frame_id == before


def test_partial_palette_allowed(stream):
    stream.send_palette(bytes(3 * 16), start=240)
    p = stream.sock.sent[0]
    assert p[4] == 240 and len(p) == 10 + 48


def test_palette_past_the_end_is_rejected(stream):
    """256 entries is the whole palette; the panel clamps, but catch it here."""
    with pytest.raises(ValueError):
        stream.send_palette(bytes(3 * 32), start=240)


def test_palette_must_be_whole_triples(stream):
    with pytest.raises(ValueError):
        stream.send_palette(bytes(770))


def test_frame_size_is_checked(stream):
    with pytest.raises(ValueError):
        stream.send_frame_indexed(bytes(W * H - 1), W, H)
    with pytest.raises(ValueError):
        stream.send_frame(bytes(W * H * 3 - 1), W, H)


# --- quantiser -------------------------------------------------------------

def test_quantiser_shapes(img):
    indices, palette = frame_to_indexed(img)
    assert len(indices) == W * H
    assert len(palette) == 768, "palette must be padded to a full 256 entries"


def test_quantiser_pads_short_palettes():
    """A flat image quantises to very few colours.

    Without padding, the short palette would leave whatever the previous scene
    put in the high indices still lit.
    """
    flat = Image.new("RGB", (16, 16), (10, 20, 30))
    indices, palette = frame_to_indexed(flat)
    assert len(indices) == 256
    assert len(palette) == 768


def test_quantiser_is_lossless_on_few_colours():
    """Flat graphics and text are what this format is for."""
    im = Image.new("RGB", (32, 32))
    px = im.load()
    for y in range(32):
        for x in range(32):
            px[x, y] = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 255)][(x // 8) % 4]
    indices, palette = frame_to_indexed(im)
    rebuilt = [
        tuple(palette[indices[i] * 3 : indices[i] * 3 + 3]) for i in range(0, 1024, 37)
    ]
    original = list(im.getdata())
    assert all(rebuilt[n] == original[i] for n, i in enumerate(range(0, 1024, 37)))


# --- scene field -----------------------------------------------------------

def test_format_defaults_to_rgb():
    """Existing shows must keep working against firmware that has never heard
    of the indexed format."""
    assert Display().format == "rgb"


def test_format_accepts_indexed():
    assert Display(format="indexed").format == "indexed"


def test_format_rejects_anything_else():
    with pytest.raises(Exception):
        Display(format="palette")


# ---------------------------------------------------------------------------
# The two halves must agree, and only a test can make that true.
# ---------------------------------------------------------------------------
#
# Everything above checks the SERVER's arithmetic against literals written in
# this file. That cannot catch the failure that actually matters: the panel
# firmware defines the same constants independently, in Rust, and if the two
# drift the symptom is a SHEARED IMAGE rather than an error. Nothing raises,
# nothing logs, and the packets all look valid.
#
# This was impossible to test while the firmware and the server lived in
# separate repositories -- you cannot assert against a file you have not
# cloned. They now ship as `firmware/` and `server/` in one tree specifically
# so this test can exist.

import re
from pathlib import Path


def _firmware_wire_source() -> str | None:
    """`bitmap_udp.rs`, wherever this tree happens to be laid out.

    Skips rather than fails when the firmware is absent: the server repo is
    still developed on its own, and a test that cannot run is not a test that
    failed. It is CI on the combined tree that has to be able to run it.
    """
    import os
    here = Path(__file__).resolve()
    rel = "sw_rust/barsign_disp/src/bitmap_udp.rs"
    bases = []
    if os.environ.get("LEDWALL_FIRMWARE"):
        bases.append(Path(os.environ["LEDWALL_FIRMWARE"]))
    bases += [
        here.parents[1] / ".." / "firmware",   # combined repo: server/ + firmware/
        here.parents[2] / "firmware",
    ]
    for base in bases:
        f = (base / rel).resolve()
        if f.is_file():
            return f.read_text()
    return None


def _rust_const(src: str, name: str) -> int:
    """Read `const NAME: usize = <expr>; // value` from the Rust source.

    Takes the trailing comment's value where there is one, because the
    firmware writes these as expressions (`MAX_PAYLOAD / 3`) and evaluating
    Rust here would be worse than reading the number it documents -- the
    comment and the expression are checked against each other below.
    """
    m = re.search(rf"const\s+{name}\s*:\s*usize\s*=\s*([^;]+);(?:\s*//\s*(\d+))?", src)
    assert m, f"{name} not found in bitmap_udp.rs -- did it get renamed?"
    expr, commented = m.group(1).strip(), m.group(2)
    if commented:
        return int(commented)
    if expr.isdigit():
        return int(expr)
    pytest.skip(f"{name} is an expression with no documented value: {expr}")


@pytest.fixture
def fw() -> str:
    src = _firmware_wire_source()
    if src is None:
        pytest.skip("firmware tree not present -- run this on the combined repo")
    return src


def _gateware_dma_source() -> str | None:
    """`dma_writer.py`, found the same way as the firmware's wire source.

    The gateware holds the THIRD copy of the chunk stride. The two tests below
    it have named it as such since they were written without ever asserting it,
    so a change there failed silently -- and the symptom is a sheared picture,
    not an error.
    """
    import os
    here = Path(__file__).resolve()
    rel = "gateware/dma_writer.py"
    bases = []
    if os.environ.get("LEDWALL_FIRMWARE"):
        bases.append(Path(os.environ["LEDWALL_FIRMWARE"]))
    bases += [here.parents[1] / ".." / "firmware", here.parents[2] / "firmware"]
    for base in bases:
        f = (base / rel).resolve()
        if f.is_file():
            return f.read_text()
    return None


@pytest.fixture
def gw() -> str:
    src = _gateware_dma_source()
    if src is None:
        pytest.skip("gateware tree not present -- run this on the combined repo")
    return src


def test_gateware_chunk_stride_matches(gw):
    """The THIRD copy of 487, in the gateware that actually places the pixels.

    `stream.py` and `bitmap_udp.rs` were already checked against each other.
    The gateware was not, even though the test above calls it out by name --
    and it is the copy that decides where a chunk physically lands in SDRAM.
    Drift here shears the image; nothing raises.
    """
    m = re.search(r"pixels_per_chunk\s*=\s*(\d+)", gw)
    assert m, "pixels_per_chunk not found in dma_writer.py -- renamed?"
    assert int(m.group(1)) == S.PIXELS_PER_CHUNK

    # And the indexed stride is derived as 3x in the gateware too, not written
    # out as 1461 -- assert the derivation rather than the literal.
    assert re.search(r"chunk_index\s*\*\s*\(\s*3\s*\*\s*pixels_per_chunk\s*\)", gw), \
        "the gateware's indexed stride is no longer 3 * pixels_per_chunk"


def test_gateware_wide_stride_matches(gw):
    """The wide chunk is one pixel shorter in the gateware too, or it shears."""
    assert S.PIXELS_PER_CHUNK_WIDE == S.PIXELS_PER_CHUNK - 1
    assert re.search(r"chunk_index\s*\*\s*\(\s*pixels_per_chunk\s*-\s*1\s*\)", gw), \
        "the gateware's wide stride is no longer pixels_per_chunk - 1"


def test_header_size_matches_firmware(fw):
    """The server packs the header with a struct; the panel indexes it by size."""
    assert S.HEADER.size == _rust_const(fw, "HEADER_SIZE")
    assert S.HEADER_WIDE.size == _rust_const(fw, "HEADER_SIZE_WIDE")


def test_wide_pixels_per_chunk_matches_firmware(fw):
    """486 on both sides; the firmware's repair copies chunks at this stride."""
    assert S.PIXELS_PER_CHUNK_WIDE == _rust_const(fw, "PIXELS_PER_CHUNK_WIDE")


def test_pixels_per_chunk_matches_firmware(fw):
    """487 on both sides, or every chunk after the first lands in the wrong place."""
    assert S.PIXELS_PER_CHUNK == _rust_const(fw, "PIXELS_PER_CHUNK")


def test_indexed_chunk_matches_firmware(fw):
    """1461 = 3 * 487. The gateware's `chunk_pixels` is the third copy of this."""
    assert S.PIXELS_PER_CHUNK_INDEXED == _rust_const(fw, "PIXELS_PER_CHUNK_INDEXED")


def test_firmware_chunk_arithmetic_is_self_consistent(fw):
    """The firmware's own comment must match its own expression.

    `PIXELS_PER_CHUNK = MAX_PAYLOAD / 3` with `// 487` beside it: this asserts
    the comment has not gone stale against the expression it documents, which
    is what the tests above read.
    """
    max_payload = _rust_const(fw, "MAX_PAYLOAD")
    assert max_payload // 3 == _rust_const(fw, "PIXELS_PER_CHUNK")
    assert 3 * _rust_const(fw, "PIXELS_PER_CHUNK") == _rust_const(fw, "PIXELS_PER_CHUNK_INDEXED")
    # 1462 is not divisible by 3, which is the whole reason the indexed size is
    # 3*487 and not MAX_PAYLOAD.
    assert max_payload % 3 != 0
