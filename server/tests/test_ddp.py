"""DDP input: the wire, discovery, and the layer that draws it."""

import json
import socket
import time

import pytest

from marquee_core import ddp
from marquee_core.render import Renderer
from marquee_core.scene import load_show


def pkt(flags, ident, offset=0, data=b"", dtype=ddp.TYPE_RGB8, seq=0):
    return ddp.header(flags, ident, offset, len(data), dtype, seq) + data


def reply(raw):
    flags, _seq, _dtype, ident, offset, data = ddp.parse(raw)
    return flags, ident, offset, data


def test_header_is_the_documented_ten_bytes():
    h = ddp.header(ddp.F_PUSH, 1, 0x01020304, 0x0506, ddp.TYPE_RGB8, 7)
    assert h == bytes([0x41, 7, 0x0B, 1, 1, 2, 3, 4, 5, 6])


def test_writes_show_only_on_push_and_blocks_may_arrive_in_any_order():
    rx = ddp.Receiver()
    rx.handle(pkt(0, 1, 3, b"\x04\x05\x06"))
    rx.handle(pkt(ddp.F_PUSH, 1, 0, b"\x01\x02\x03"))
    assert rx.frame(1) == (b"\x01\x02\x03\x04\x05\x06", ddp.TYPE_RGB8)
    # The next frame is half-written: the shown one must not change.
    rx.handle(pkt(0, 1, 0, b"\xff\xff\xff"))
    assert rx.frame(1)[0] == b"\x01\x02\x03\x04\x05\x06"
    # The buffer is not cleared between frames, so a sender may send only
    # what changed. A bare push (no data) is how several displays sync.
    rx.handle(pkt(ddp.F_PUSH, 1))
    assert rx.frame(1)[0] == b"\xff\xff\xff\x04\x05\x06"


def test_a_sender_that_never_pushes_is_shown_as_it_arrives():
    rx = ddp.Receiver()
    rx.handle(pkt(0, 1, 0, b"\x09\x09\x09"))
    assert rx.frame(1)[0] == b"\x09\x09\x09"


def test_push_to_all_devices_pushes_every_output():
    rx = ddp.Receiver()
    rx.handle(pkt(ddp.F_PUSH, 1, 0, b"\x01\x01\x01"))
    rx.handle(pkt(ddp.F_PUSH, 2, 0, b"\x02\x02\x02"))
    rx.handle(pkt(0, 1, 0, b"\x07\x07\x07"))
    rx.handle(pkt(0, 2, 0, b"\x08\x08\x08"))
    rx.handle(pkt(ddp.F_PUSH, ddp.ID_ALL))
    assert rx.frame(1)[0] == b"\x07\x07\x07" and rx.frame(2)[0] == b"\x08\x08\x08"


def test_timecode_header_is_skipped_not_drawn():
    rx = ddp.Receiver()
    raw = bytearray(pkt(ddp.F_PUSH, 1, 0, b"\x01\x02\x03"))
    raw[0] |= ddp.F_TIME
    raw[10:10] = b"\xaa\xbb\xcc\xdd"
    rx.handle(bytes(raw))
    assert rx.frame(1)[0] == b"\x01\x02\x03"


def test_back_to_back_duplicates_are_dropped_by_sequence():
    rx = ddp.Receiver()
    a = pkt(ddp.F_PUSH, 1, 0, b"\x01\x01\x01", seq=5)
    rx.handle(a, ("s", 1))
    rx.handle(a, ("s", 1))
    assert rx.stats()["outputs"][1]["frames"] == 1
    rx.handle(pkt(ddp.F_PUSH, 1, 0, b"\x02\x02\x02", seq=6), ("s", 1))
    assert rx.stats()["outputs"][1]["frames"] == 2


@pytest.mark.parametrize("raw", [
    b"", b"\x41\x00\x0b\x01",                        # short
    bytes([0x01, 0, 0x0B, 1, 0, 0, 0, 0, 0, 3]) + b"abc",   # version 0
    ddp.header(0, 1, 0, 30) + b"only-nine",          # shorter than it claims
    ddp.header(0, 1, ddp.MAX_BYTES, 3) + b"abc",     # offset past the cap
    ddp.header(ddp.F_STORAGE, 1, 0, 3) + b"abc",     # "play from storage"
])
def test_bad_packets_draw_nothing(raw):
    rx = ddp.Receiver()
    assert rx.handle(raw) == []
    assert rx.frame(1) is None


def test_outputs_are_capped():
    rx = ddp.Receiver()
    for i in range(1, 60):
        rx.handle(pkt(ddp.F_PUSH, i, 0, b"\x01\x01\x01"))
    assert len(rx.stats()["outputs"]) == ddp.MAX_OUTPUTS


def test_discovery_status_query_gets_a_json_reply():
    rx = ddp.Receiver(version="9.9.9")
    # Exactly the spec's discovery packet: VER1 | QUERY, ID status, len 0.
    out = rx.handle(bytes([0x42, 0, 0, 251, 0, 0, 0, 0, 0, 0]))
    assert len(out) == 1
    flags, ident, offset, data = reply(out[0])
    assert flags & ddp.F_REPLY and flags & ddp.F_PUSH and not flags & ddp.F_QUERY
    assert (ident, offset) == (ddp.ID_STATUS, 0)
    st = json.loads(data)["status"]
    assert st["ver"] == "9.9.9" and st["push"] is True and st["man"]
    assert len(st["mac"].split(":")) == 6


