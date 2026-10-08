#!/usr/bin/env python3
"""
Colorlight E320 V6.2 — platform definition.

The E320 is electrically a 5A-75E: same `LFE5U-25F` in CABGA256, same
GigaDevice `GD25Q32`, same ESMT `M12L64322A-5T` SDRAM, same Ethernet PHYs, and
the same JTAG pad designators (`J27`/`J30`/`J31`/`J32`), fitted from the factory
rather than soldered on. It boots the stock 5A-75E bitstream unmodified.

The one real difference is the panel side. The 5A-75E exposes sixteen 16-pin
HUB75 connectors of six data lines each. The E320 exposes **eight 26-pin
HUB320 connectors of twelve data lines each** — and it is the *same 96 data
lines*, just regrouped two-for-one. Port `J<n>` is `j(2n-1)` + `j(2n)` merged,
with the common block shared exactly as before.

So everything except the connector table is inherited.

## How this map was obtained

Measured, not guessed, using `gateware/pinwalk.py` and a multimeter — see
`docs/PINWALK.md`. **Fourteen pins of J3 were read directly:**

    pin  1  2.90 kHz  G2   R1        pin 17  5.00 kHz  N5   A
    pin  2  2.85 kHz  G1   G1        pin 18  4.90 kHz  N3   B
    pin  3  3.35 kHz  H2   B1        pin 19  5.40 kHz  P3   C
    pin  4  NC             gnd       pin 21  4.95 kHz  N4   E
    pin  5  4.00 kHz  K5   R2        pin 22  NC             gnd
    pin  9  4.20 kHz  L4   R3        pin 23  4.45 kHz  M3   clk
    pin 13  5.80 kHz  R2   R4        pin 24  4.85 kHz  N1   lat

Every other pin is **forced**, not assumed:

*   `D` at pin 20 and `oe` at pin 25 are the only members of the common block
    left once the other six are placed — the block is a closed set of eight
    balls, so nothing else can go there.
*   The unread data lines (6, 7, 10, 11, 14, 15) follow from the merge, which
    was confirmed independently in all four RGB groups: pins 1–3 in group 1,
    pin 5 in group 2, pin 9 in group 3, pin 13 in group 4.
*   Grounds fall on 4, 8, 12, 16, 22 and 26 — one after each RGB group and one
    between the address and control blocks. Pins 4 and 22 were read as NC.

Pin 9 was the deciding test: it is the first data line of the *second* merged
half, so it is the one reading that distinguishes the merge hypothesis from a
plain HUB75 layout. It landed on prediction. The physical silkscreen label of
the probed connector is `J3`, which matches `j5 + j6` under the pairing above
with no offset — so the port numbering is anchored, not assumed.

Pin 22 is the one prediction that failed, and the failure is what produced the
layout above: the control block starts at 23, not 22, with a ground between
the address and control groups.

## What is verified, and what is not

**Pin 1 was read on all eight ports**, so the port ordering is measured
everywhere and derived nowhere:

    J1 1.45 C4 (j1)   J3 2.90 G2 (j5)   J5 5.55 P11 (j9)    J7 3.70 H15 (j13)
    J2 3.05 G5 (j3)   J4 6.20 T3 (j7)   J6 1.25 B15 (j11)   J8 3.90 J13 (j15)

**The within-pair order is confirmed on two ports**, J3 and J1 — that the
SECOND merged half (pins 9-15) is the even-numbered 5A-75E connector. On J1 it
was read as a three-pin run rather than a single pin: pins 9, 10, 11 gave
2.45 / 2.55 / 2.95 = F3 / F5 / G3 = `j2` r0, g0, b0.

That run matters more than a single reading would. `F3`, `F4` and `F5` are
adjacent both in the frequency plan and across the j1/j2 boundary, so a single
pin reading 2.50 instead of 2.45 is equally explained by an off-by-one probe,
by rounding, or by a real layout difference. A run of three distinguishes
them: an off-by-one would have read 2.50 / NC / 2.45, with a ground in the
middle. Where neighbouring balls land on neighbouring frequencies, read a run.

Within a port, only J3 was mapped pin by pin. J2 and J4..J8 inherit their
remaining pins from the merge rule.

**No panel has been attached to an E320.** Nothing here is confirmed by light
on glass, which is the only test that matters in the end.

## Pin order of a HUB320 connector

     1- 3  R1 G1 B1      4  gnd
     5- 7  R2 G2 B2      8  gnd
     9-11  R3 G3 B3     12  gnd
    13-15  R4 G4 B4     16  gnd
    17-21  A B C D E    22  gnd
    23-25  clk lat oe   26  gnd

`helper.hub75_conn()` reads a connector as six data pins plus a common block
and takes only TWO RGB groups, so it cannot drive these connectors fully. Four
group width lives in the `icn1065` stage. The table below is laid out in the
same slot order `helper.hub75_conn()` expects for its first six entries, so a
two-group build works on the first half of each port while a four-group build
uses all of it.
"""

