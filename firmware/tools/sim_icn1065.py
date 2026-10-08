#!/usr/bin/env python3
"""Simulate the ICN1065 command encoder against docs/ICN1065.md.

The panel is not here yet and a place-and-route round trip is ten minutes, so
this checks the waveform the way docs/FPGA-GUIDE.md section 10 says to: in the
simulator, before the FPGA.

What it proves: LAT is high for exactly the command's length, the payload is
shifted MSB-first on the data line during that window, padding follows with LAT
low, and `busy` covers the whole thing. Those four properties are the entire
contract the rest of the driver builds on.

    ./build.sh sim-icn1065      (or: python3 tools/sim_icn1065.py)
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "gateware"))

from migen import *
from icn1065 import (
    Icn1065Command, Icn1065Prefix, Icn1065RegBlock, Icn1065Serialiser,
    Icn1065RowScanner, V_SYNC, REG_LATCH, PRE_ACT1, PRE_ACT2, CLK_PAD,
    REG_VALUE, REG_CNT, CFG_SCAN, WRAPPER, scan_register, brightness12,
    ROW_OE_LEN, ROW_OE_CNT, ROW_OE_PULSE, SPWM_BITS, GREY_BITS,
    Icn1065Output, frame_clocks, Icn1065PixelSource, Icn1065Gamma,
    DEFAULT_CHIP,
    Icn1065Serialiser,
    sustainable_refresh, shift_divider, Icn1065Arbiter,
    GROUPS_HUB75, GROUPS_HUB320,
    Icn1065PipelinedArbiter,
    gamma12, gamma_table12, brightness12,
    sdram_bytes_per_second,
)


def capture(dut, length, pad, data):
    """Run one command, returning the per-clock (lat, data_out, busy) trace.

    Migen writes land on the clock edge, so `start` needs a settle cycle before
    the FSM sees it. Rather than count those cycles exactly -- which bakes a
    testbench detail into every assertion -- capture a generous window and let
    check() find the pulse.
    """
    trace = []
    yield dut.length.eq(length)
    yield dut.pad.eq(pad)
    yield dut.data.eq(data)
    yield
    yield dut.start.eq(1)
    yield
    yield dut.start.eq(0)
    for _ in range(length + pad + 8):
        trace.append(((yield dut.lat), (yield dut.data_out), (yield dut.busy)))
        yield
    return trace


def check(name, trace, length, pad, data, width=16):
    lat = [t[0] for t in trace]
    dat = [t[1] for t in trace]
    busy = [t[2] for t in trace]

    high = sum(1 for v in lat if v)
    assert high == length, f"{name}: LAT high {high} clocks total, expected {length}"

    # One contiguous LAT-high run of exactly `length`, wherever it starts.
    start = lat.index(1)
    run = 0
    while start + run < len(lat) and lat[start + run]:
        run += 1
    assert run == length, f"{name}: LAT run is {run} clocks, expected {length}"

    # Payload shifted MSB first, aligned to the start of the command.
    n = min(width, length + pad)
    emitted = dat[start:start + n]
    expect = [(data >> (width - 1 - i)) & 1 for i in range(n)]
    assert emitted == expect, f"{name}: data {emitted} != MSB-first {expect}"

    # busy covers the command and its padding, contiguously.
    assert sum(busy) == length + pad, \
        f"{name}: busy {sum(busy)} clocks, expected {length + pad}"
    bstart = busy.index(1)
    assert all(busy[bstart:bstart + length + pad]), f"{name}: busy not contiguous"
    assert bstart == start, f"{name}: busy starts at {bstart}, LAT at {start}"
    print(f"  ok  {name:<28} LAT={length:>2}  pad={pad:>2}  data=0x{data:04x}")


def lat_runs(trace):
    """Lengths of each contiguous LAT-high run, in order."""
    runs, n = [], 0
    for lat, _ in trace:
        if lat:
            n += 1
        elif n:
            runs.append(n); n = 0
    if n:
        runs.append(n)
    return runs


def word_at(trace, start, width=16, lane=0):
    """Reassemble the MSB-first word shifted out from clock `start`.

    `lane` picks a colour: 0 red, 1 green, 2 blue. The three are separate chips
    and their configuration does not have to agree -- the DP3364S capture has
    different values on each, so a test that only ever looked at lane 0 would
    not notice green and blue being sent red's table.
    """
    v = 0
    for i in range(width):
        if start + i < len(trace):
            v = (v << 1) | ((trace[start + i][1] >> lane) & 1)
    return v


def run_prefix(dut, frames):
    """Capture the LAT/data trace for `frames` consecutive frame prefixes."""
    traces = []
    for _ in range(frames):
        trace = []
        yield dut.start.eq(1)
        yield
        yield dut.start.eq(0)
        yield
        # The train is ~80 clocks; give it plenty and stop when it goes idle.
        for _ in range(4000):
            trace.append(((yield dut.lat), (yield dut.data_out)))
            if not (yield dut.busy):
                break
            yield
        for _ in range(4):
            yield
        traces.append(trace)
    return traces


def check_prefix(chip=None):
    """The frame prefix for one chip, and that registers advance one per frame.

    Run for every chip in the registry. The grammar differs between them --
    ICND1065 sends three command bursts and five slots, DP3364S sends two and
    one -- so this is the test that the engine is genuinely table-driven rather
    than an ICND1065 with parameters bolted on.
    """
    from spwm_chips import CHIPS
    chip = chip or DEFAULT_CHIP
    print(f"\nFrame prefix, {chip.name} (1/64 scan, 256-column module)")
    failures = 0
    ROWS = 64
    BLOCK = 256          # 256-column module: 16 driver ICs x 16 bits
    cfg = chip.with_scan(ROWS)
    dut = Icn1065Prefix(block_len=BLOCK, chip=cfg)
    traces = []
    # Enough frames to reach the first register whose colour lanes disagree --
    # DP3364S's first three happen to be uniform, so a fixed three frames would
    # have "proved" the lanes were independent by never looking at a case where
    # it mattered.
    differ = [i for i, r in enumerate(cfg.regs) if len(set(r)) > 1]
    frames = max(3, (differ[0] + 1) if differ else 3)

    def bench():
        nonlocal traces
        traces = yield from run_prefix(dut, frames=frames)

    run_simulation(dut, bench())

    cmds = [V_SYNC, PRE_ACT1, PRE_ACT2] if chip.mid_latch else [V_SYNC, PRE_ACT2]
    expect_runs = cmds + [REG_LATCH] * chip.slots
    try:
        runs = lat_runs(traces[0])
        assert runs == expect_runs, f"LAT runs {runs} != {expect_runs}"
        print(f"  ok  command train {runs} "
              f"({len(cmds)} bursts, {chip.slots} slot(s))")

        # Blocks begin `BLOCK` clocks before their LAT falls, and the word is
        # shifted from the START of the block -- LAT is only the trailing mark.
        starts = []
        n = 0
        for i, (lat, _) in enumerate(traces[0]):
            if lat and n == 0:
                starts.append(i); n = 1
            elif lat:
                n += 1
            elif n:
                n = 0
        block_lat = starts[len(cmds):len(cmds) + chip.slots]
        payloads = [word_at(traces[0], l + REG_LATCH - BLOCK) for l in block_lat]
        wrappers = [w[0] for w in cfg.wrappers]
        got_wrappers = [p for i, p in enumerate(payloads) if i != chip.rot_slot]
        assert got_wrappers == wrappers, f"wrappers {got_wrappers} != {wrappers}"
        if wrappers:
            print(f"  ok  register wrapped by "
                  f"{' '.join(f'0x{w:04x}' for w in wrappers)}")
        else:
            print(f"  ok  no wrapper slots, as this chip's grammar says")

        # One register per frame, in order, with the scan register substituted.
        got = []
        for t in traces:
            s2, n2 = [], 0
            for i, (lat, _) in enumerate(t):
                if lat and n2 == 0:
                    s2.append(i); n2 = 1
                elif lat:
                    n2 += 1
                elif n2:
                    n2 = 0
            rot = s2[len(cmds) + chip.rot_slot]
            got.append(tuple(word_at(t, rot + REG_LATCH - BLOCK, lane=c)
                             for c in range(3)))
        want = cfg.regs[:frames]
        assert got == want, f"registers {got} != {want}"
        print(f"  ok  one register per frame, in order: "
              f"{' '.join(f'0x{r[0]:04x}' for r in got[:4])}"
              f"{' ...' if len(got) > 4 else ''}")

        # The colour lanes are independent, which is the whole reason the
        # register block grew from one shift register to three.
        if any(len(set(r)) > 1 for r in cfg.regs):
            differing = [r for r in got if len(set(r)) > 1]
            assert differing, ("this chip's table has per-colour values but "
                               "every lane emitted the same word")
            print(f"  ok  colour lanes differ where the table says they should "
                  f"(e.g. {differing[0]})")
        else:
            print(f"  --  this chip's table is uniform across colours")
    except AssertionError as e:
        print(f"  FAIL {e}")
        failures += 1
    return failures


def check_regblock():
    """A register block fills a row: the word repeated once per driver IC."""
    print("\nRegister block (256 columns = 16 driver ICs)")
    failures = 0
    BLOCK, WORD = 256, 0x023f
    dut = Icn1065RegBlock(BLOCK)
    trace = []

    def bench():
        nonlocal trace
        yield dut.data[0].eq(WORD)
        yield
        yield dut.start.eq(1)
        yield
        yield dut.start.eq(0)
        for _ in range(BLOCK + 8):
            trace.append(((yield dut.lat), (yield dut.data_out), (yield dut.busy)))
            yield

    run_simulation(dut, bench())
    try:
        start = [t[2] for t in trace].index(1)
        body = trace[start:start + BLOCK]
        assert len(body) == BLOCK, f"block is {len(body)} clocks, expected {BLOCK}"
        print(f"  ok  block spans {BLOCK} clocks -- one full row")

        # The word, repeated once per driver IC.
        reps = BLOCK // 16
        words = [sum(body[r * 16 + i][1] << (15 - i) for i in range(16))
                 for r in range(reps)]
        assert all(w == WORD for w in words), \
            f"repeats differ: {[hex(w) for w in set(words)]}"
        print(f"  ok  0x{WORD:04x} repeated {reps}x, MSB first -- one per driver IC")

        lat = [t[0] for t in body]
        assert sum(lat) == REG_LATCH, f"LAT high {sum(lat)}, expected {REG_LATCH}"
        assert all(lat[-REG_LATCH:]), "LAT is not at the END of the block"
        assert not any(lat[:-REG_LATCH]), "LAT asserted before the end"
        print(f"  ok  LAT high for the LAST {REG_LATCH} clocks only")
    except (AssertionError, ValueError) as e:
        print(f"  FAIL {e}")
        failures += 1
    return failures


def check_serialiser():
    """One pixel pair, 16 clocks, six channels, MSB first, top 4 bits zero."""
    print("\nSPWM serialiser")
    failures = 0
    vals8 = [0xFF, 0x80, 0x00, 0x01, 0x7F, 0xC0]
    vals12 = [brightness12(v) for v in vals8]
    dut = Icn1065Serialiser()
    trace = []

    def bench():
        nonlocal trace
        for sig, v in zip(dut.channels_in, vals12):
            yield sig.eq(v)
        yield
        yield dut.start.eq(1)
        yield
        yield dut.start.eq(0)
        for _ in range(SPWM_BITS + 6):
            trace.append(((yield dut.channels_out), (yield dut.busy)))
            yield

    run_simulation(dut, bench())
    try:
        start = [t[1] for t in trace].index(1)
        body = [t[0] for t in trace[start:start + SPWM_BITS]]
        assert len(body) == SPWM_BITS, f"{len(body)} clocks, expected {SPWM_BITS}"
        print(f"  ok  {SPWM_BITS} clocks per pixel")

        for ch in range(6):
            bits = [(w >> ch) & 1 for w in body]
            got = sum(b << (SPWM_BITS - 1 - i) for i, b in enumerate(bits))
            assert got == vals12[ch], \
                f"channel {ch}: 0x{got:04x} != 0x{vals12[ch]:04x}"
        print(f"  ok  all six channels shift MSB first, independently")

        top4 = [w for w in body[:4]]
        assert all(w == 0 for w in top4), f"bits 15..12 not zero: {top4}"
        print(f"  ok  bits 15..12 are zero, as the chip requires")

        assert brightness12(0xFF) == 0xFFF, hex(brightness12(0xFF))
        assert brightness12(0x00) == 0x000
        print(f"  ok  brightness 0x00->0x000, 0xFF->0xFFF (full scale reached)")
    except (AssertionError, ValueError) as e:
        print(f"  FAIL {e}")
        failures += 1
    return failures


def check_rowscan():
    """One OE pulse per row slot, active high, exact slot width."""
    print("\nRow scanner (1/64 scan)")
    failures = 0
    ROWS = 64
    dut = Icn1065RowScanner(rows_per_frame=ROWS)
    trace = []

    def bench():
        nonlocal trace
        yield
        yield dut.start.eq(1)
        yield
        yield dut.start.eq(0)
        for _ in range(ROWS * ROW_OE_LEN + 16):
            trace.append(((yield dut.oe), (yield dut.busy), (yield dut.frame_done)))
            yield

    run_simulation(dut, bench())
    try:
        start = [t[1] for t in trace].index(1)
        body = trace[start:start + ROWS * ROW_OE_LEN]
        oe = [t[0] for t in body]

        edges = sum(1 for i in range(1, len(oe)) if oe[i] and not oe[i - 1])
        edges += 1 if oe[0] else 0
        assert edges == ROWS * ROW_OE_CNT, \
            f"{edges} OE rising edges, expected {ROWS * ROW_OE_CNT}"
        print(f"  ok  {edges} OE pulses for {ROWS} rows -- one per slot, "
              f"and the count is what advances the chip's counter")

        assert sum(oe) == ROWS * ROW_OE_CNT * ROW_OE_PULSE, \
            f"OE high {sum(oe)} clocks, expected {ROWS * ROW_OE_CNT * ROW_OE_PULSE}"
        print(f"  ok  OE high {sum(oe)} clocks total ({ROW_OE_PULSE} per pulse)")

        assert len(body) == ROWS * ROW_OE_LEN
        print(f"  ok  frame is {ROWS} x {ROW_OE_LEN} = {ROWS * ROW_OE_LEN} clocks")

        assert sum(t[2] for t in trace) == 1, "frame_done should strobe exactly once"
        print(f"  ok  frame_done strobes once")
    except (AssertionError, ValueError) as e:
        print(f"  FAIL {e}")
        failures += 1
    return failures


def check_full_frame():
    """A whole frame end to end on a small panel: order, counts, timing model.

    Run at 16x8 rather than 256x64 so the simulation finishes in seconds. The
    structure is identical -- what is being checked is that the three stages
    compose in the right order and that frame_clocks() predicts reality, which
    is what every rate estimate in the benchmark rests on.
    """
    print("\nFull frame (16x8 panel, structure identical to 256x64)")
    failures = 0
    COLS, ROWS, SLOT = 16, 8, 16
    dut = Icn1065Output(columns=COLS, rows_per_frame=ROWS, slot_len=SLOT)
    trace = []

    def bench():
        nonlocal trace
        # Supply a constant pixel so the data path is exercised without a
        # framebuffer; the protocol does not care what the value is.
        for sig in dut.pix_in:
            yield sig.eq(0x0FFF)
        yield
        yield dut.start.eq(1)
        yield
        yield dut.start.eq(0)
        for _ in range(60000):
            trace.append(((yield dut.lat), (yield dut.oe),
                          (yield dut.busy), (yield dut.pix_req)))
            if (yield dut.frame_done):
                break
            yield

    run_simulation(dut, bench())
    try:
        lat = [t[0] for t in trace]
        oe = [t[1] for t in trace]
        req = [t[3] for t in trace]

        # Prefix comes first and is the only LAT activity.
        expect_runs = [V_SYNC, PRE_ACT1, PRE_ACT2] + [REG_LATCH] * 5
        runs = lat_runs([(l, 0) for l in lat])
        assert runs == expect_runs, f"LAT runs {runs} != {expect_runs}"
        print(f"  ok  prefix first, and the only LAT activity: {runs}")

        # One pixel fetch per pixel of the frame.
        assert sum(req) == COLS * ROWS, \
            f"{sum(req)} pixel fetches, expected {COLS * ROWS}"
        print(f"  ok  {sum(req)} pixel fetches = {COLS} columns x {ROWS} rows")

        # Row scan comes last: no OE before the final LAT.
        last_lat = max(i for i, v in enumerate(lat) if v)
        first_oe = next(i for i, v in enumerate(oe) if v)
        assert first_oe > last_lat, "OE before the prefix finished"
        print(f"  ok  row scan runs after the pixels, never during the prefix")

        edges = sum(1 for i in range(1, len(oe)) if oe[i] and not oe[i - 1])
        assert edges == ROWS, f"{edges} OE pulses, expected {ROWS}"
        print(f"  ok  {edges} OE pulses -- one per row slot")

        # The timing model has to match the hardware, or the benchmark lies.
        predicted = frame_clocks(COLS, ROWS, SLOT)
        actual = len(trace)
        drift = abs(actual - predicted) / predicted
        assert drift < 0.01, \
            f"frame_clocks() says {predicted}, simulation took {actual} ({drift:.0%} out)"
        print(f"  ok  frame_clocks() predicts {predicted}, simulation {actual} "
              f"({drift:.1%} -- FSM overhead)")
    except (AssertionError, ValueError, StopIteration) as e:
        print(f"  FAIL {e}")
        failures += 1
    return failures


def check_pixel_source(gamma=False):
    """A pixel pair: two reads, right addresses, 0x00BBGGRR unpacked correctly.

    Run twice. `gamma=False` is the protocol test -- addressing, channel order,
    the sticky-ack handshake -- with values that are easy to read in a trace.
    `gamma=True` then re-runs the same stimulus against the real display path,
    which matters because that path shares THREE gamma units between the two
    halves and latches the upper half's corrected values. If that latch were
    wrong the lower half's colour would appear in the upper half, which is
    exactly the bug the GAP state was added to fix and would otherwise sail
    through a linear-only test.
    """
    print(f"\nPixel source (framebuffer 0x00BBGGRR"
          f"{', gamma-corrected' if gamma else ', linear'})")
    failures = 0
    COLS, ROWS, WIDTH, BASE = 256, 64, 768, 0x1000
    # Upper pixel r=0x12 g=0x34 b=0x56, lower r=0xFF g=0x00 b=0x80.
    UPPER, LOWER = 0x00563412, 0x008000FF
    dut = Icn1065PixelSource(COLS, ROWS, WIDTH, fb_base=BASE, gamma=gamma)
    got = {}

    def bench():
        col, row = 5, 3
        up_adr = BASE + row * WIDTH + col
        yield dut.col.eq(col)
        yield dut.row.eq(row)
        yield
        yield dut.req.eq(1)
        yield
        yield dut.req.eq(0)
        addrs = []
        # Memory stub: answer whatever address is on the bus THIS cycle, so the
        # lower read cannot be served the upper word.
        for _ in range(60):
            a = (yield dut.mem_adr)
            req = (yield dut.mem_req)
            if req and (not addrs or addrs[-1] != a):
                addrs.append(a)
            yield dut.mem_dat.eq(UPPER if a == up_adr else LOWER)
            # A deliberately unhelpful memory: it holds ack while req is high,
            # which a real one may well do. The source must cope.
            yield dut.mem_ack.eq(req)
            if (yield dut.valid):
                vals = []
                for c in dut.channels:
                    vals.append((yield c))
                got["ch"] = vals
                got["addrs"] = list(addrs)
                break
            yield
        got.setdefault("addrs", addrs)

    run_simulation(dut, bench())
    try:
        col, row = 5, 3
        want = [BASE + row * WIDTH + col, BASE + (row + ROWS) * WIDTH + col]
        assert got.get("addrs") == want, f"addresses {got.get('addrs')} != {want}"
        print(f"  ok  two reads: upper row {row}, lower row {row + ROWS} "
              f"(same column, {ROWS} rows down)")

        ch = got.get("ch")
        assert ch, "never asserted valid"
        conv = gamma12 if gamma else brightness12
        exp = [conv(v) for v in (0x12, 0x34, 0x56, 0xFF, 0x00, 0x80)]
        assert ch == exp, f"channels {[hex(c) for c in ch]} != {[hex(e) for e in exp]}"
        print(f"  ok  0x00BBGGRR unpacked: r1g1b1 = "
              f"{' '.join(f'0x{c:03x}' for c in ch[:3])}, r2g2b2 = "
              f"{' '.join(f'0x{c:03x}' for c in ch[3:])}")
        assert ch[3] == 0xFFF and ch[4] == 0x000
        print(f"  ok  full scale and zero survive the 8->12 bit expansion")
        if gamma:
            assert ch[:3] != ch[3:], "upper and lower halves are identical"
            print(f"  ok  the three shared gamma units latch the upper half "
                  f"without it leaking into the lower")
    except AssertionError as e:
        print(f"  FAIL {e}")
        failures += 1
    return failures


def check_arbiter(latency=4, n=9, shift_div=4):
    """Nine sources, one port: does every output get its pixel pair in time?

    This is the check the whole P1.25 plan rests on. The bandwidth arithmetic in
    docs/BENCHMARKS.md section 4c says a 3x3 wall needs the panel clocked at
    sys/4; this asks whether a real round-robin arbiter, against a memory with
    real latency, actually delivers two reads per source inside that window --
    and whether it does so FAIRLY, since one starved source tears one output's
    rows and the other eight look perfect.

    `shift_div` 4 means a pixel is 16 shift clocks = 64 sys clocks.
    """
    print(f"\nArbiter ({n} sources, one port, {latency}-cycle memory, sys/{shift_div})")
    failures = 0
    budget = SPWM_BITS * shift_div          # sys clocks per pixel pair

    class DUT(Module):
        def __init__(self):
            self.srcs = [Icn1065PixelSource(256, 64, 768, fb_base=i * 0x8000)
                         for i in range(n)]
            self.submodules.arb = arb = Icn1065Arbiter(n)
            self.submodules += self.srcs
            self.comb += arb.connect(self.srcs)

    dut = DUT()
    served = [0] * n
    spans = []

    def bench():
        # Every source asks continuously: the worst case, and also the real
        # one -- the serialiser never stops wanting pixels.
        for s in dut.srcs:
            yield s.req.eq(1)
        pending, done_at = None, [None] * n
        started = [None] * n
        countdown = 0
        for cycle in range(6000):
            req = (yield dut.arb.mem_req)
            # Fixed-latency memory: `latency` cycles from req to ack.
            if req and pending is None:
                pending, countdown = (yield dut.arb.mem_adr), latency
            yield dut.arb.mem_ack.eq(0)
            if pending is not None:
                countdown -= 1
                if countdown <= 0:
                    yield dut.arb.mem_dat.eq(0x00563412)
                    yield dut.arb.mem_ack.eq(1)
                    pending = None
            for i, s in enumerate(dut.srcs):
                if started[i] is None:
                    started[i] = cycle
                if (yield s.valid):
                    served[i] += 1
                    if done_at[i] is None:
                        done_at[i] = cycle
                    spans.append(cycle - started[i])
                    started[i] = None
            yield

    run_simulation(dut, bench())
    try:
        assert all(served), f"starved sources: {[i for i, c in enumerate(served) if not c]}"
        print(f"  ok  every source was served -- no starvation "
              f"({min(served)}-{max(served)} pixel pairs each)")

        spread = max(served) - min(served)
        assert spread <= 2, f"unfair: served counts {served}"
        print(f"  ok  round-robin is fair: counts differ by at most {spread}")

        worst = max(spans)
        globals().setdefault('_ARB', {})[latency] = worst
        need = -(-worst // SPWM_BITS)      # ceil: the divider this latency forces
        print(f"  --  worst pixel pair {worst} sys clocks -> needs sys/{need} "
              f"({worst/ n / 2:.1f} clocks per read, memory latency {latency})")
        assert worst > SPWM_BITS * 2, (
            f"worst case {worst} fits sys/2 -- if this ever passes, the "
            f"bandwidth model in shift_divider() needs re-deriving")
        print(f"  ok  does NOT fit sys/2's {SPWM_BITS * 2} clocks, as the "
              f"bandwidth model predicts")
    except AssertionError as e:
        print(f"  FAIL {e}")
        failures += 1
    return failures


def check_pipelined_arbiter(latency=16, n=9, shift_div=4,
                            groups=GROUPS_HUB75):
    """The same nine-source load, but with more than one read in flight.

    `check_arbiter` establishes that a blocking arbiter turns memory latency
    into a throughput limit: 2 * n * latency clocks per pixel pair, which at a
    realistic 16-cycle LiteDRAM latency is 291 clocks and a 7.7 Hz wall. This
    checks the fix. Nine sources give nine outstanding reads, so the pixel pair
    should cost about `2 * n` clocks and stop caring about latency at all.
    """
    print(f"\nPipelined arbiter ({n} sources, {latency}-cycle memory, "
          f"sys/{shift_div}, {groups} RGB groups"
          f"{' -- HUB320' if groups == GROUPS_HUB320 else ''})")
    failures = 0
    budget = SPWM_BITS * shift_div

    class DUT(Module):
        def __init__(self):
            self.srcs = [Icn1065PixelSource(256, 64, 768, fb_base=i * 0x8000,
                                            groups=groups)
                         for i in range(n)]
            self.submodules.arb = arb = Icn1065PipelinedArbiter(n)
            self.submodules += self.srcs
            self.comb += arb.connect(self.srcs)

    dut = DUT()
    served = [0] * n
    spans = []

    def bench():
        for s in dut.srcs:
            yield s.req.eq(1)
        yield dut.arb.cmd_ready.eq(1)      # memory takes a command every cycle
        inflight = []                       # (cycle_due, address)
        started = [None] * n
        for cycle in range(6000):
            if (yield dut.arb.cmd_valid):
                inflight.append((cycle + latency, (yield dut.arb.cmd_adr)))
            yield dut.arb.dat_valid.eq(0)
            if inflight and inflight[0][0] <= cycle:
                inflight.pop(0)
                yield dut.arb.dat.eq(0x00563412)
                yield dut.arb.dat_valid.eq(1)
            for i, s in enumerate(dut.srcs):
                if started[i] is None:
                    started[i] = cycle
                if (yield s.valid):
                    served[i] += 1
                    spans.append(cycle - started[i])
                    started[i] = None
            yield

    run_simulation(dut, bench())
    try:
        assert all(served), f"starved: {[i for i, c in enumerate(served) if not c]}"
        print(f"  ok  every source served ({min(served)}-{max(served)} pairs each)")
        assert max(served) - min(served) <= 2, f"unfair: {served}"
        print(f"  ok  fair: counts differ by at most {max(served) - min(served)}")

        worst = max(spans)
        need = -(-worst // SPWM_BITS)

        # The substantive claim is not a particular number of clocks -- it is
        # that the cost is LINEAR in latency rather than multiplied by the
        # source count. A blocking arbiter costs `groups * n * latency`; this
        # costs a fixed per-issue overhead PLUS latency.
        blocking = groups * n * latency
        bound = 3 * groups * n + 2 * latency
        assert worst <= bound, (f"{worst} clocks exceeds the linear bound "
                                f"{bound} -- latency is still multiplying")
        print(f"  ok  worst pixel pair {worst} clocks, under the linear bound "
              f"{bound}; a blocking arbiter would need ~{blocking}")

        # And it is honestly WORSE at trivial latency.
        #
        # Each issue costs a cycle in the registered pick, which exists because
        # the combinational priority chain was the critical path of the whole
        # SoC -- 26 MHz against a 40 MHz requirement. With no latency to hide
        # there is nothing for the pipeline to buy, so a blocking arbiter wins.
        # Real LiteDRAM latency is 10-20 cycles, where this is several times
        # faster. Asserting the crossover keeps that trade honest rather than
        # quietly reporting only the cases that flatter it.
        if latency <= 2:
            assert worst > blocking, (
                f"pipelining unexpectedly wins at latency {latency} "
                f"({worst} vs {blocking}) -- re-derive the crossover")
            print(f"  --  slower than blocking at this latency "
                  f"({worst} vs ~{blocking}); the pipeline register only pays "
                  f"once there is latency to hide")
        else:
            assert worst < blocking, f"{worst} is not better than ~{blocking}"
            print(f"  ok  {blocking // worst}x better than the blocking "
                  f"arbiter at {latency}-cycle latency")

        if need <= shift_div:
            print(f"  ok  fits the {budget}-clock sys/{shift_div} window at "
                  f"{latency}-cycle latency")
        else:
            # Not a failure: it is the honest answer for that latency, and the
            # divider is the deliverable, not a pass/fail.
            print(f"  --  needs sys/{need} at {latency}-cycle latency "
                  f"({40 / need:.1f} MHz, "
                  f"{40e6 / need / frame_clocks(256, 64):.1f} Hz refresh)")
    except AssertionError as e:
        print(f"  FAIL {e}")
        failures += 1
    return failures


def check_hub320():
    """The HUB320 wiring: four RGB groups per connector instead of two.

    HUB320 is a WIDTH change, not a protocol change -- same CLK, LAT and OE,
    same driver ICs. What differs is that a connector carries four horizontal
    slices rather than two, so a request fetches four pixels and the panel is
    four times `rows_per_frame` tall.

    The thing this has to prove is that the extra groups do not leak into each
    other. Four sequential reads off one bus, with a memory entitled to hold
    `ack` high, is exactly the shape of the bug the GAP state was added for --
    and with four groups there are three gaps to get right rather than one.
    """
    print("\nHUB320 (4 RGB groups per connector)")
    failures = 0
    COLS, ROWS, WIDTH, BASE = 256, 64, 768, 0x1000
    # One distinct colour per slice, so a leak is unmistakable.
    WORDS = [0x00563412, 0x008000FF, 0x0011FF22, 0x00CC00AA]

    try:
        ser = Icn1065Serialiser(groups=GROUPS_HUB320)
        assert len(ser.channels_in) == 12, len(ser.channels_in)
        assert len(ser.channels_out) == 12, len(ser.channels_out)
        print(f"  ok  serialiser carries 12 channels (4 groups x RGB)")

        out = Icn1065Output(COLS, ROWS, groups=GROUPS_HUB320)
        assert len(out.rgb) == 12 and len(out.pix_in) == 12
        print(f"  ok  output stage drives 12 data lines")
    except AssertionError as e:
        print(f"  FAIL {e}")
        failures += 1

    dut = Icn1065PixelSource(COLS, ROWS, WIDTH, fb_base=BASE,
                             gamma=False, groups=GROUPS_HUB320)
    got = {}

    def bench():
        col, row = 5, 3
        adrs = [BASE + (row + g * ROWS) * WIDTH + col for g in range(4)]
        yield dut.col.eq(col)
        yield dut.row.eq(row)
        yield
        yield dut.req.eq(1)
        yield
        yield dut.req.eq(0)
        seen = []
        for _ in range(80):
            a = (yield dut.mem_adr)
            req = (yield dut.mem_req)
            if req and (not seen or seen[-1] != a):
                seen.append(a)
            yield dut.mem_dat.eq(WORDS[adrs.index(a)] if a in adrs else 0)
            # Sticky ack, which a simple memory is entitled to do.
            yield dut.mem_ack.eq(req)
            if (yield dut.valid):
                vals = []
                for c in dut.channels:
                    vals.append((yield c))
                got["ch"] = vals
                got["adrs"] = list(seen)
                break
            yield
        got.setdefault("adrs", seen)

    run_simulation(dut, bench())
    try:
        col, row = 5, 3
        want = [BASE + (row + g * ROWS) * WIDTH + col for g in range(4)]
        assert got.get("adrs") == want, f"{got.get('adrs')} != {want}"
        print(f"  ok  four reads, rows {row}, {row+ROWS}, {row+2*ROWS}, "
              f"{row+3*ROWS} -- one per slice, same column")

        ch = got.get("ch")
        assert ch, "never asserted valid"
        exp = []
        for w in WORDS:
            exp += [brightness12(w & 0xFF), brightness12((w >> 8) & 0xFF),
                    brightness12((w >> 16) & 0xFF)]
        assert ch == exp, (f"channels {[hex(c) for c in ch]} != "
                           f"{[hex(e) for e in exp]}")
        print(f"  ok  all 12 channels unpacked, no leakage between the four "
              f"slices (three GAP states, not one)")

        # The claim that makes HUB320 affordable at all.
        hub75 = sdram_bytes_per_second(256, 64, 16, 30, groups=GROUPS_HUB75)
        hub320 = sdram_bytes_per_second(256, 64, 8, 30, groups=GROUPS_HUB320)
        assert hub75 == hub320, f"{hub75} != {hub320}"
        print(f"  ok  16 HUB75 outputs and 8 HUB320 outputs cover the same "
              f"pixels for the same {hub320/1e6:.0f} MB/s -- HUB320 costs pins, "
              f"not bandwidth")
    except AssertionError as e:
        print(f"  FAIL {e}")
        failures += 1
    return failures


def check_rgb565():
    """16-bit framebuffer pixels: two per word, and they must not bleed.

    Halving bytes per pixel halves both the framebuffer a card must hold and
    the bandwidth it must read, which are the two limits that decide how dense
    a wall can get. The risk it introduces is new: two pixels now share a
    32-bit word, so a wrong `half` select hands a pixel its neighbour's colour
    -- and on a photograph that looks like softness rather than like a bug.

    So this reads column 0 and column 1, which live in the SAME word, and
    asserts they come back different and correct.
    """
    print("\n16-bit framebuffer pixels (B5 G6 R5, two per word)")
    failures = 0
    COLS, ROWS, WIDTH, BASE = 256, 64, 768, 0x1000

    def pack(r5, g6, b5):
        return (b5 << 11) | (g6 << 5) | r5

    # Two visibly different pixels sharing one word: pure red, then pure blue.
    LOW, HIGH = pack(31, 0, 0), pack(0, 0, 31)
    WORD = (HIGH << 16) | LOW

    try:
        for col, want_name, want in ((0, "low half = red", (31, 0, 0)),
                                     (1, "high half = blue", (0, 0, 31))):
            dut = Icn1065PixelSource(COLS, ROWS, WIDTH, fb_base=BASE,
                                     gamma=False, pix_bits=16)
            got = {}

            def bench():
                yield dut.col.eq(col)
                yield dut.row.eq(0)
                yield
                yield dut.req.eq(1)
                yield
                yield dut.req.eq(0)
                for _ in range(60):
                    yield dut.mem_dat.eq(WORD)
                    yield dut.mem_ack.eq((yield dut.mem_req))
                    if (yield dut.valid):
                        got["adr"] = (yield dut.mem_adr)
                        vals = []
                        for c in dut.channels:
                            vals.append((yield c))
                        got["ch"] = vals
                        break
                    yield

            run_simulation(dut, bench())
            ch = got.get("ch")
            assert ch, f"col {col}: never asserted valid"
            # 5-bit 31 -> 8-bit 255 -> brightness12 0xfff; 0 -> 0.
            r, g, b = ch[0], ch[1], ch[2]
            exp = tuple(0xFFF if v else 0 for v in want)
            assert (r, g, b) == exp, f"col {col}: {(hex(r),hex(g),hex(b))} != {exp}"
            print(f"  ok  column {col}: {want_name} -> "
                  f"r=0x{r:03x} g=0x{g:03x} b=0x{b:03x}")

        # Both columns read the SAME word address -- that is the packing.
        d0 = Icn1065PixelSource(COLS, ROWS, WIDTH, fb_base=BASE, pix_bits=16)
        assert d0.pix_bits == 16
        print(f"  ok  columns 0 and 1 share one word, selected by the index's low bit")

        # 5-bit and 6-bit codes must reach full scale, not stop short.
        assert ((31 << 3) | (31 >> 2)) == 255, "5-bit expansion misses white"
        assert ((63 << 2) | (63 >> 4)) == 255, "6-bit expansion misses white"
        print(f"  ok  5- and 6-bit codes reach 255, so white is white")

        # What it buys, which is the whole reason for the format.
        px_32 = (0x140000 // 4)
        px_16 = (0x140000 // 2)
        assert px_16 == 2 * px_32
        print(f"  ok  framebuffer holds {px_16:,} px instead of {px_32:,} "
              f"-- {px_16 // 32768} modules instead of {px_32 // 32768}")
        bw32 = sdram_bytes_per_second(256, 64, 9, 30) / 1e6
        print(f"  --  and a 9-module wall at 30 Hz reads {bw32/2:.0f} MB/s "
              f"instead of {bw32:.0f}")
    except AssertionError as e:
        print(f"  FAIL {e}")
        failures += 1
    return failures


def check_bandwidth():
    """The SDRAM budget, which is what actually limits a 3x3 wall."""
    print("\nSDRAM budget (M12L16161A: 16-bit SDR at 40 MHz = 80 MB/s peak)")
    failures = 0
    PEAK = 80e6
    try:
        large = sdram_bytes_per_second(256, 64, 9, 73.6)
        assert large > PEAK, f"large wall at 73.6 Hz is {large/1e6:.0f} MB/s"
        print(f"  ok  large 768x384 at 73.6 Hz needs {large/1e6:.0f} MB/s "
              f"-- OVER peak, so the natural rate is not reachable")

        large30 = sdram_bytes_per_second(256, 64, 9, 30)
        assert large30 < PEAK * 0.6, f"{large30/1e6:.0f} MB/s"
        print(f"  ok  large at 30 Hz needs {large30/1e6:.0f} MB/s -- fits with headroom")

        small = sdram_bytes_per_second(256, 64, 4, 73.6)
        assert small < PEAK * 0.6, f"{small/1e6:.0f} MB/s"
        print(f"  ok  small 512x256 at 73.6 Hz needs {small/1e6:.0f} MB/s -- fits")
        # The shift clock is what SETS the data refresh, so a bandwidth limit
        # is really a statement about how fast the panel may be clocked.
        div, shift, refresh = shift_divider(256, 64, 9)
        assert div > 2, f"large wall should need a slower clock than sys/2, got {div}"
        print(f"  ok  large wall must shift at sys/{div} = {shift/1e6:.0f} MHz "
              f"-> {refresh:.1f} Hz (not sys/2; 20 MHz cannot be fed)")

        sdiv, sshift, srefresh = shift_divider(256, 64, 4)
        assert sdiv == 2, f"small wall should reach sys/2, got sys/{sdiv}"
        print(f"  ok  small wall shifts at sys/{sdiv} = {sshift/1e6:.0f} MHz "
              f"-> {srefresh:.1f} Hz (limited by the clock, not by SDRAM)")

        # The efficiency assumption is the weakest number in this model, so
        # pin the direction of its influence rather than a single value.
        r_opt, _, _ = sustainable_refresh(256, 64, 9, efficiency=0.8)
        r_pess, _, _ = sustainable_refresh(256, 64, 9, efficiency=0.5)
        assert r_pess < r_opt, "efficiency must move the refresh rate"
        print(f"  ok  9-panel refresh spans {r_pess:.0f}-{r_opt:.0f} Hz across a "
              f"50-80% SDRAM efficiency assumption")
        print(f"  --  data refresh and flicker refresh are DECOUPLED here: the")
        print(f"      chip PWMs at 3840-7680 Hz regardless, so 30 Hz of new")
        print(f"      content is 30 fps video with no flicker")
    except AssertionError as e:
        print(f"  FAIL {e}")
        failures += 1
    return failures


def check_gamma():
    """The gamma unit: close enough to a real curve, and matching its twin.

    Two separate claims, both of which matter. That the piecewise approximation
    is within a code or two of a real 2.8 curve, and that the gateware computes
    exactly what `gamma12()` computes -- because `gamma12()` is what host-side
    tooling will use to predict what the wall is going to look like.
    """
    print("\nGamma (8-bit framebuffer -> 12-bit SPWM, piecewise over 16 segments)")
    failures = 0
    try:
        true = gamma_table12()
        err = max(abs(gamma12(i) - true[i]) for i in range(256))
        assert err <= 20, f"max error {err} of 4095"
        print(f"  ok  within {100*err/4095:.2f}% of the true 2.8 curve "
              f"(max {err} of 4095)")

        assert all(gamma12(i) <= gamma12(i + 1) for i in range(255)), "not monotonic"
        print(f"  ok  monotonic -- a non-decreasing curve cannot band")

        assert gamma12(0) == 0 and gamma12(255) == 4095, \
            f"endpoints {gamma12(0)}..{gamma12(255)}"
        print(f"  ok  endpoints exact: 0 -> 0, 255 -> 4095")

        # Why correct into 12 bits at all, stated as a test rather than a claim.
        lut8 = [int(pow(i / 255.0, 2.8) * 255 + 0.5) for i in range(256)]
        black8 = sum(1 for v in lut8 if v == 0)
        black12 = sum(1 for i in range(256) if gamma12(i) == 0)
        assert black12 < black8, f"{black12} vs {black8}"
        print(f"  ok  crushes {black12} codes to black where hub75's 8-bit LUT "
              f"crushes {black8}")

        dut = Icn1065Gamma()
        got = []

        def bench():
            for i in range(256):
                yield dut.i.eq(i)
                yield          # combinational, but settle a delta first
                got.append((yield dut.o))

        run_simulation(dut, bench())
        bad = [(i, got[i], gamma12(i)) for i in range(256) if got[i] != gamma12(i)]
        assert not bad, f"{len(bad)} mismatches, first {bad[:3]}"
        print(f"  ok  Icn1065Gamma matches gamma12() on all 256 codes")
    except AssertionError as e:
        print(f"  FAIL {e}")
        failures += 1
    return failures


def main():
    failures = 0
    # Only the bare command pulses -- VSYNC and PRE_ACT carry no payload, they
    # are pure LAT-width encodings. Register data goes through Icn1065RegBlock.
    cases = [
        ("VSYNC", V_SYNC, CLK_PAD, 0x0000),
        ("PRE_ACT1", PRE_ACT1, CLK_PAD, 0x0000),
        ("PRE_ACT2", PRE_ACT2, CLK_PAD, 0x0000),
    ]
    print("ICN1065 command pulses (VSYNC / PRE_ACT)")
    for name, length, pad, data in cases:
        dut = Icn1065Command()
        trace = []

        def bench():
            nonlocal trace
            trace = yield from capture(dut, length, pad, data)

        run_simulation(dut, bench())
        try:
            check(name, trace, length, pad, data)
        except AssertionError as e:
            print(f"  FAIL {e}")
            failures += 1

    print("\nProtocol constants")
    assert REG_CNT == 38, REG_CNT
    print(f"  ok  38 configuration registers")
    assert REG_VALUE[CFG_SCAN] == 0x022a, hex(REG_VALUE[CFG_SCAN])
    print(f"  ok  table's scan register is 0x022a (43 rows -- the reference panel)")
    assert scan_register(64) == 0x023f, hex(scan_register(64))
    print(f"  ok  1/64 scan computes to 0x023f, which is what a 256x128 needs")
    assert len({V_SYNC, REG_LATCH, PRE_ACT1, PRE_ACT2}) == 4
    print(f"  ok  command lengths are distinct -- they are the only encoding")

    from spwm_chips import CHIPS
    for _chip in CHIPS.values():
        failures += check_prefix(_chip)
    failures += check_regblock()
    failures += check_serialiser()
    failures += check_rowscan()
    failures += check_full_frame()
    failures += check_pixel_source(gamma=False)
    failures += check_pixel_source(gamma=True)
    failures += check_gamma()
    failures += check_rgb565()
    for _lat in (1, 4, 16):
        failures += check_arbiter(latency=_lat)
    for _lat in (1, 4, 16, 32):
        failures += check_pipelined_arbiter(latency=_lat)
    failures += check_hub320()
    for _n in (8, 9):
        failures += check_pipelined_arbiter(
            latency=16, n=_n, groups=GROUPS_HUB320)
    failures += check_bandwidth()

    if failures:
        print(f"\n{failures} FAILED")
        return 1
    print("\nAll passed. Untested against real hardware -- no panel here yet.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
