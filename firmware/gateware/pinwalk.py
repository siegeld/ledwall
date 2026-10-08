#!/usr/bin/env python3
"""
pinwalk -- identify which FPGA ball drives which connector pin, with a multimeter.

WHY THIS EXISTS
---------------
A receiver card's panel connectors are wired to FPGA balls that no datasheet
tells you. The 5A-75E's map was reverse engineered by the chubby75 project; the
E320's HUB320 map is not published anywhere, and it cannot be recovered from a
factory bitstream -- the IOLOGIC trick that found its SDRAM, flash and Ethernet
pins only sees *registered* IO, and panel data lines are combinational.

So the mapping has to be measured. Walking one pin at a time and probing after
each step needs a host in the loop and ~115 round trips. This does it in one
pass instead: every candidate ball is driven with a square wave at its OWN
frequency, so touching a connector pin with a meter in Hz mode reads the ball's
identity directly off the display. No CPU, no firmware, no network, no host --
just a bitstream and a meter.

    frequency -> index:   i = (f_hz - BASE_HZ) / STEP_HZ
    index -> ball:        pinwalk-map.csv, written beside the bitstream

READ THIS BEFORE USING IT
-------------------------
*   **Nothing that the board itself drives is walked.** Driving an output
    against an output is how boards die. The reserved set below is taken from
    the 5A-75E platform map (SDRAM, Ethernet, SPI flash, clk25, serial) plus
    the whole of bank 8, which carries the sysCONFIG and JTAG pins. Everything
    else on the package is fair game, because on these cards the remaining
    balls feed 74HC245 buffer *inputs*.
*   **It cannot tell you what a pin MEANS**, only which ball it is. Knowing
    that connector pin 7 is ball `N4` is the hard half; deciding it is the row
    address line is the easy half, and comes from the panel's HUB pinout.
*   The frequencies are exact, not approximate -- each is an integer divide of
    the 25 MHz input -- so a reading that is not within a few Hz of a table
    entry means you are probing a floating pin, a ground, or a power rail.

USAGE

    ./build.sh pinwalk              # build, then flash it to the card
    # probe each connector pin with a meter in Hz mode, read off the table

To put the card back afterwards, reflash the normal bitstream; nothing here
touches flash beyond the bitstream region.
"""

import argparse
import csv
import json
import os
import sys

from migen import *

from litex.build.generic_platform import IOStandard, Pins, Subsignal
from litex_boards.platforms import colorlight_5a_75e


# Frequency plan ------------------------------------------------------------
#
# A cheap multimeter resolves a few Hz on a clean square wave well below
# 20 kHz, so 1 kHz .. ~7 kHz in 50 Hz steps covers 115 balls with margin on
# both ends. Keep STEP_HZ comfortably larger than your meter's error: at 50 Hz
# apart, a reading has to be 25 Hz wrong before it names the wrong ball.
BASE_HZ = 1000
STEP_HZ = 50

# The input clock. clk25 is a real 25 MHz oscillator on these boards and is
# confirmed working on any card that has booted the normal gateware, so there
# is no reason to involve a PLL here -- fewer moving parts in a diagnostic.
CLK_HZ = 25_000_000


def _trellis_iodb():
    """Package pin -> die location, from the prjtrellis database."""
    candidates = [
        os.environ.get("TRELLIS_DB", ""),
        "/opt/oss-cad-suite/share/trellis/database",
        "/usr/local/share/trellis/database",
        "/usr/share/trellis/database",
    ]
    for base in candidates:
        if not base:
            continue
        path = os.path.join(base, "ECP5", "LFE5U-25F", "iodb.json")
        if os.path.exists(path):
            with open(path) as handle:
                return json.load(handle)
    raise SystemExit(
        "pinwalk: cannot find the prjtrellis iodb.json. Set TRELLIS_DB to the "
        "database directory (it holds ECP5/LFE5U-25F/iodb.json)."
    )


def reserved_pins(platform):
    """Every ball the BOARD drives, or that configuration owns. Never walked.

    Two sources, because neither alone is complete:

    1. the platform's own IO map -- SDRAM, Ethernet, SPI flash, clk25, serial.
       These are either driven by something on the board (the PHY's receive
       pins, the flash's MISO, the oscillator) or shared with it (the SDRAM
       bus), and in both cases driving them risks contention.
    2. bank 8 in its entirety -- sysCONFIG and JTAG. Driving these does not
       merely risk the board, it risks the only recovery path we have.
    """
    reserved = set()

    for resource in platform.constraint_manager.available:
        name, number, pins_obj = resource[0], resource[1], resource[2]
        def collect(obj):
            if isinstance(obj, Pins):
                reserved.update(obj.identifiers)
            elif isinstance(obj, Subsignal):
                for item in obj.constraints:
                    collect(item)
            elif isinstance(obj, (list, tuple)):
                for item in obj:
                    collect(item)
        collect(pins_obj)
        if len(resource) > 3:
            for extra in resource[3:]:
                collect(extra)

    return reserved