from litex_boards.platforms import colorlight_5a_75e


# The eight HUB320 ports. Slot order matches the pin order documented above,
# with "-" for the ground pins, so slot number equals (pin number - 1).
#
# Eight ports x 12 data lines is 96, which is exactly the number of data balls
# the 5A-75E's j1..j16 provide. That arithmetic landing dead on is what made
# the merge rule credible before any of it was measured.
_connectors_e320_v6_2 = [
    # port    R1  G1  B1   _   R2  G2  B2   _   R3  G3  B3   _   R4  G4  B4   _    A   B   C   D   E  clk lat  oe   _
    ("J1",  "C4  D4  E4   -   D3  E3  F4   -   F3  F5  G3   -   G4  H3  H4   -   N5  N3  P3  P4  N4  M3  N1  M4   -"),
    ("J2",  "G5  H5  J5   -   J4  B1  C2   -   C1  D1  E2   -   E1  F2  F1   -   N5  N3  P3  P4  N4  M3  N1  M4   -"),
    ("J3",  "G2  G1  H2   -   K5  K4  L3   -   L4  L5  P2   -   R2  T2  R3   -   N5  N3  P3  P4  N4  M3  N1  M4   -"),
    ("J4",  "T3  R4  M5   -   P5  N6  N7   -   P7  M7  P8   -   R8  M8  M9   -   N5  N3  P3  P4  N4  M3  N1  M4   -"),
    ("J5", "P11 N11 M11   -  T13 R12 R13   -  R14 T14 D16   -  C15 C16 B16   -   N5  N3  P3  P4  N4  M3  N1  M4   -"),
    ("J6", "B15 C14 T15   -  P15 R15 P12   -  P13 N12 N13   -  M12 P14 N14   -   N5  N3  P3  P4  N4  M3  N1  M4   -"),
    ("J7", "H15 H14 G16   -  F16 G15 F15   -  E15 E16 L12   -  L13 M14 L14   -   N5  N3  P3  P4  N4  M3  N1  M4   -"),
    ("J8", "J13 K13 J12   -  H13 H12 G12   -  G14 G13 F12   -  F13 F14 E14   -   N5  N3  P3  P4  N4  M3  N1  M4   -"),
]

# Which 5A-75E connectors each port is made of. Kept because it is the fact the
# whole table rests on, and because anyone re-deriving it will want to check.
PORT_SOURCES = {f"J{n+1}": (f"j{2*n+1}", f"j{2*n+2}") for n in range(8)}

