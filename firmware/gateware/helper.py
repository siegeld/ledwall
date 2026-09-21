#!/usr/bin/env python3
from migen import *
from litex.build.generic_platform import Subsignal, Pins, Misc, IOStandard


def hub75_conn(platform, n_outputs=8):
    connectors = platform.constraint_manager.connector_manager.connector_table
    hub75_extension = []
    for connector in range(n_outputs):
        name = "j" + str(connector + 1)
        # Results in 0..(n_outputs-1)
        number = connector
        pins = connectors[name]
        hub75_extension.append(
            (
                "hub75_data",
                number,
                Subsignal("r0", Pins(pins[0])),
                Subsignal("g0", Pins(pins[1])),
                Subsignal("b0", Pins(pins[2])),
                Subsignal("r1", Pins(pins[4])),
                Subsignal("g1", Pins(pins[5])),
                Subsignal("b1", Pins(pins[6])),
            )
        )

    # Now the common pins, they're the same across all
    pins = connectors["j1"]
    hub75_extension.append(
        (
            "hub75_common",
            0,
            Subsignal(
                "row",
                Pins(
                    pins[8]
                    + " "
                    + pins[9]
                    + " "
                    + pins[10]
                    + " "
                    + pins[11]
                    + " "
                    + pins[7]
                ),
            ),
            Subsignal("clk", Pins(pins[12])),
            Subsignal("lat", Pins(pins[13])),
            Subsignal("oe", Pins(pins[14])),
        ),
    )
    return hub75_extension
