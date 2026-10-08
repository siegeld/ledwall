# Flashing a card — the only procedure

**Single authority for getting a bitstream or firmware onto a Colorlight card.**
Every other doc that described this now points here. Flashing instructions found
anywhere else are stale: fix them to link here rather than restating them.

"Programming" is overloaded in this repo:

| You mean | Go to |
|---|---|
| Bitstream/firmware onto the card over JTAG | **this file** |
| Writing a display program (the scene language) | `docs/PROGRAMMING.md` |

Everything below was measured on 2026-10-04 on the E320 bench card, re-checked
against both tools. Where a claim is **not** verified it says so.

---

## 0. Four rules

1. **Go through `./build.sh`.** Never hand-type an `openFPGALoader` or `ecpprog`
   line. It takes tools from the tree (not `PATH`), uses cable `ft2232_b`, and
   checks freshness. `./build.sh --help` lists targets.
2. **A card that will not configure → `./build.sh recover` FIRST** (§1).
3. **`openFPGALoader --dump-flash` is not a measurement.** Never diagnose from
   it (§2).
4. **Never compare flash against a `.bit` file byte-for-byte** (§3).

## 0b. Can the card even be programmed right now? Check first.

```bash
./tools/jtag_reliability.sh 6
```

**Programming reliability is not constant on this bench.** Measured across
2026-10-04 on the E320: loads succeeded first-try early in the session, and
after roughly thirty attempts reached **0 of 6 and stayed there**. Six seconds
of measurement up front is cheaper than attributing the failure to your
bitstream, your cable or your tool -- which is what the whole of §6 and §8
below is a record of.

Everything in this list was tried against that 0-of-6 state and changed
nothing. **Do not re-test them:**

| Tried | Result |
|---|---|
| Both tools (`openFPGALoader`, `ecpprog -S`) | identical `Abort ERR` / `CRC ERR` |
| All three cable definitions | no change |
| `--freq` 6 MHz / 3 MHz / 1 MHz / 400 kHz / 100 kHz | no change |
| The nightly `openFPGALoader` | no change |
| Flash erased at offset 0 **and** the descriptor | no change |
| Flash restored to a bootable factory state | no change |
| `ecpprog -t` (reset) before each load | no change |
| `ecpprog -t -a` (REFRESH) before each load | no change |
| Orphaned processes cleared, ModemManager stopped+disabled | no change |
| Bitstreams of 132 KB, 428 KB, 694 KB | no change |

The symptom is a latched state: the status register sits with
`ISC Enable`/`Write Enable`/`Read Enable` set and `Abort` + `EXEC` errors, and
no software-issued reset or refresh clears it. Per Lattice's sysCONFIG note
(FPGA-TN-02039) the only recoveries are **PROGRAMN** or **REFRESH** -- and
PROGRAMN is **not wired to this card's JTAG header**, which carries only
`J27` TCK, `J30` TDO, `J31` TMS, `J32` TDI, `J33` 3V3, `J34` GND.

> **So the real fix is to stop needing JTAG.** Bringing PROGRAMN out to the
> header would give a software-triggerable reset and is worth one wire. But the
> durable answer is OTA: once a card holds a good bitstream and firmware,
> marquee updates it over the network and JTAG never enters the loop again.
> That is what RB-5554 is for, and why "power-cycle it" is a bench recovery
> rather than an operating procedure.

## 0c. The capability DEGRADES over a session, in both directions

The single most important operational fact, and it took a whole day to see
because every individual failure looks like a different bug.

Measured on the E320 across 2026-10-04, same cable, same tools, same card:

| | early in the session | late in the session |
|---|---|---|
| JTAG SRAM configuration | succeeded first try, repeatedly | **0 of 30+** |
| Flash write truncates after | ~10,072 bytes | **836 bytes** |
| Short transfers (IDCODE, flash ID, 4 KB verify) | perfect | **still perfect** |

Both sustained-transfer paths got monotonically worse while every short
transfer stayed byte-exact. Nothing in the tooling or the images changed
between those columns.

**Why this matters more than any individual symptom:** each failure along the
way is independently plausible as something else. A truncated flash write looks
like block protection. A failed configuration looks like a bad bitstream, then a
bad cable, then a wrong speed grade. A silent card looks like a broken MAC. We
chased all of those. The thing that actually distinguishes them is the
*trend* -- so **measure before you theorise**:

```bash
./tools/jtag_reliability.sh 6     # can it be configured at all, right now?
```

and if a flash write matters, verify the first kilobyte landed rather than
trusting "Writing 100% / Done" -- which is printed in both the working and the
truncated case.

The mechanism is not established. What is established: a power cycle restored
it once, and no software-issued reset (`ecpprog -t`, `ecpprog -t -a` REFRESH)
ever did. Per Lattice FPGA-TN-02039 the only recoveries are PROGRAMN or
REFRESH, and **PROGRAMN is not wired to this card's JTAG header.**

## 0d. What the vendor doc says, and why only a power cycle works here

Lattice's sysCONFIG note (FPGA-TN-02039, and TN1260) is explicit: when `INITN`
is asserted by an **error condition**, the error is cleared only by forcing the
device into the **initialization state**, and that state is entered by exactly
three things:

1. power being applied,
2. a **PROGRAMN** falling edge,
3. an **IEEE 1532 REFRESH** over a slave port (JTAG or SSPI).

Against that, measured on the E320:

| Path | Result |
|---|---|
| REFRESH | available and WORKS -- `openFPGALoader --reset` reports `Refresh: DONE` -- and configuration still fails afterwards |
| PROGRAMN | **not wired to this card's JTAG header**, which carries only `J27` TCK, `J30` TDO, `J31` TMS, `J32` TDI, `J33` 3V3, `J34` GND |
| Power | the only remaining path, and the only thing that has ever recovered it |

So the dependence on a power cycle is not a mystery and not a workaround: it is
the **only one of the three recovery mechanisms this board exposes.**

