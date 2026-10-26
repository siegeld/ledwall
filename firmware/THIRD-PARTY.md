# Third-party components

This project is BSD-2-Clause ([LICENSE](LICENSE)). It also **redistributes**
the components below, which carry their own terms. Those terms travel with the
files; this page exists so nobody has to go looking for them.

## Vendored in this repository

| Component | Where | Licence |
|---|---|---|
| **smoltcp** 0.8.0 (patched) | `sw_rust/smoltcp-0.8.0/` | 0BSD — [`LICENSE-0BSD.txt`](sw_rust/smoltcp-0.8.0/LICENSE-0BSD.txt) |
| **DejaVu fonts** | `assets/fonts/*.ttf` | Bitstream Vera + public-domain changes — [`LICENSE-DejaVu.txt`](assets/fonts/LICENSE-DejaVu.txt) |

**Why smoltcp is vendored rather than depended on**: the fork exposes DHCP
option 66 as `Config.tftp_server_name`, which is how a panel learns its boot
server. Do not replace it with upstream.

**The DejaVu licence has a condition worth knowing**: modified versions must be
renamed so they contain neither "Bitstream" nor "Vera". The fonts here are
unmodified — `panelc` rasterises them at build time into coverage bitmaps and
does not alter the font files.

## Build-time only, not redistributed

These are pulled into the Docker toolchain image and are not part of this
repository:

| Component | Licence |
|---|---|
| LiteX, Migen, LiteEth, LiteDRAM, LiteSPI | BSD-2-Clause |
| yosys | ISC |
| nextpnr, prjtrellis | ISC |
| VexRiscv | MIT |
| openFPGALoader | Apache-2.0 |

`sw_rust/litex-pac/` is **generated** from the SoC's SVD by `svd2rust`; it
describes this design's own registers.
