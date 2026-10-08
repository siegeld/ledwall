# The bitstream's version

`gateware/VERSION` is the FPGA bitstream's own semver, and it is **deliberately
separate from the firmware's**.

The two artifacts change at very different rates. The Rust firmware is released
constantly; the gateware changes rarely. Tying the bitstream's version to the repo
version — which is what 2.17.0 and 2.18.0 did — meant every firmware release
appeared to ship new FPGA logic, which is noise at best and a lie at worst: it
tells an operator the gateware changed when nothing in it did.

So a card reports two independent versions:

    bitstream  e320-v1.0.0        <- this file, plus the board
    firmware   2.18.0             <- sw_rust/barsign_disp/Cargo.toml

## When to bump it

When a change alters the bitstream: anything under `gateware/`, a different
`--panel`, `--outputs`, `--sys-clk`, or a LiteX/nextpnr version that changes the
generated logic. Not for firmware-only changes, and not for documentation.

- **patch** — a rebuild with no functional change (toolchain bump, seed change)
- **minor** — new gateware capability, same pinout and interfaces
- **major** — anything that makes an existing firmware incompatible: a CSR map
  change, a pinout change, a different panel interface

## What is in the name, and why

    e320-128x64-out6-v1.0.0

Everything before the version is **compiled into the bitstream** and changes what
the gateware does, so leaving any of it out would let two incompatible images
share one name:

| | why it is in the name |
|---|---|
| board | the pinout and connector map |
| panel | the shift register width and row count — baked in, which is why `build.sh` keys its bitstream cache by panel and says "firmware is universal, only bitstreams differ" |
| outputs | how many connectors the gateware drives |

**A corrected mistake, recorded so it is not repeated.** The first version of this
file named only `<board>-v<version>`, arguing that panel geometry was "a build
variant of the same gateware". It is not. Two bitstreams built `--panel 128x64`
and `--panel 256x64` are different logic and are not interchangeable, and under
that scheme they were both `e320-v1.0.0`. A name that cannot distinguish two
incompatible images is not an identity.

The **system clock** is a field rather than part of the name: in practice it
follows the board (the E320 is a `-6` part built at 35 MHz, the 5A-75E at 40), and
`build.sh` refuses a firmware whose clock constant disagrees with the gateware's,
so a mismatch cannot reach a card silently. If you build the same board, panel and
output count at a different clock, **bump the version** — otherwise two different
bitstreams share a name again.

The same applies to anything else compile-time that is not in the name, chain
length included: if you change it, bump the version. The version is the backstop
for everything the name does not spell out.