**PROGRAMN is not reachable.** Confirmed against the hardware 2026-10-04: the
board carries only a 4-pin header (TCK/TDO/TMS/TDI) and a 2-pin header
(3V3/GND), which matches chubby75's documentation. `J28` is the **KEY+ button on
FPGA pin `R7`**, not a config pin. PROGRAMN is a dedicated ECP5 ball with an
internal weak pull-up and is not broken out at all -- reaching it means
soldering to a BGA-adjacent via, not adding a jumper. An earlier version of this
section suggested "one wire to the header" would fix it; **that was wrong.**

Which inverts the conclusion usefully: the power cycle cannot be eliminated, so
**the need for JTAG has to be.** Provision a card once -- bitstream into flash,
firmware over netboot -- and from then on marquee updates it over the network
with no programmer and no power cycling in the loop. That is what RB-5554 is
for, and it is the answer to "we cannot rely on a power cycle": correct, so do
not rely on it operationally; rely on it exactly once, per card, at
provisioning time.

### Excluded, with evidence

| Hypothesis | Why it is out |
|---|---|
| Supply capacity | the bench supply is oversized ~50x |
| Current droop / signal integrity from load | **100 kHz behaves identically to 6 MHz**, at roughly 1/60th the dynamic switching current. Droop would have improved dramatically |
| Thermal, or anything that relaxes with time | controlled test: 0 of 3, then **10 minutes** of zero JTAG activity, then **0 of 6**. No recovery |
| FT2232H drive strength | channel B (the JTAG channel) already reads **12 mA with Schmitt input on**, not the 4 mA default that the documented TCK-integrity case was fixed from |
| Software | every cause in §0b, each tested |

### Correction: the status register is LATCHED, not live

Do not read live state from it. After erasing the multiboot descriptor that was
causing a failing master-SPI boot, the status still read `0x06c00000` with
`SPIm Fail1` set. Earlier reasoning in this file inferred live meaning from
those bits -- including §1's `SPIm Fail1` argument -- and that inference is
unsound on its own. Treat a status read as "something went wrong at some point
since the last initialization", not as "this is wrong now".

## 0d. The degradation points at a RAIL, and here is how to measure it

§0c records that the programming capability decays over a session and that a
power cycle restores it. That pattern is not a software signature -- every
software cause was tested and excluded (§0b) -- and no software state behaves
that way. It is what a **marginal supply that sags under sustained current**
looks like:

| Observation | Fits a sagging rail |
|---|---|
| IDCODE / flash ID / 4 KB verify always perfect | negligible current |
| Sustained transfers corrupt, worsening over hours | load plus self-heating |
| Clock rate irrelevant (100 kHz = 6 MHz) | droop, not setup/hold |
| Both directions affected (config AND flash writes) | the rail, not a signal |
| Only a power cycle recovers it | regulator fold-back / cooling |

**A supply can be amply rated and still fail at the card.** A high-resistance
connection -- the `J33` 3.3 V wire, a connector, a solder joint -- droops
locally under load no matter how big the supply is.

This is already a known signature here: §7's voltage-switch table, row 3, is
exactly "`--detect` passes with the right IDCODE, every bulk transfer dies with
`CRC ERR`, lowering `--freq` does not help" -- and that row is about the
Tigard's level shifters losing their **VTGT** supply, which is fed from `J33`.
Having the switch on `VTGT` is necessary but not sufficient; the wire has to
deliver 3.3 V *under load*.

**Measure these three with a meter, idle and during a configuration attempt:**

1. **3.3 V at `J33` on the card.** More than ~100 mV of droop implicates the feed.
2. **VTGT at the Tigard.** This powers the shifters; sag here corrupts bulk
   transfers precisely as observed.
3. **The card's own 3.3 V away from `J33`**, to separate "the card's rail sags"
   from "the wire to the Tigard sags".

A droop on any of them explains the whole picture at once: truncating flash
writes, 0-of-30 configurations, and why it got worse as the session went on.

> **Method note.** This conclusion comes from the TREND, not from any single
> failure. Each individual symptom was independently plausible as something
> else and several were chased hard -- block protection, a bad bitstream, the
> wrong speed grade, a PHY reset, the wrong RJ45 jack. What distinguishes a
> rail problem is that the capability *decays with use and returns with a power
> cycle*. Record the trend before theorising about any one failure.

## 1. CORRECTED: give the ECP5 something VALID to boot, do not erase

> **This section said the opposite until 2026-10-07 and the inversion cost a
> session.** It claimed a bad image in flash blocks JTAG configuration and that
> the fix was to erase flash. Erasing makes it WORSE.

What is actually true: a **failed** master-SPI boot leaves the configuration
engine in an error state, and that state blocks JTAG configuration. An empty
flash fails to boot just as surely as a corrupt one -- the ECP5 finds no
preamble, raises `BSE=4` (Preamble) and sits in error. So erasing flash
guarantees the failure it was supposed to cure.

Measured on the E320:

| Flash state | Pre-load `BSE` | JTAG configuration |
|---|---|---|
| valid factory image + descriptor | 0 | worked, often first try |
| erased offset 0 + erased descriptor | 4 (Preamble) | **0 of 30+** |
| descriptor -> intact factory copy at `0x2B0000` | **0** | works within a few attempts |

**The recipe that opens the window:**

```bash
# point the descriptor at a bitstream that definitely loads, then REFRESH so the
# ECP5 boots it cleanly, then load. Repeat the refresh+load pair a few times.
openFPGALoader --cable ft2232_b --reset        # REFRESH -> clean boot -> BSE 0
openFPGALoader --cable ft2232_b <your.bit>     # retry; succeeded on rounds 4 and 6
```

A power cycle alone does **not** do it: straight after one, with flash erased,
configuration was still 0 of 2 with a fresh `BSE=4`. What recovers the device is
having something valid for it to boot, not removing power.

> **Keep a known-good image in flash at all times.** `factory/` holds verified
> captures, and a third copy of the factory primary lives at `0x2B0000` -- which
> is what rescued this card after the erases. Treat that copy as precious.



> A corrupt or partially-written bitstream in flash stops the FPGA being
> configured **over JTAG**. The ECP5 keeps retrying a master-SPI boot from the
> bad image, that attempt fails (status bit **`SPIm Fail1`**), and it **aborts
> the JTAG configuration you are performing.**

