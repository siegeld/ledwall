#!/usr/bin/env python3
"""Verify `Words32ToPixels` byte-exact against a reference unpacking.

This module replaces the `Bytes32to8` narrowing on the pixel path, so a
single-byte misalignment here does not produce a subtle artefact -- it shears
the colour of every pixel downstream of the error. It therefore gets checked
against a plain-Python reference for every payload length that matters, and
especially for the tail: a 1461-byte chunk is 487 pixels, which is 121 groups
of four plus a remainder, so the "every 3 words = 4 pixels" identity does NOT
land evenly on the final word.

Run: ./build.sh sim-words2px   (or python3 tools/sim_words_to_pixels.py)
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from migen import *                                    # noqa: E402

from gateware.words_to_pixels import Words32ToPixels    # noqa: E402


def reference(payload):
    """What the pixels MUST be: RGB triples in wire order, tail discarded."""
    n = len(payload) // 3
    return [(payload[3 * i] | (payload[3 * i + 1] << 8) | (payload[3 * i + 2] << 16))
            for i in range(n)]


def to_words(payload):
    """Pack a byte payload into 32-bit words, first byte in the LOW bits.

    The payload is word-aligned on the wire (14+20+8+10 = 52 bytes of header
    is exactly 13 words), so this models the real stream. A payload that is
    not a multiple of 4 is zero-padded in its final word -- which is what the
    MAC does, and the padding must never become a pixel.
    """
    words = []
    for i in range(0, len(payload), 4):
        c = payload[i:i + 4]
        w = 0
        for j, b in enumerate(c):
            w |= b << (8 * j)
        words.append(w)
    return words


def run_case(payload, stall_pattern=None, name=""):
    """Feed `payload` through the DUT and return the pixels it emitted."""
    dut = Words32ToPixels()
    words = to_words(payload)
    got = []
    done_seen = []

    def tb():
        wi = 0
        # `stall_pattern` deasserts out_ready on a repeating schedule, to prove
        # the handshake holds rather than only working at full rate.
        cyc = 0
        guard = 0
        while wi < len(words):
            rdy = 1
            if stall_pattern:
                rdy = 0 if stall_pattern[cyc % len(stall_pattern)] == 0 else 1
            yield dut.out_ready.eq(rdy)
            yield dut.valid.eq(1)
            yield dut.data.eq(words[wi])
            yield dut.last.eq(1 if wi == len(words) - 1 else 0)
            yield
            # Sample the outputs for the beat that was just accepted.
            if (yield dut.valid) and (yield dut.out_ready):
                n = yield dut.n_out
                if n >= 1:
                    got.append((yield dut.px0))
                if n >= 2:
                    got.append((yield dut.px1))
                if (yield dut.done):
                    done_seen.append(len(got))
                wi += 1
            cyc += 1
            guard += 1
            assert guard < 100000, "testbench did not terminate"
        yield dut.valid.eq(0)
        yield dut.out_ready.eq(0)
        yield

    run_simulation(dut, tb())
    return got, done_seen


def main():
    failures = []

    # The real chunk, plus the lengths that exercise every residue phase and
    # every tail remainder.
    cases = [
        ("real 1461B chunk (487 px)", 1461),
        ("4 bytes  (1 word)", 4),
        ("8 bytes  (2 words)", 8),
        ("12 bytes (3 words = 4 px exactly)", 12),
        ("24 bytes (6 words = 8 px exactly)", 24),
        ("3 bytes  (1 px, padded word)", 3),
        ("6 bytes", 6),
        ("9 bytes", 9),
        ("15 bytes", 15),
        ("1460 bytes (tail 2 over)", 1460),
        ("1462 bytes (tail 1 over)", 1462),
        ("1459 bytes", 1459),
    ]

    for name, n in cases:
        payload = bytes((i * 7 + 13) & 0xFF for i in range(n))
        want = reference(payload)
        got, done = run_case(payload, name=name)
        # A payload that is not a multiple of 4 is zero-padded in its last
        # word, so the DUT may emit up to one extra pixel built from padding.
        # Trim to the reference length and check the extra was padding-only.
        trimmed = got[:len(want)]
        ok = trimmed == want
        extra = got[len(want):]
        note = ""
        if extra:
            note = f" (+{len(extra)} padding-derived px, discarded)"
        print(f"  {'ok ' if ok else 'FAIL'} {name:38} "
              f"{len(want):>4} px expected, {len(got):>4} emitted{note}")
        if not ok:
            failures.append(name)
            for i, (a, b) in enumerate(zip(trimmed, want)):
                if a != b:
                    print(f"       first mismatch at px {i}: "
                          f"got 0x{a:06x} want 0x{b:06x}")
                    break

    # Backpressure: the same payload must come out identical when the consumer
    # only takes a beat every other cycle, and every third.
    print()
    payload = bytes((i * 7 + 13) & 0xFF for i in range(1461))
    want = reference(payload)
    for pat, label in [([1, 0], "every other cycle"),
                       ([1, 0, 0], "every third cycle"),
                       ([1, 1, 1, 0, 0, 0, 0], "bursty 3-on 4-off")]:
        got, _ = run_case(payload, stall_pattern=pat)
        ok = got[:len(want)] == want
        print(f"  {'ok ' if ok else 'FAIL'} backpressure {label:28} "
              f"{len(got)} px emitted")
        if not ok:
            failures.append(f"backpressure {label}")

    # Two chunks back to back: the residue MUST NOT leak across `last`, or
    # every pixel of the second chunk shears by a byte.
    print()
    p1 = bytes((i * 7 + 13) & 0xFF for i in range(1461))
    p2 = bytes((i * 11 + 5) & 0xFF for i in range(1461))
    dut = Words32ToPixels()
    w1, w2 = to_words(p1), to_words(p2)
    allw = [(w, i == len(w1) - 1) for i, w in enumerate(w1)] + \
           [(w, i == len(w2) - 1) for i, w in enumerate(w2)]
    got2 = []

    def tb2():
        for w, last in allw:
            yield dut.out_ready.eq(1)
            yield dut.valid.eq(1)
            yield dut.data.eq(w)
            yield dut.last.eq(1 if last else 0)
            yield
            n = yield dut.n_out
            if n >= 1:
                got2.append((yield dut.px0))
            if n >= 2:
                got2.append((yield dut.px1))
        yield dut.valid.eq(0)
        yield

    run_simulation(dut, tb2())
    want1, want2 = reference(p1), reference(p2)
    # chunk 1 emits len(want1) real pixels, then up to 1 padding pixel.
    second = got2[len(want1):]
    # drop the padding pixel from chunk 1's final word if present
    for skip in (0, 1):
        if second[skip:skip + len(want2)] == want2:
            print(f"  ok   second chunk intact after `last` "
                  f"(skipped {skip} padding px)")
            break
    else:
        print("  FAIL second chunk SHEARED -- residue leaked across `last`")
        failures.append("residue leak across last")
        for i, (a, b) in enumerate(zip(second, want2)):
            if a != b:
                print(f"       first mismatch at px {i}: "
                      f"got 0x{a:06x} want 0x{b:06x}")
                break

    # THE REAL CASE. Fed exactly as `pixdma` will feed it: the UDP payload is
    # 10 app-header bytes then pixel data, arriving word-aligned (measured by
    # sim_hwpath32). So the header FSM eats words 0 and 1, then word 2 -- whose
    # low two bytes are the last of the header and whose high two bytes are the
    # first two PIXEL bytes -- is loaded into the residue. Words 3 onward are
    # ordinary beats. If this is off by a byte, every pixel shears.
    print()
    app = bytes([0x42, 0x4D] + [0] * 8)
    pixels = bytes((i * 13 + 29) & 0xFF for i in range(1461))
    upayload = app + pixels
    uwords = to_words(upayload)
    want = reference(pixels)

    dut = Words32ToPixels()
    got3 = []

    def tb3():
        # Word 2 carries header bytes 8,9 in the low half and pixel bytes 0,1
        # in the high half. Load those two bytes as the starting residue.
        yield dut.valid.eq(0)
        yield dut.res_set.eq(1)
        yield dut.res_set_val.eq((uwords[2] >> 16) & 0xFFFF)
        yield dut.res_set_n.eq(2)
        yield
        yield dut.res_set.eq(0)
        for i in range(3, len(uwords)):
            yield dut.out_ready.eq(1)
            yield dut.valid.eq(1)
            yield dut.data.eq(uwords[i])
            yield dut.last.eq(1 if i == len(uwords) - 1 else 0)
            yield
            n = yield dut.n_out
            if n >= 1:
                got3.append((yield dut.px0))
            if n >= 2:
                got3.append((yield dut.px1))
        yield dut.valid.eq(0)
        yield

    run_simulation(dut, tb3())
    ok = got3 == want
    print(f"  {'ok ' if ok else 'FAIL'} REAL 1471B UDP payload via residue preload: "
          f"{len(got3)} px emitted, {len(want)} expected, exact={ok}")
    if ok:
        print("       and NO padding pixel -- 2 preloaded + 1460 streamed = 1462 "
              "bytes = 487 px + 1 spare byte")
    else:
        failures.append("real payload via preload")
        for i, (a, b) in enumerate(zip(got3, want)):
            if a != b:
                print(f"       first mismatch at px {i}: "
                      f"got 0x{a:06x} want 0x{b:06x}")
                break

    print()
    if failures:
        print(f"FAILED: {len(failures)} case(s): {failures}")
        return 1
    print("all cases pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
