#!/usr/bin/env python3
from migen import *
from litex.build.generic_platform import Subsignal, Pins, Misc, IOStandard


# Slot layout of a HUB320 connector, as measured on the Colorlight E320 and
# recorded in gateware/colorlight_e320.py. Slot == pin number - 1, so the
# ground pins at 4, 8, 12, 16, 22 and 26 are slots 3, 7, 11, 15, 21 and 24.
_HUB320_RGB_SLOTS = [0, 1, 2, 4, 5, 6, 8, 9, 10, 12, 13, 14]
_HUB320_ROW_SLOTS = [16, 17, 18, 19, 20]        # A B C D E
_HUB320_CLK, _HUB320_LAT, _HUB320_OE = 21, 22, 23


# Where a TWO-group (plain HUB75) panel's signals sit on each connector layout.
#
# A 5A-75E HUB75 connector is 16 slots; an E320 HUB320 connector is 25. The two
# RGB pairs happen to land on the SAME slots in both, which is why a 2-group
# build on the E320 gets most of the way before failing -- but the row and
# control block does not: HUB320 slot 7 is a GROUND where the 5A-75E has row E.
# That mismatch is the `hub75_common_row[4] constrained to pin '-'` nextpnr
# error. The underlying BALLS are identical on both boards (N5 N3 P3 P4 N4 for
# A..E, M3/N1/M4 for clk/lat/oe); only the slot indices move.
_HUB75_LAYOUTS = {
    16: dict(rgb=(0, 1, 2, 4, 5, 6), row=(8, 9, 10, 11, 7),
             clk=12, lat=13, oe=14),
    25: dict(rgb=(0, 1, 2, 4, 5, 6), row=tuple(_HUB320_ROW_SLOTS),
             clk=_HUB320_CLK, lat=_HUB320_LAT, oe=_HUB320_OE),
}


def _hub75_layout(pins, name):
    """Pick the slot map for a connector by its width."""
    try:
        return _HUB75_LAYOUTS[len(pins)]
    except KeyError:
        raise ValueError(
            f"connector {name} has {len(pins)} slots; expected 16 (HUB75) "
            f"or 25 (HUB320). See gateware/helper.py _HUB75_LAYOUTS."
        ) from None


def hub75_conn(platform, n_outputs=8):
    connectors = platform.constraint_manager.connector_manager.connector_table
    hub75_extension = []
    for connector in range(n_outputs):
        name = "j" + str(connector + 1)
        # Results in 0..(n_outputs-1)
        number = connector
        pins = connectors[name]
        lay = _hub75_layout(pins, name)
        labels = ("r0", "g0", "b0", "r1", "g1", "b1")
        hub75_extension.append(
            (
                "hub75_data",
                number,
                *[Subsignal(lab, Pins(pins[slot]))
                  for lab, slot in zip(labels, lay["rgb"])],
            )
        )

    # Now the common pins, they're the same across all
    pins = connectors["j1"]
    lay = _hub75_layout(pins, "j1")
    hub75_extension.append(
        (
            "hub75_common",
            0,
            Subsignal("row", Pins(" ".join(pins[s] for s in lay["row"]))),
            Subsignal("clk", Pins(pins[lay["clk"]])),
            Subsignal("lat", Pins(pins[lay["lat"]])),
            Subsignal("oe", Pins(pins[lay["oe"]])),
        ),
    )
    return hub75_extension


def hub320_conn(platform, n_outputs=8):
    """Request the HUB320 connectors: TWELVE data lines each, not six.

    The difference from `hub75_conn` is width, not protocol. A HUB75
    connector carries two RGB groups; HUB320 carries four, which is why the
    E320 fits the same 96 data balls into eight connectors where the 5A-75E
    needs sixteen. Everything downstream -- the serialiser, the arbiter, the
    framebuffer -- already handles four groups; only the pins were missing.

    The connector table must be the E320's (see `colorlight_e320.Platform`).
    Pointing this at a 5A-75E will raise, because its connectors only have
    sixteen slots and the row block lives somewhere else entirely -- which is
    the failure you want, rather than a build that places happily and drives
    the wrong balls.
    """
    connectors = platform.constraint_manager.connector_manager.connector_table
    names = [f"J{n + 1}" for n in range(n_outputs)]
    for name in names:
        if name not in connectors:
            raise KeyError(
                f"no connector {name!r} in this platform's table. "
                "hub320_conn() needs the E320 connector table -- use "
                "gateware/colorlight_e320.py, not colorlight_5a_75e.")
        if len(connectors[name]) < 25:
            raise ValueError(
                f"connector {name} has {len(connectors[name])} slots; a "
                "HUB320 connector has 25 (26 pins, last is ground). This "
                "looks like a HUB75 table.")

    extension = []
    labels = ["r0", "g0", "b0", "r1", "g1", "b1",
              "r2", "g2", "b2", "r3", "g3", "b3"]
    for number, name in enumerate(names):
        pins = connectors[name]
        extension.append((
            "hub75_data", number,
            *[Subsignal(lab, Pins(pins[slot]))
              for lab, slot in zip(labels, _HUB320_RGB_SLOTS)],
        ))

    # The common block is identical on every connector -- the same balls are
    # wired to all eight -- so it is requested once, from J1, exactly as the
    # HUB75 path does.
    pins = connectors[names[0]]
    extension.append((
        "hub75_common", 0,
        Subsignal("row", Pins(" ".join(pins[s] for s in _HUB320_ROW_SLOTS))),
        Subsignal("clk", Pins(pins[_HUB320_CLK])),
        Subsignal("lat", Pins(pins[_HUB320_LAT])),
        Subsignal("oe", Pins(pins[_HUB320_OE])),
    ))
    return extension