Every symptom points elsewhere: a dead-looking board, bitstreams reporting
`Abort ERR`/`CRC ERR`, flash reads returning noise, a mute card.

```bash
./build.sh recover      # erase the bitstream region, then configure over JTAG
```

Evidence: SRAM configuration failed **every** time — three different bitstreams
(including a stock one known to have run on this card), both tools, every clock
from 200 kHz to 6 MHz — always `SPIm Fail1` + `Abort ERR`. Immediately after
erasing the region, the next SRAM load returned **rc=0,
`Disable configuration: DONE`, first attempt**, and the status register read
`0x00000000` (clean, no error bits). Nothing else changed.

The erase is a *tiny* JTAG transfer, so it works even when bulk writes do not.
That is why it is first.

## 1a. Provisioning a new card — the one and only JTAG session

**This is the procedure.** Do it once, on the bench, while the board is in your
hand. After it the card is network-managed for life: firmware and bitstream both
update over Ethernet (§2e), and a bad push cannot strand it (§2d).

You need JTAG for exactly two writes, and the order matters — the golden first,
so the card is never without a recovery image.

```bash
# 0. Build the pair you intend to run. build.sh prints the names to push later.
./build.sh --board e320 --panel 128x64 --sys-clk 35000000 bitstream   # the GOLDEN
./build.sh --board e320 --panel 128x64 firmware

# 1. GOLDEN -> 0x200000. Written once, never again. Pick the SIMPLEST image that
#    boots and reaches the network -- its only job is to be a way back. A plain
#    128x64 build is ideal; do NOT use the panel-specific gateware here, because
#    an image that fails for a panel reason is no use as a recovery path.
python3 -c 'd=open("bitstreams/e320/128x64.bit","rb").read()
i=d.find(bytes.fromhex("ffffffbdb3")); open("/tmp/golden.bin","wb").write(d[i:])'
./.ecpprog/bin/ecpprog -I B -p -X -o 0x200000 /tmp/golden.bin

# 2. APPLICATION -> offset 0. The image the card actually runs.
python3 -c 'd=open("bitstreams/e320/128x64.bit","rb").read()
i=d.find(bytes.fromhex("ffffffbdb3")); open("/tmp/app.bin","wb").write(d[i:])'
./.ecpprog/bin/ecpprog -I B -p -X -o 0 /tmp/app.bin

# 3. Let the board configure itself from flash.
./.ecpprog/bin/ecpprog -I B -t -a
```

Then confirm on the NETWORK, not over JTAG (§8h, and `ecpprog -t` de-configures a
running card):

```console
$ curl http://<card>/api/flash/bitstream
{"state":"idle", ..., "golden":true, "app_preamble":true}
$ curl http://<card>/api/bitstream
{"bitstream":{"name":"e320-128x64-out6-v1.1.0", ...},"firmware":{"version":"2.21.1"}}
```

`golden: true` is the line that matters. **The card refuses an over-the-air
bitstream write without it** (`FlashError::NoGolden`), because that write is the
one operation here that could genuinely need a cable.

### Why `-X`, and why two separate writes

`-X` skips `ecpprog`'s verify. On this card a JTAG read-back bit-slips and will
report a difference on a perfectly good write (§2b) — judge it by the board (§8h).

They are separate writes because the golden and the application are different
things with different lifetimes. The golden is a liability you accept once; the
application is replaced over the network from then on. Nothing in the firmware can
write the golden region — `region_is_writable()` refuses it unconditionally, with
no flag and no override — so the only way to lose your way back is to attach JTAG
and do it on purpose.

### A long JTAG flash write de-configures the running card

Expect it: writing ~450 KB takes the SPI port through ISC, which drops the running
design. The card goes unreachable until the `ecpprog -I B -t -a` in step 3, or the
next power cycle. That is not a failure, and on a card already in service it is the
reason to prefer an over-the-air push (§2e) over a cable.

### After provisioning: never JTAG again

| To change | Use | Live when |
|---|---|---|
| Rust firmware | `POST /api/v1/cards/{id}/firmware` | on reboot |
| FPGA bitstream | `POST /api/v1/cards/{id}/bitstream` | next power cycle |
| Panel layout / program | the card's record in marquee | next boot |

## 2. Reads: `ecpprog` is trustworthy, `openFPGALoader` is not

Same card, same region, minutes apart:

| Method | Result |
|---|---|
| `openFPGALoader --dump-flash`, two consecutive 1 MB reads | 45–57% of bytes differed |
| the same at **4 KB** | 78–81% differed |
| the same at 6 MHz / 1 MHz / 100 kHz | no improvement — not a clock issue |
| **`ecpprog -R 524288`** on an erased region | **all 8 sectors, 0 bytes non-`0xFF`** |
| **`ecpprog -R`** twice on a clean device | **byte-identical** |

So `--dump-flash` manufactured a 50–97% "corruption" rate on a link that reads
byte-perfectly. Everything derived from it was false: "the flash matches no
image in the tree", "the link is corrupting bulk transfers", "the cable needs
reseating", "the adapter is bad". Hours each.

**Caveat:** `ecpprog` reads are stable only on a **clean device state**. Two
`ecpprog` reads taken right after an `openFPGALoader` flash write differed in
108,046 bytes. Clear the state first (§5).

## 2b. A READ-BACK IS NOT EVIDENCE: this card's flash reads bit-slip

Measured 2026-10-07 on the E320. **Read the same 4 KB of real flash data twelve
times and you get up to twelve different answers.**

| Offset read | Clock | Reads agreeing with the majority |
|---|---|---|
| `0x210000` (real data) | 6 MHz | **1 of 12** |
| `0x210000` | 1.5 MHz | 4 of 12 |
| `0x210000` | 50 kHz | 2 of 12 |
| `0x3ff000` (99% `0xff`) | 6 MHz | 12 of 12 |

The corruption has a signature: from a random offset onward, every byte is the
correct byte **shifted left one bit**.

```
golden  4c 86 aa 4c 82 80 0c ac 4a
read    99 0d 54 99 05 00 19 58 95      0x4c<<1=0x98 -> 0x99, 0x86<<1 -> 0x0d
```