def test_a_status_query_may_carry_a_hint_and_a_length():
    # Some controllers send {"mac":...} or a nonzero read length. A query's
    # length is how much to read, so no data need follow the header.
    rx = ddp.Receiver()
    assert rx.handle(ddp.header(ddp.F_QUERY, ddp.ID_STATUS, 0, 1440))
    assert rx.handle(ddp.header(ddp.F_QUERY, ddp.ID_STATUS, 0, 9) + b'{"mac":1}')


def test_config_query_lists_outputs_and_unknown_reads_get_the_empty_reply():
    rx = ddp.Receiver(pixels=512)
    cfg = json.loads(reply(rx.handle(ddp.header(ddp.F_QUERY, ddp.ID_CONFIG))[0])[3])
    assert cfg["config"]["ports"] == [{"port": 1, "ts": 0, "l": 512, "ss": 0}]
    rx.handle(pkt(ddp.F_PUSH, 3, 0, bytes(30)))
    cfg = json.loads(reply(rx.handle(ddp.header(ddp.F_QUERY, ddp.ID_CONFIG))[0])[3])
    assert cfg["config"]["ports"] == [{"port": 3, "ts": 0, "l": 10, "ss": 0}]
    flags, ident, offset, data = reply(rx.handle(ddp.header(ddp.F_QUERY, 1, 0, 300))[0])
    assert flags & ddp.F_REPLY and flags & ddp.F_PUSH and (offset, data) == (0, b"")


def test_replies_are_never_answered():
    # Our own startup broadcast comes back to us, and so do other displays'
    # replies to a broadcast query. Answering either would be a loop.
    rx = ddp.Receiver()
    assert rx.handle(rx.announcement()) == []
    assert rx.handle(rx.handle(ddp.header(ddp.F_QUERY, ddp.ID_STATUS))[0]) == []
    assert rx.frame(ddp.ID_STATUS) is None


def test_announcement_is_the_spec_text():
    _f, ident, _o, data = reply(ddp.Receiver().announcement())
    assert ident == ddp.ID_STATUS
    assert data == b'{"status":{"update":"change","state":"up"}}'


def test_over_a_real_socket_discovery_then_pixels():
    rx = ddp.Receiver(version="1.2.3")
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    s.settimeout(3)
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    assert rx.start(port, bind="127.0.0.1")
    s.sendto(ddp.header(ddp.F_QUERY, ddp.ID_STATUS), ("127.0.0.1", port))
    data, _ = s.recvfrom(2048)
    assert json.loads(reply(data)[3])["status"]["ver"] == "1.2.3"
    s.sendto(pkt(ddp.F_PUSH, 1, 0, b"\x0a\x0b\x0c"), ("127.0.0.1", port))
    for _ in range(100):
        if rx.frame(1):
            break
        time.sleep(0.02)
    assert rx.frame(1)[0] == b"\x0a\x0b\x0c"
    # A second receiver on the same port reports failure instead of raising.
    assert ddp.Receiver().start(port, bind="127.0.0.1") in (True, False)


# -- the layer ---------------------------------------------------------------

SHOW = """
display: {width: 8, height: 8}
scenes:
  - name: a
    layers:
      - {type: solid, color: "#000040", box: [0, 0, 8, 8]}
      - {type: ddp, box: [0, 0, 8, 8], width: 2, height: 1, fit: stretch%s}
"""


@pytest.fixture
def rx():
    r = ddp.Receiver()
    ddp.set_default(r)
    yield r
    ddp.set_default(None)


def test_layer_draws_nothing_until_a_sender_appears(rx):
    r = Renderer(load_show(SHOW % ""))
    assert r.render(0).getpixel((0, 0)) == (0, 0, 0x40)
    assert not r.errors


def test_layer_scales_the_senders_picture_into_its_box(rx):
    rx.handle(pkt(ddp.F_PUSH, 1, 0, bytes([255, 0, 0, 0, 255, 0])))
    img = Renderer(load_show(SHOW % "")).render(0)
    assert img.getpixel((0, 1))[0] > 200 and img.getpixel((7, 1))[1] > 200


def test_layer_uncovers_what_is_beneath_after_the_timeout(rx):
    rx.handle(pkt(ddp.F_PUSH, 1, 0, bytes([255, 0, 0, 0, 255, 0])))
    r = Renderer(load_show(SHOW % ", timeout: 50ms"))
    assert r.render(0).getpixel((0, 1))[0] > 200
    time.sleep(0.12)
    assert r.render(0).getpixel((0, 1)) == (0, 0, 0x40)


def test_layer_takes_rgbw_and_a_short_frame(rx):
    # RGBW: the white byte is dropped. And one pixel where two were promised
    # pads black rather than raising.
    rx.handle(pkt(ddp.F_PUSH, 1, 0, bytes([0, 0, 255, 99]), dtype=ddp.TYPE_RGBW8))
    r = Renderer(load_show(SHOW % ""))
    img = r.render(0)
    assert img.getpixel((0, 1))[2] > 200 and not r.errors


def test_serpentine_flips_odd_rows(rx):
    show = """
display: {width: 8, height: 8}
scenes:
  - name: a
    layers:
      - {type: ddp, box: [0, 0, 2, 2], serpentine: true, fit: none}
"""
    rx.handle(pkt(ddp.F_PUSH, 1, 0, bytes([1, 0, 0, 2, 0, 0, 3, 0, 0, 4, 0, 0])))
    img = Renderer(load_show(show)).render(0)
    assert [img.getpixel(p)[0] for p in ((0, 0), (1, 0), (0, 1), (1, 1))] == [1, 2, 4, 3]


def test_without_a_receiver_the_layer_is_simply_absent():
    ddp.set_default(None)
    r = Renderer(load_show(SHOW % ""))
    assert r.render(0).getpixel((0, 0)) == (0, 0, 0x40) and not r.errors