def candidate_pins(platform):
    """Bonded CABGA256 balls that are safe to drive, in a stable order."""
    iodb = _trellis_iodb()
    package = iodb["packages"]["CABGA256"]
    bank_of = {
        (m["row"], m["col"], m["pio"]): m.get("bank")
        for m in iodb["pio_metadata"]
    }

    reserved = reserved_pins(platform)
    out = []
    for ball, loc in package.items():
        bank = bank_of.get((loc["row"], loc["col"], loc["pio"]))
        if bank == 8:
            continue              # sysCONFIG and JTAG -- never
        if ball in reserved:
            continue              # the board drives it, or shares it
        out.append(ball)

    # Sort by (letter-run, number) so the table reads like a BGA map rather
    # than like dictionary order -- it makes probing far less error-prone.
    def key(ball):
        letters = "".join(c for c in ball if c.isalpha())
        digits = "".join(c for c in ball if c.isdigit())
        return (len(letters), letters, int(digits or 0))

    return sorted(out, key=key)


class PinWalk(Module):
    """One free-running toggle per ball, each at its own frequency.

    A divider per pin rather than one shared counter: the point is that every
    ball carries a DIFFERENT frequency simultaneously, so a single probe touch
    identifies a pin without any host interaction. Each divider is ~14 bits
    plus a comparator, so 115 of them is a rounding error on an LFE5U-25.
    """

    def __init__(self, pads, freqs):
        assert len(pads) == len(freqs)
        for pad, freq_hz in zip(pads, freqs):
            # Toggle every half period; the resulting square wave is at freq_hz.
            half = max(1, CLK_HZ // (2 * freq_hz))
            width = max(1, (half - 1).bit_length())
            counter = Signal(width)
            self.sync += [
                If(
                    counter == (half - 1),
                    counter.eq(0),
                    pad.eq(~pad),
                ).Else(
                    counter.eq(counter + 1),
                )
            ]


class PinWalkSoC(Module):
    def __init__(self, platform, balls):
        self.clock_domains.cd_sys = ClockDomain()
        self.comb += self.cd_sys.clk.eq(platform.request("clk25"))

        extension = [
            ("walk", i, Pins(ball), IOStandard("LVCMOS33"))
            for i, ball in enumerate(balls)
        ]
        platform.add_extension(extension)
        pads = [platform.request("walk", i) for i in range(len(balls))]

        freqs = [BASE_HZ + STEP_HZ * i for i in range(len(balls))]
        self.submodules.walk = PinWalk(pads, freqs)


def write_map(balls, path):
    """The lookup table. Without this the bitstream is useless."""
    with open(path, "w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["index", "ball", "frequency_hz"])
        for i, ball in enumerate(balls):
            writer.writerow([i, ball, BASE_HZ + STEP_HZ * i])
    return path


def main():
    parser = argparse.ArgumentParser(
        description="Build the pin-walker bitstream and its frequency map."
    )
    parser.add_argument("--revision", default="8.2", help="board revision")
    parser.add_argument("--build", action="store_true", help="run the build")
    parser.add_argument(
        "--output-dir", default="build/pinwalk", help="build directory"
    )
    parser.add_argument(
        "--list", action="store_true",
        help="print the ball/frequency table and exit without building",
    )
    args = parser.parse_args()

    platform = colorlight_5a_75e.Platform(revision=args.revision)
    balls = candidate_pins(platform)

    if not balls:
        raise SystemExit("pinwalk: no candidate balls -- check the reserved set")

    top_hz = BASE_HZ + STEP_HZ * (len(balls) - 1)
    print(f"pinwalk: {len(balls)} candidate balls, "
          f"{BASE_HZ} Hz .. {top_hz} Hz in {STEP_HZ} Hz steps")

    if args.list:
        for i, ball in enumerate(balls):
            print(f"  {i:4d}  {ball:5s}  {BASE_HZ + STEP_HZ * i:6d} Hz")
        return

    os.makedirs(args.output_dir, exist_ok=True)
    map_path = write_map(balls, os.path.join(args.output_dir, "pinwalk-map.csv"))
    print(f"pinwalk: wrote {map_path}")

    soc = PinWalkSoC(platform, balls)
    platform.build(soc, build_dir=args.output_dir, run=args.build)


if __name__ == "__main__":
    main()