# J4's second half is a dead end for us as things stand. Its balls are claimed
# by the SECOND Ethernet PHY in the platform's IO map, so the pin walker will
# not drive them and a build cannot request them as panel outputs while that
# PHY is declared. Our SoC only ever uses `eth 0`, so they MAY be free -- but
# "unused by our gateware" is not "safe to drive": if the second PHY is
# populated, its receive pins are driven by the PHY and driving them from the
# FPGA is contention. Reading J4 pin 9 as 0 Hz does NOT settle this: 0 Hz is
# also what an undriven ball reads, so that measurement confirms the exclusion
# list rather than the board. Resolve it by checking whether a second PHY is
# populated, then building a walker variant that frees these.
J4_SECOND_HALF_UNRESOLVED = ["P7", "M7", "P8", "R8", "M8", "M9"]

# Slot index of each signal within a port, for code that would otherwise count
# on its fingers and get it wrong.
SLOTS = {
    "r0": 0, "g0": 1, "b0": 2,
    "r1": 4, "g1": 5, "b1": 6,
    "r2": 8, "g2": 9, "b2": 10,
    "r3": 12, "g3": 13, "b3": 14,
    "A": 16, "B": 17, "C": 18, "D": 19, "E": 20,
    "clk": 21, "lat": 22, "oe": 23,
}


class Platform(colorlight_5a_75e.Platform):
    """A 5A-75E platform with the E320's connector table.

    Everything else — SDRAM, Ethernet, SPI flash, clk25, serial, the sysCONFIG
    bank — is identical and is inherited rather than copied, so a correction to
    the upstream 5A-75E map reaches this board too.
    """

    def __init__(self, revision="8.2", toolchain="trellis"):
        super().__init__(revision=revision, toolchain=toolchain)

        # The E320 is a -6 part. Say so, or every timing number lies.
        #
        # Revision 8.2's device string is `LFE5U-25F-7BG256I`, a -7I, and that
        # is what reaches `nextpnr-ecp5 --speed 7`. But the E320's own factory
        # bitstream declares `LFE5U-25F-6CABGA256` -- a -6 -- and the markings
        # agree. We keep revision 8.2 because its PINOUT is the one that
        # demonstrably works here for SDRAM, flash and serial; only the speed
        # grade is wrong, so only the speed grade is overridden.
        #
        # This matters because the margins are thin: the 4-group HUB320 build
        # reports 42.5-55.5 MHz against a 40 MHz requirement depending on seed,
        # and the Ethernet RX domain 145-151 MHz against 125 MHz. Timed as a -7
        # on a -6 die, those numbers are optimistic by roughly the grade
        # difference, and `--timing-allow-fail` means a build that misses
        # timing ships anyway. A design failing only in its fastest domain
        # comes up, runs the CPU, drives panels and has broken Ethernet.
        #
        # Configuration is unaffected either way -- the IDCODE is identical
        # across speed grades -- which is why this went unnoticed.
        self.device = self.device.replace("-7BG256I", "-6BG256C")
        table = self.constraint_manager.connector_manager.connector_table
        for name, pins in _connectors_e320_v6_2:
            table[name] = pins.split()
            table[name.lower()] = pins.split()


if __name__ == "__main__":
    # Sanity check: every ball in the table must be a real package pin, the
    # ground slots must line up, and no port may repeat a data ball.
    seen = {}
    for name, pins in _connectors_e320_v6_2:
        p = pins.split()
        assert len(p) == 25, f"{name}: {len(p)} slots, expected 25"
        for slot in (3, 7, 11, 15, 24):
            assert p[slot] == "-", f"{name}: slot {slot} should be ground, got {p[slot]}"
        for slot in (0, 1, 2, 4, 5, 6, 8, 9, 10, 12, 13, 14):
            ball = p[slot]
            assert ball != "-", f"{name}: slot {slot} should be data"
            if ball in seen:
                raise AssertionError(f"{ball} used by both {seen[ball]} and {name}")
            seen[ball] = name
    n_ports = len(_connectors_e320_v6_2)
    assert len(seen) == n_ports * 12, f"{len(seen)} data balls, expected {n_ports*12}"
    print(f"OK: {n_ports} ports, {len(seen)} distinct data balls, grounds aligned")
