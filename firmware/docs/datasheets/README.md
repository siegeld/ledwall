# Datasheets and vendor references

Kept in the repo because this panel's bring-up turned on details that are hard
to re-find, and two of them were only resolved by reading a PDF.

| file | what it settles |
|---|---|
| `DP3364S_Rev1.0_EN.pdf` | Same family as the **DP3264S** on this module, NOT the same part -- register counts differ, so treat it as a family reference. Section 10.1 gives the LE command widths (DATA_LATCH 1, VSYNC 3, WR_CFG 5, PRE_ACT 14), which match our gateware exactly. Section 10.2 gives the data order: within a chip, **channel 15 first, channel 0 last**. |
| `ledplex-20250718084930.pdf` | LEDPLEX P1.25 320x160 1/64-scan module spec. Names the connector **"HUB E320"** and lists Colorlight/Nova as matching cards -- the interface has a vendor name, which makes it searchable. No driver IC, no pixel mapping. |

## What vendor documents did NOT settle

The module's **pixel mapping is factory-specific and unpublished**. Colorlight's
LEDSetting exposes it as Data Group Swap, Smart Module Settings, cascade
direction and split style -- i.e. it is per-module configuration, not a
protocol you can derive.

Two hypotheses taken from other modules' configs were tested on glass and
**both failed**:

* `chan_rev=1` (datasheet channel order 15->0 applied alone) -> sawtooth, twice.
  It reverses within a chip but not chip order, which is half a transformation.
* Full 128-column reversal, from a NovaStar `.rcfgx` recording
  "Cascade direction: RightLeft" for a 3264/5958 module -> vertical sawtooth
  and the corner blocks left their corners. That file is for a **64x64**
  module; mapping does not transfer between modules.

The lesson is in `E320-BRINGUP.md` 4.8: measure the mapping on the glass with
1-pixel features (`stair`, `chanmap`), do not infer it from another module's
configuration.