One SPI clock edge is lost or inserted mid-transfer and the rest of the stream
stays out of step. It is **not** clock related -- 50 kHz is no better than
6 MHz.

### The writes were fine the whole time

A read that comes back byte-exact is only possible if the flash content is
correct. Write a known pattern to an erased sector and read it twelve times:
**3 came back byte-exact**, so the write had landed perfectly -- while
`ecpprog`'s own verify of that same write said `Found difference`.

So on this card:

| Operation | Trustworthy? |
|---|---|
| Flash **write** | **Yes** -- content lands correctly |
| Flash **read** / verify, more than ~1 KB of non-`0xff` data | **No** -- ~75% corrupt |
| Flash read, tens of bytes | Usually fine |
| A region that is mostly `0xff` | **Reads clean even when broken** -- `0xff` shifted left is still `0xff`, so ones CANNOT reveal a slip. Never measure read health on an erased region (§0b). |

### What this invalidates

An entire session was spent chasing a "flash write truncation bug" that does not
exist. Every measurement of it -- "truncates at byte 348 at 6 MHz, byte 12,626
at 1 MHz, byte 1,145 at 100 kHz" -- was the point where a **read** lost bit
sync, attributed to the write. The numbers looked like they scaled with clock
because two of the three were taken in that order; the third broke the pattern.

**It also invalidates `factory/e320-golden-0x200000.bin`.** That file was
produced by a 768 KB JTAG read of the card on 2026-10-03, over this same path,
so it is almost certainly corrupt. Writing it back and getting BSE error 3
(CRC) was the ECP5 correctly rejecting a corrupt image, faithfully written. Do
not treat it as a golden. Build a bitstream instead -- `ecppack` output on disk
never traverses this link, so it is clean by construction.

### How to verify a write without trusting a read

Two ways, in order of preference:

1. **Let the board judge it** (§8h). Point the descriptor at the slot, REFRESH,
   read the status register. The ECP5 reads flash over its own dedicated pins,
   not through JTAG, so it is the one verifier that does not share this fault.
2. **Chunked verify with retry** -- `tools/flash_chunked.sh`. A bit-slipped
   verify always FAILS; it cannot produce a false pass. So `VERIFY OK` on a
   chunk is trustworthy even though the link is not: verify in small chunks,
   retry a failing chunk before believing it, rewrite only what fails
   repeatedly. Note this converges slowly here (10-15% of 4 KB chunks fail
   three verifies in a row) and a DIFFERENT set of chunks fails each round --
   which is itself the proof that the content is fine and the reads are not.

### The fault is the JTAG read path only -- the FPGA reads flash correctly

**CORRECTED 2026-10-07.** This section previously concluded that the SPI bus
between the ECP5 and the flash was physically marginal and that "no software
change can fix it". That was wrong, and it was wrong in the direction that
stops you working: it declares the card broken.

The evidence that settles it came from the card itself once it was running.
`GET /api/flash` reads flash through the FPGA's memory-mapped path and returned:

```
offset0      : ff ff ff bd b3 ff ff ff ff 3b 00 00 00 e2 00 00 00 41 11 10 43 ...
offset100000 : 00 07 0e 15 1c 23 2a 31 38 3f 46 4d 54 5b 62 69 70 77 7e 85 ...
```

The first is the bitstream preamble. The second is the `i*7` test pattern this
session wrote to `0x100000` -- byte-perfect, hundreds of bytes of it. **The
FPGA's own flash reads are completely clean.** Only reads issued over the JTAG
bridge bit-slip.

So the picture is:

| Path | Reliable? |
|---|---|
| Flash **write** over JTAG | **Yes** |
| Flash **read** over JTAG (`ecpprog`, `openFPGALoader`) | **No** -- ~75% corrupt on 4 KB of real data |
| Flash read by the FPGA (memory-mapped, `/api/flash`) | **Yes** -- byte-perfect |
| JTAG IDCODE / status | Mostly fine, but see below |

The `SPIm Fail1` and BSE error 4 that prompted the hardware theory had a plain
cause: **nothing was at flash offset 0**, which is where the ECP5 starts
reading. See the correction in §8j.

### Even IDCODE can come back corrupted, and it blocks programming

`openFPGALoader --detect` returned `0xa0111043` where the part is `0x41111043`
-- only the top byte wrong, the low three bytes correct. When that happens
inside `program_mem()` the loader refuses with

```
mismatch between target's idcode and bitstream idcode
    bitstream has 0x41111043 hardware requires 0x10408043
```

and will not write. That is upstream issue #727 plus this read unreliability on
top of it. `.ofl/bin/openFPGALoader` IS NOW THE PATCHED BUILD (the unpatched
1.1.1 is kept beside it as `openFPGALoader.unpatched-1.1.1`), but the patch
does not cure a genuinely mis-read IDCODE.

**When the IDCODE check blocks you, write the bitstream with `ecpprog`,** whose
writes are reliable and which does not gate on an IDCODE read. Strip the 28-byte
`.bit` header first so the preamble lands at the slot:

```bash
python3 -c 'd=open("bitstreams/128x64.bit","rb").read()
i=d.find(bytes.fromhex("ffffffbdb3")); open("/tmp/p.bin","wb").write(d[i:])'
./.ecpprog/bin/ecpprog -I B -p -X -o 0 /tmp/p.bin   # -X: write, skip the verify
./.ecpprog/bin/ecpprog -I B -t -a                   # REFRESH
```

`-X` matters: the verify reads back over the bad path and will report a
difference on a perfectly good write (§2b). Judge it by the board (§8h).


## 2c. A FAILED openFPGALoader write can still configure -- and run broken

The worst failure mode found on 2026-10-07, because every symptom points at
software.

`openFPGALoader -f <bitstream>` wrote the E320 bitstream and printed:

```
Error
Error
Fail
```

The preamble appeared at offset 0 and the card **configured** -- DONE high,
`0x00200100`, no BSE error -- then DHCP'd, netbooted over TFTP, and the firmware
died instantly and re-netbooted about twice a second. 1306 TFTP serves in ten
minutes was the only visible symptom.

Writing the *same* bitstream with `ecpprog -X` (a complete 426,374 of 426,374
bytes) and serving the *same* firmware gave a working card immediately: HTTP up,
`outputs 2, scan 32`, ISR counting, 0% packet loss, one TFTP serve.

**So a partial bitstream can pass the ECP5's own checks and assert DONE while
being functionally broken.** A gateware that configures is not a gateware that
is complete. When a card configures but its firmware will not stay up:

1. Rewrite the bitstream with `ecpprog -X` and confirm the byte count printed
   equals the payload size. Do not trust a write whose tool said `Fail`.
2. Only then suspect the firmware.

### What this is NOT

Two hours went into the firmware before the bitstream was rewritten, on the
theory that the Peripheral Access Crate was stale -- `colorlight.svd` is
rewritten per board by the *bitstream* build, and the checked-in PAC source was
three weeks older. That theory was wrong, and it is worth recording why so it is
not re-derived:

```
$ diff the two boards' csr.json
csr_bases : identical
memories  : identical
constants : identical except config_identifier and config_platform_name
```

**The 5A-75E and E320 SoCs have the same register map.** A stale PAC changes
only the identifier string's length, which is cosmetic. Regenerating it after
`--board` changes is still correct hygiene (`./build.sh ... pac`), but it was
not the fault here and a stale PAC will not crash a card.

## 2d. A GOLDEN image makes the card unbrickable — measured

This is the result bitstream OTA rests on, so it is written down with its
evidence.

**Park a known-good bitstream at a high flash address and the card cannot be
bricked by a bad image at offset 0.** When configuration from offset 0 fails,
the ECP5 scans forward through flash, finds the next valid bitstream, and loads
that instead — automatically, with no JTAG and no intervention.

Measured on the E320, 2026-10-07. Slot B (`0x200000`) held a 5A-75E bitstream
that reports `hw_outputs 6`; offset 0 held an E320 bitstream that reports
`hw_outputs 2`, so which one booted is unambiguous from `/api/status`:

| What was done to offset 0 | Card afterwards |
|---|---|
| nothing (baseline) | boots offset 0 — `outputs 2` |
| 512 zero bytes written mid-image, build used `--bootaddr 0x200000` | **boots slot B — `outputs 6`** |
| same corruption, build used `--bootaddr 0` | **boots slot B — `outputs 6`** |
| first 4 KB sector fully erased to `0xff` | **boots slot B — `outputs 6`** |

Every failure case came up on the network with 0% packet loss and a working
HTTP API.

### It does NOT depend on `--bootaddr`

The obvious theory was ECP5 multi-boot: `ecppack --bootaddr N` "sets next
BOOTADDR and enables multi-boot", so a primary that fails should jump to `N`.
That is not what is happening — a build with `--bootaddr 0`, naming no fallback
target at all, fell back just the same. So **do not** rely on a particular
`--bootaddr` value for the fail-safe; it comes from the preamble scan.

(For what it is worth, `--bootaddr` is **not** a separate descriptor you can
hand-build. Diffing two otherwise identical uncompressed bitstreams built with
`--bootaddr 0` and `--bootaddr 0x200000` shows the difference is one bit at byte
542376 inside the frame data plus a two-byte CRC at 542450 — it is internal
configuration state. An attempt to hand-write a 16-byte "JUMP image" at offset 0
in the factory's `0x3FFF10` format produced `preamble=1, BSE=0` and a card that
never configured.)

### What this licenses

```
flash 0x000000   application bitstream   <- OTA rewrites this
flash 0x200000   GOLDEN bitstream        <- written ONCE over JTAG, never again
flash 0x3FF000   multiboot descriptor sector
```

A bitstream update then has no unrecoverable window. Erase interrupted, write
truncated, image built wrong — any of them and the golden boots, the card is
reachable, and you push another image. The one invariant that must hold is that
**nothing may ever write the golden region**, which is `region_is_writable()`'s
job in `flash_write.rs`.

### The remaining constraint: making it take effect

Configuration is re-read on power-up or on a JTAG REFRESH. There is no
user-accessible self-reconfiguration primitive in this SoC, so a bitstream
pushed over the network **takes effect at the next power cycle**. That is still
"no JTAG ever again" — which is the goal — but it is not instant, and any claim
that it is would be wrong.

**Confirmed on a real power cycle, 2026-10-07.** The bitstream OTA was first
verified with a JTAG REFRESH standing in for a power-on, which proves the written
image is loadable but not that a cold board picks it. David then power-cycled the
card for real: it came up on the OTA-written image (`hw_outputs 2`, where the
golden reports 6), `isr_count` low as a fresh boot, 0% packet loss, golden still
intact. So the whole chain — write over the network, lose power, boot the new
gateware — is proven without a cable in it.

## 2e. Updating a provisioned card — over the network, no cable

Once §1a is done, both artifacts update over Ethernet. They are **different
things on different paths** and conflating them is the mistake to avoid:

| | Rust firmware | FPGA bitstream |
|---|---|---|
| File | `boot.bin` | `.bit` |
| Where it lives | **nowhere on the card** — refetched over TFTP every boot | the card's SPI flash, offset 0 |
| Live when | immediately, on reboot | **next power cycle** |
| Version | its own semver, `Cargo.toml` | its own semver, `gateware/VERSION` |

```bash
# Build everything and stage it, in the order the PAC requires:
tools/rebuild_all_variants.sh http://marquee

# Or one at a time. build.sh prints the exact name to push.
curl -F file=@.tftp/boot.bin;filename=e320-spwm-v2.21.1.bin \
     http://marquee/api/v1/firmware
curl -X POST http://marquee/api/v1/cards/2/firmware \
     -H 'Content-Type: application/json' -d '{"firmware":"e320-spwm-v2.21.1.bin"}'

curl -X POST http://marquee/api/v1/cards/2/bitstream \
     -H 'Content-Type: application/json' \
     -d '{"bitstream":"e320-256x256-icnd1065-hub320-out8-v1.1.0.bit"}'
```

A bitstream push takes a minute or two — every 4 KB is a sector erase — and the
card keeps running its current gateware throughout. It does **not** reboot: writing
flash does not reconfigure an FPGA, so rebooting would restart only the firmware
and leave you believing the new gateware was live.

**Names must be the identity.** marquee refuses any bitstream filename that is not
`<board>-<panel>-out<n>-v<semver>.bit`, because the fleet view compares what a card
reports RUNNING (read from the gateware's own identifier ROM) against what was
PUSHED. With an ad-hoc name those never agree and "already live" cannot be told
from "written, awaiting a power cycle".

### The failure that cost the most here

A name longer than the firmware's request buffer was **silently truncated**, so the
card asked the TFTP server for a file that did not exist and the push failed with
`written: 0` — the only clue a "not found" in the server log for a name that looked
almost right. Fixed in firmware 2.21.1 (the buffer is 96 bytes and an over-long
name is refused), but the lesson generalises: when a push reports zero bytes, read
the TFTP server's log and compare the requested name character by character.

## 3. Why "the flash doesn't match the file" is usually a measuring error

**`openFPGALoader` strips the `.bit` file's ASCII comment header before
writing.** A `.bit` begins:

```
ff 00 "Part: LFE5U-25F-7CABGA256" 00 | ff ff ff bd b3 ...
^-- 28-byte comment header ----------^ ^-- actual bitstream preamble
```

Flash receives the payload from offset **28** onward. So a byte-for-byte
`cmp` of flash against the `.bit` reports ~100% difference on a **perfectly
good** write. Compare from the `ffffffbdb3` preamble, or do not compare at all —
use `./build.sh verify-firmware`, which reads long, truncates and retries, and
where **one exact match is proof** (a slipped read cannot coincidentally equal
the source).

## 4. Writes: BOTH tools are broken above ~10 KB. This is unsolved.

Measured, `ecpprog`, pre-erased region:

| Write | Result |
|---|---|
| 4 KB at offset 0 | **VERIFY OK**, 0 bytes differ |
| 64 KB at offset 0 | only the **first 39 pages (~10,072 bytes)** land; the rest reads back `0xFF` |
| 4 KB at offset 4096 or 8192 (`-o`) | **fails** — `-o` writes do not land at all |
| 500 KB, 6 MHz, no `-p` | first difference at 8192 |
| 500 KB, 6 MHz, with `-p` | first difference at 16384 |
| 500 KB, 1 MHz (`-k 6`), with `-p` | first difference at 24576 |

The failure is **truncation, not corruption**: 51,335 of the differing bytes
read back as `0xFF`, i.e. never written. A slower clock lands more pages, which
is what insufficient BUSY polling between page programs looks like.

`-p` (disable write protection) is required — without it even less lands.

`openFPGALoader`'s flash write reports `Erasing`/`Writing` 100% and exit 0, but
the card then does not configure from flash (`ecpprog -I B -t -a` → status
`0x00000000`, unconfigured), so its write does not land either. **Chunking
around the bug does not work**, because `-o` is itself broken.

**Consequence: use `./build.sh recover` (volatile JTAG configuration) for now.**
Netboot already supplies firmware, so a flashed bitstream is only needed to
survive a power cut.

**The next experiment** is to establish whether `-o` is broken for writes only
or also for erase, and whether openFPGALoader's `--offset` works where
ecpprog's `-o` does not. Do not re-run the measurements above.

## 5. The two tools do not hand off cleanly

`ecpprog` leaves the device in ISC mode. The next `openFPGALoader` run then
fails with **`Enable configuration: FAIL`** and a status showing
`ISC Enable`/`Write Enable`/`Read Enable` already set.

**Clear the TAP between tools:**

```bash
openFPGALoader --cable ft2232_b --detect     # resets TAP; exit code is unreliable
```

This alone turned a failing write into `rc=0`.

## 6. Cables

`build.sh` uses **`ft2232_b`**; JTAG is on FT2232 channel **B** (channel A scans
`empty`, indistinguishable from an unpowered board). **Never pass `--cable`.**

`--cable tigard` works for `--detect`, flash ID and dumps, but reports a
**poorer status decode**. One identical failing SRAM load:

| `tigard` | `ft2232_b` |
|---|---|
| `Abort ERR` | `JTAG Active`, **`SPIm Fail1`**, `Abort ERR`, `EXEC Error` |

`SPIm Fail1` is the bit that identifies §1. On `tigard` you never see it.

## 7. marquee owns TFTP, not build.sh

`marquee-player` runs on host networking and owns UDP **6969**, serving the
fleet from its database. So `./build.sh boot`/`start` logs
`cannot bind 0.0.0.0:6969` and gives you no server, and **`.tftp/boot.bin` is
not what the card receives** — the default is
`marquee/data/firmware/boot.bin`. Read boots with
`docker compose logs player | grep tftp` on the marquee host.

A card whose MAC marquee does not know **still boots**, logging
`not identified ... serving default firmware`. A successful netboot is therefore
**not** evidence the card is registered.

The BIOS is netboot-**first**, so a firmware change needs no flashing at all:
stage it in marquee and reboot the card.

## 8. Tested and excluded — do not re-test

| Theory | Why it is wrong |
|---|---|
| The board is dead | IDCODE `0x41111043` and flash ID `0xC8 0x40 0x16` read 6/6 perfect throughout |
| A power cycle helps | Nothing in §1–§5 involves the card's power state |
| `--reset` wedges the E320 | `Skip resetting device` + `Refresh: FAIL` appears on **both** cables and exits 0 — the reset never happened. JTAG reset is fine |
| The Tigard voltage switch / `J33` | Tested across several sessions, no change (also `HARDWARE.md` §8b) |
| `ftdi_sio` steals the interface | Unbound from both channels; no change. libftdi already detaches it |
| USB autosuspend / re-enumeration | `power/control` is `on`, never suspended, zero enumeration or error events |
| The JTAG link corrupts bulk transfers | A 512 KB `ecpprog` read is byte-clean and repeatable. §2 |
| Erase is failing | All 8 sectors of a 512 KB erase verified 100% `0xFF` |
| Lowering the JTAG clock fixes configuration | No effect at all (200 kHz = 6 MHz). It affects only §4 |
| It is the bitstream | Three different bitstreams fail identically |
| The 4-group HUB320 build steals the Ethernet PHY pins | Checked in the generated LPF: `J4`'s second half (`P7 M7 P8 R8 M8 M9`) is **unassigned**, and `eth0` is fully constrained |
| Wrong board revision | `build.sh` and the E320 platform both use `8.2` |

## 8b. The sharpest statement of the open fault: writes fail, reads don't

Everything that fails is a **large write toward the device**; everything that
reads back is clean. Measured repeatedly on 2026-10-04 with flash erased, so
§1 is not in play:

| Direction | Operation | Result |
|---|---|---|
| read | `ecpprog -R 524288` | byte-clean, repeatable |
| read | flash ID, IDCODE | 6/6 perfect |
| erase (small write) | `-e 524288`, all 8 sectors | 100% `0xFF`, always works |
| write | flash page programs past ~10 KB | silently truncated (§4) |
| write | JTAG SRAM configuration (~450 KB shift) | `Abort ERR` or `CRC ERR` |

The SRAM configuration path touches **no flash at all** — it is a pure JTAG
shift — and it fails at **every** clock rate tried (100 kHz, 500 kHz, 1 MHz,
2 MHz, 6 MHz), with both `openFPGALoader` and `ecpprog -S`, and with three
different bitstreams that all pass timing (the 2-group E320 build closes at
56.68 MHz against 40, Ethernet 137 MHz against 125).

`CRC ERR` specifically means the bitstream arrived corrupted. So the outbound
(TDI) direction is unreliable while the inbound (TDO) direction is not.

**But it is intermittent, and that matters:** configuration *did* succeed once
this session — `rc=0`, `Disable configuration: DONE`, status `0x00000000` — on
the first attempt right after an erase. So the path is not dead, and three
consecutive retries afterwards all failed.

Two notes on measuring this, both of which cost time:

- `ecpprog -S` returns **rc=0 even when configuration failed**; it never checks
  `DONE`. Read its status line instead: `0x...0100` has `DONE` high,
  `0x02800000` is BSE error 5 (Abort).
- Inserting an `openFPGALoader --detect` between the erase and the
  configuration **breaks** it (`Abort ERR`), even though that same `--detect` is
  the right fix for the ecpprog→openFPGALoader handoff on a *flash write* (§5).
  Go straight from erase to configure.

## 8c. Two traps in measuring any of this

**An erased region cannot prove a read works.** Erased flash is all `0xFF`, and
`0xFF` is also what a failed read returns (floating MISO pulls high). On
2026-10-04 "`ecpprog -R 524288` is byte-clean and repeatable" was used to
conclude the JTAG link was healthy — but that read was of an *erased* region, so
it proved nothing. On real content two consecutive reads of the same region
differed in 9,425 bytes. **Only a read of known non-`0xFF` content counts**; the
smallest honest one is `ecpprog`'s own verify after a 4 KB write, which does
report `VERIFY OK`.

**`rc=0` from either tool does not mean the FPGA is configured.** Neither checks
`DONE`. `ecpprog -S` returns 0 while its own status line reads `0x02800000`
(BSE error 5, Abort, `DONE` low); `openFPGALoader` prints
`Disable configuration: DONE`, which is only the ISC-disable step. A
configuration reported as successful here was followed by **zero** network
traffic from the card.

Reading the status to check `DONE` is itself destructive: `ecpprog -I B -t`
does an `init..`/reset that can clear the configuration you are trying to
observe. There is no non-destructive `DONE` read with these tools.

## 8d. Compression is NOT the cause — a correction

An earlier version of this file said compressed bitstreams fail JTAG
configuration while uncompressed ones work. **That was wrong**, and it is left
here as a correction rather than deleted because it is the kind of plausible
pattern that gets re-derived.

What actually happened: the comparison was run while an orphaned
`openFPGALoader` held the FTDI device (§8g), and the 3-of-3 successes credited
to "uncompressed" were in fact the **compressed** image — the uncompressed build
overwrote that file afterwards. A later 693 KB uncompressed build then failed
where a 448 KB compressed one had succeeded, which points at size if anything,
and that is confounded by a cable being moved at the same time.

**Configuration on this bench is intermittent.** Measured the same afternoon,
no code or image changes in between: 5 of 6 attempts succeeded, then 0 of 24.
So judge nothing from a handful of attempts, and check §8g before forming any
theory at all.

`./build.sh --no-compress` exists and is harmless to try, but it is not a fix
with evidence behind it.

## 8g. Check for an orphaned JTAG process BEFORE anything else

```bash
pgrep -x openFPGALoader || echo clean          # must print "clean"
pkill -x openFPGALoader                        # -x, NOT -f (see below)
```

An orphaned `openFPGALoader` holds `/dev/bus/usb/001/<dev>` and poisons every
run after it. On 2026-10-04 one `--detect -f`, left behind when the harness
backgrounded a command at its 120 s timeout, held the device for over twenty
minutes while spinning. Symptoms:

- `unable to open ftdi device: -5 (unable to claim usb device. Make sure the
  default FTDI driver is not in use)` — which reads as an `ftdi_sio` problem
  and is **not** one; `ftdi_sio` was unbound at the time.
- Intermittent `Abort ERR` / `CRC ERR` on configuration. With contention
  cleared, a bitstream that had failed 18 times straight configured 3 for 3.

Two traps in cleaning up:

- **`timeout` does not protect you.** It kills the child it starts, not an
  orphan from an earlier call.
- **Use `pkill -x openFPGALoader`, never `pkill -f openFPGALoader`.** The `-f`
  form matches the shell wrapper of the very command running it, so it kills
  itself (exit 144) and leaves the orphan alive. The same applies to
  `ps | grep -E "[o]penFPGALoader"`, which still matches its own wrapper.

`ftdi_sio` DOES also claim the FT2232H and re-attaches within a second of any
release, but libftdi detaches it on its own; unbinding it by hand changed
nothing in a controlled test (0 of 5 configurations either way). It is not the
problem. Unbinding BOTH interfaces triggers a re-probe and makes things worse.

## 8h. Judging a flash write by the board, not the programmer

When a verify failure might be a bad READ rather than a bad write (§2, §8c),
settle it without the programmer: write with `-X` (no verify), then make the
ECP5 configure **itself** from flash and read the status.

```bash
ecpprog -I B -p -X <bitstream>      # write, skip the programmer's verify
ecpprog -I B -t -a                  # ECP5 reads the image itself; short JTAG op
```

The bitstream data never crosses JTAG on that second step — only the trigger
does, and short transfers stay reliable even on a link that corrupts bulk ones.
So the status register is independent evidence:

| Status | Means |
|---|---|
| `0x00200100` (`DONE` high) | the flash image is good |
| `0x05800000` | BSE error 3 (CRC) + EXEC error, `DONE` low — the image is genuinely bad |

Measured 2026-10-04: `0x05800000`, confirming that `ecpprog`'s verify failure at
offset 16384 was real and the write had truncated. Worth knowing because it is
the only way to get a trustworthy answer when both the read and the write paths
are suspect.

## 8i. The Tigard's target-voltage switch is FOUR-way

`1V8` / `3V3` / `5V` / `VTGT`. **With a powered target it belongs on `VTGT`**,
so the target powers Tigard's level shifters at its own rail.

`HARDWARE.md` used to call this a two-position switch (`VTGT` vs `3V3`). It is
not, and the error is worth knowing about: on 2026-10-04 a card failed every
bulk transfer for hours while `1V8` was never considered, because it was not in
the documented list. At `1V8` the shifters drive 1.8 V into a 3.3 V board, right
at the input threshold — short transfers latch correctly, long ones accumulate
bit errors, and the clock rate makes no difference. **That is not separable from
a genuine link fault by software**, so check the switch physically before
spending time on tooling.

Upstream: github.com/tigard-tools/tigard, and
learn.securinghardware.com's Tigard course overview.

## 8j. The card DOES boot from offset 0 -- that is where to write

**CORRECTED 2026-10-07.** This section used to say the opposite, and the
inversion cost most of a session: it sent me to write bitstreams at `0x200000`
and point a descriptor at them, while the FPGA sat reading address 0 and finding
`0xff`. The status register said BSE error 4, "no preamble", which was the literal
truth.

**An ECP5 starts reading flash at address `0x000000` at power-up and on
REFRESH.** A bitstream written to offset 0 is what boots. That is also exactly
what `openFPGALoader -f <bitstream>` and `build.sh flash` do by default -- the
ordinary path is the correct one, and needs no descriptor at all.

Proven on the E320 on 2026-10-07: with the bitstream payload at offset 0 the
status register read `0x00200100` -- DONE high, standard preamble, no BSE error,
`SPIm Fail1` clear -- and the card went on to DHCP, netboot over TFTP and serve
HTTP. With offset 0 erased and a descriptor pointing at a good image at
`0x200000`, it did not configure at all.

### What the descriptor at 0x3FFF10 actually is

The multiboot JUMP descriptor is real and the factory does use it:

```
ff ff bd b3  ff ff ff ff  7e 00 00 00  03 20 00 00
sync         dummy        JUMP         SPI read 0x03 @ 0x200000
```

But it is a **multiboot** feature -- a redirect taken by a primary image, not
the first thing the FPGA reads. It does not substitute for having something
valid at offset 0. Writing only the descriptor leaves a card that cannot boot.

So: **write the bitstream to offset 0.** Use the A/B slots and the descriptor
only for the OTA flow that deliberately manages them (`flash_write.rs`).

### The status register is LATCHED -- read it twice

Three times this session a status read was interpreted as the result of the
REFRESH that had just happened when it was in fact the previous attempt's.
After a REFRESH, `0x02c00000` (BSE 5, ABORT) was followed moments later by
`0x00600100` with DONE high: the card had configured and the first read was
stale. §0d says this; it is easy to read too fast and conclude the opposite of
what happened.

Worse, `ecpprog -t` reads the flash ID, which takes the SPI port away from the
FPGA and **knocks a configured device out of configuration** -- so DONE reads
high once and then zero on every later poll. Do not use repeated `ecpprog -t`
to confirm a card stayed up. Confirm it on the network instead.


## 9. Known-unknown — not solved




The E320 bench card **configures cleanly** after §1 and then produces **no
network traffic at all** — no DHCP, no ARP, no TFTP — with both the 4-group
HUB320 bitstream and the stock `bitstreams/128x64.bit`. It is absent from the
ARP table at every address on the segment. It netbooted normally at 21:59 on
2026-10-03, so this is a change, and nothing listed in §8 explains it.

Not yet distinguished:

- whether `DONE` is actually high after a successful load — `ecpprog -I B -t`
  does an `init..`/reset that can itself clear the configuration, so the status
  read disturbs what it measures;
- whether the Ethernet link is up (the bench segment is **not** on a
  UniFi-managed switch, so the port cannot be queried remotely);
- the 2-group E320 control build now **builds and closes timing** (fixed: it
  was a slot-map bug in `hub75_conn`, not missing pin data), but it cannot be
  loaded onto the card while §8b holds, so it has not yet ruled the Ethernet
  pinout in or out.

**The silence and the JTAG trouble are separate problems.** Configuration has
now reported success (`rc=0`) more than once, including with an uncompressed
bitstream, and the card still produced no DHCP, ARP or TFTP at either its DHCP
address or the gateware's compiled-in fallback `192.168.1.50`. So fixing §8b
would not by itself bring the card back.

What has NOT been checked, because it cannot be checked from here: whether the
card's Ethernet link is up and whether its supply is healthy. The bench segment
is not on a UniFi-managed switch, so the port cannot be queried remotely, and
there is no remote power. Note for whoever does look: a rail that powers the
JTAG TAP (microamps) but sags when the fabric configures or the PHY comes up
would produce exactly this set of symptoms — small JTAG operations reliable,
large ones corrupt, configuration intermittent, and silence after a "successful"
load. That is a hypothesis, not a finding.

## See also

- `HARDWARE.md` §7 — JTAG, and why `--detect` proves nothing
- `HARDWARE.md` §1b — the E320 variant and its measured pin map
- `PROGRAMMING.md` — display programs, the *other* kind of programming
