import { useQuery } from "@tanstack/react-query";
import { HelpCircle, Search } from "lucide-react";
import { useMemo, useState } from "react";
import { api } from "../lib/api";
import { PageHeader } from "../components/widgets";

/** A help entry. `k` is extra search keywords that need not appear in the prose —
 *  people search for the symptom ("frozen", "black") not the cause. */
type Entry = { h: string; p: string; code?: string; k?: string };
type Group = { g: string; blurb: string; items: Entry[] };

const GROUPS: Group[] = [
  {
    g: "Concepts",
    blurb: "What this is and why it is built this way.",
    items: [
      { h: "What do I need running?",
        p: "Just Marquee. Once a Colorlight card has been programmed, nothing from the colorlight repo runs at runtime — that repo builds gateware and firmware, compiles on-panel programs, and flashes boards. Marquee serves the panel its firmware and config at boot, then streams it pixels or pushes it values. You go back to colorlight only to write a NEW on-panel program, change panel geometry, or build new firmware; switching a panel between programs its firmware already carries is a change here.",
        k: "need running requirements colorlight repo runtime what do i need dependencies" },
      { h: "What Marquee is",
        p: "Fleet control and content for LED matrix panels driven by Colorlight receiver cards. It owns the panel inventory, renders content host-side, streams finished frames over the panel's UDP protocol, and boots panels over TFTP.",
        k: "overview intro about" },
      { h: "Why rendering happens here",
        p: "The panel CPU is a VexRiscv with no data cache; measured, its pixel loop tops out near 600,000 px/s and it cannot do fonts, codecs or HTTP. Rendering on a host and sending finished frames is what a signage media server does, and it means changing content never touches the panel.",
        k: "architecture design cpu" },
      { h: "The two ways to drive a panel",
        p: "STREAM mode: Marquee renders frames and sends pixels, ~24 Mbit/s, and the panel displays what it is given. PROGRAM mode: the panel runs its own compiled program and Marquee sends only named VALUES, about 20 bytes/s. A panel is in exactly one mode. They can never share a panel — two writers on one framebuffer looks like corruption rather than like a conflict.",
        k: "mode modes stream program difference" },
      { h: "What each mode costs you",
        p: "Streaming can play anything — video, photos, live camera — and reaches ~103 fps at 128x128 in indexed format. But when the host dies the last frame stays lit and looks correct forever. A program tops out near 12.5 fps and cannot play video, but it keeps running with nothing plugged in and can display the fact that its data went stale. A sign that lies is worse than a blank one.",
        k: "compare tradeoff choose which mode fps failure" },
    ],
  },
  {
    g: "Content",
    blurb: "Scenes, layers, sources and bindings.",
    items: [
      { h: "Scene documents",
        p: "A show is YAML: a display, then scenes played in order and looped. Each scene is layers drawn into boxes — solid, gradient, image, video, text, graph. Layers take box [x,y,w,h], opacity and z (higher draws on top; ties break by declaration order). Durations are written '10s', '500ms', '2m'.",
        code: `display: {width: 256, height: 128, fps: 24, format: rgb}
scenes:
  - name: welcome
    duration: 10s
    layers:
      - {type: image, src: logo.png, box: [8,72,48,48], fit: contain}
      - {type: text, text: "{clock:%H:%M}", box: [64,100,192,20], size: 16}`,
        k: "show yaml scene layer syntax write" },
      { h: "Where shows are stored",
        p: "Shows live as files in custom/<site>/shows/ in the repo, and are loaded into the database. The database is what the player reads; the files are what gets reviewed and backed up, and nothing keeps the two in step automatically. The Content page says which way round a show has drifted — 'differs from <file>' means it is only in the database, '<file> is newer' means the wall has not seen the change yet — and the export/import buttons there do the same thing as `make shows-export` and `make shows-import`.",
        k: "custom shows files backup export import version control where stored git" },
      { h: "Sources",
        p: "Local files; plex:Home Movies/x.mp4 for the mounted libraries; youtube:<id>; cam:<name> and wowza:<stream> for live feeds; and raw rtsp/rtmp/http passed to ffmpeg. Camera and Wowza URLs live in Settings, never in a show, so a show stays safe to share and a re-addressed camera is one settings change.",
        k: "video image plex youtube camera rtsp media file" },
      { h: "Bindings",
        p: "Resolved at render time inside text: {clock:%H:%M}, {date:%a %d %b}, {file:/path}, {http:url}, {ha:sensor.x}, {bus:home.status|weather.temp}, {env:NAME}. A failed lookup renders an em dash rather than raising — a sign that goes black because an API timed out is worse than one showing a stale value. An unknown binding is left visible so the typo shows up on the panel.",
        k: "template variable clock date home assistant dynamic text" },
      { h: "Graphs",
        p: "type: graph plots a Home Assistant entity's history — a line, an optional fill, an optional baseline, and nothing else. On a 64px-tall panel axes and gridlines spend most of the pixels on furniture. Autoscale is the default and is often wrong: a flat signal autoscales into a box full of noise, so pin min/max for anything with a known range. Non-numeric states are dropped rather than plotted as zero, which would draw a cliff to the floor that looks like real data.",
        k: "chart sparkline sensor history plot" },
      { h: "rgb or indexed",
        p: "Set per show in display.format. rgb sends three bytes a pixel. indexed quantises to 256 colours and sends one, which is ~3x fewer PACKETS — and packets, not pixels, are what the panel is limited by. Measured at 128x128: 36 fps rgb against 103 fps indexed. Text, charts, logos and flat graphics quantise invisibly; photographs and video band. Choose by content, not by panel.",
        k: "format quantise colour color palette fps faster speed" },
    ],
  },
  {
    g: "Walls and cards",
    blurb: "What a viewer sees, and the boards that drive it.",
    items: [
      { h: "A wall is not a card",
        p: "A WALL is what a viewer sees: one logical display with one picture on it. A CARD is one receiver board driving a rectangle of that wall. They used to be the same thing, called a panel, and that worked only while one board could drive a whole display. It cannot once a wall gets large: a receiver has a fixed number of output connectors (typically eight), its framebuffer holds about 327,680 pixels — roughly ten 256x128 modules — and its memory bandwidth sets the refresh rate. Content is assigned to the wall; devices are configured per card.",
        k: "wall card panel difference what is a wall receiver board split two cards multiple" },
      { h: "Why split a wall across two cards",
        p: "Beyond the connector count, it makes the wall FASTER. Each card only reads its own half of the picture from memory, and memory bandwidth is what limits refresh — a nine-module wall runs at about 36.8 Hz on one card and about 49.1 Hz split across two. It also lifts the framebuffer ceiling, since each board only has to hold its own region. This is the same reason commercial LED systems scale by adding receivers rather than by making one bigger.",
        k: "why two cards faster refresh performance bandwidth framebuffer limit scale more panels" },
      { h: "Placing a card on a wall",
        p: "Each card has an x and y giving its top-left corner within the wall, plus its own width and height. A wall driven by a single card is 0, 0. The Walls page draws the layout to scale, so a gap (a black band on the glass) or an overlap (two boards rendering the same pixels) is visible as a shape rather than as something you have to work out from coordinates.",
        k: "position x y origin layout place coordinates gap overlap gaps gapped gray band" },
      { h: "How one picture reaches several cards",
        p: "The wall canvas is rendered ONCE per frame and each card is sent its own crop of it. That is cheaper than rendering per card, and it is the only way two boards can show halves of one picture without drifting apart — they are handed the same frame, from the same render, in the same pass. A card has no idea it is part of anything larger; it receives a frame sized to its own region and displays it.",
        k: "crop sync tear seam split render how does it work same frame" },
      { h: "Booting cards",
        p: "The player serves TFTP on port 6969 — not 69; the gateware sets TFTP_SERVER_PORT=6969. A card gets the firmware assigned to it (or the default boot.bin) and its layout YAML generated from its record. A healthy boot fetches TWO files. boot.bin without the layout means the firmware started and died before it could ask for its configuration, which is a gateware/firmware mismatch rather than a network problem.",
        k: "tftp netboot firmware boot start power" },
      { h: "What wins at boot",
        p: "Precedence is: the TFTP config Marquee serves, then any config compiled into the card's firmware, then a bare default expecting a stream. So Marquee always wins when it answers — a card with a baked-in default cannot ignore a change you make here. The baked config is what it shows before Marquee answers, and what it falls back to if Marquee never does.",
        k: "precedence override wins default baked conflict which config" },
      { h: "Setting a card's mode",
        p: "The mode dropdown on a card is Network or Program. It is stored on the card record and appended to the layout the card fetches at boot, so it survives a reboot. It is appended at serve time rather than stored in the layout text, so there is one source of truth and editing a layout cannot accidentally strand a card in program mode. On a multi-card wall one board can be in program mode while its neighbours stream.",
        k: "program network switch change mode reboot persist" },
      { h: "Choosing an on-board program",
        p: "With mode set to Program, pick from the card's own library — Marquee asks the card which programs its firmware contains. One firmware carries them all and the config selects one, so changing program is a config change and not a rebuild. Adding a NEW program does require building and deploying firmware.",
        k: "library select picker which programs" },
      { h: "Scheduling",
        p: "Windows are local wall-clock and may wrap midnight. The highest-priority window covering the current time wins; otherwise the wall plays its standing assignment. Schedules are set per WALL, so every card covering one picture changes together. Wall-clock is stored rather than an instant so 18:00 stays 18:00 across a DST change. The schedule is re-evaluated every tick, so a change takes effect within one scene frame without restarting anything.",
        k: "daypart time window when priority override" },
      { h: "Card stats",
        p: "Counters are sampled and stored raw, not as rates, per CARD rather than per wall. A counter going backwards is how a reboot is detected — a pre-computed rate would hide it. History shows fps, refresh Hz, MAC overflows, CRC errors, reboots and availability. On a two-card wall it is the difference between the two that tells you which board is struggling.",
        k: "metrics history counters graph monitoring" },
    ],
  },
  {
    g: "On-panel programs",
    blurb: "Panels that draw their own pixels.",
    items: [
      { h: "What a program is",
        p: "A compiled function of (context, x, y) that returns a palette index — the panel asks it for every pixel of every frame. Programs are written in YAML with optional Rust lambdas, compiled by panelc into the firmware. Nothing is interpreted on the panel: a per-pixel YAML interpreter was measured at 0.3 fps.",
        k: "panelc write authoring esphome lambda yaml rust" },
      { h: "Values — the data plane",
        p: "Marquee pushes named values to program-mode panels every 10 seconds, sourced from the panel bus (weather, doors, windows, garage). That is about 20 bytes a second against the ~24 Mbit/s a streamed panel needs to show the same clock face. Panels in Network mode are skipped automatically — Marquee asks each panel what mode it is in rather than trusting a second setting.",
        k: "data push temp sensor home assistant values 20 bytes" },
      { h: "Staleness",
        p: "Because the pixels are already on the panel, a program can show that its data went quiet — the stale_marker primitive. This is the thing a streamed panel cannot do: when the host dies it keeps displaying a frame that looks perfectly correct.",
        k: "stale old data dead feed marker offline" },
      { h: "Panels that need no network",
        p: "A panel's firmware can carry its own layout and program, baked in at build time with ./build.sh --default-config in the colorlight repo. Such a panel comes up running its program with nothing plugged in but power — no Marquee, no DHCP, no TFTP. It also helps managed panels: the baked config is applied before the network comes up, so content appears about a second after power-on instead of after DHCP has finished.",
        k: "standalone offline no network isolated without server power only" },
      { h: "An offline panel has no data",
        p: "With no network there is no value push, so every key reads as a dash and the staleness clock only grows. A program intended to run standalone should either use no values at all, or be written so that missing data looks deliberate rather than broken. Test it the way it will run: clear the panel's layout here, reboot it, and look at what you actually get.",
        k: "offline no data values dash stale standalone test" },
      { h: "Writing one",
        p: "In the colorlight repo: write panels/<name>.yaml, run panelc, build the firmware, deploy it. See docs/PROGRAMMING.md there for the widget and lambda reference, the drawing library, and the performance rules. docs/GETTING-STARTED.md walks the whole loop from scratch.",
        k: "how to create new program guide docs" },
    ],
  },
  {
    g: "Integration",
    blurb: "Calling Marquee from other systems.",
    items: [
      { h: "Play something from another app",
        p: "POST /api/v1/play with a panel and either a saved show name or inline scene YAML. Machine callers authenticate with scoped, revocable API tokens minted in Settings — never a shared secret, so a compromised caller is revoked on its own without breaking everyone else.",
        k: "api rest automation trigger token auth integrate" },
      { h: "Live data",
        p: "Marquee holds ONE panel-bus subscription for the whole fleet and fans it out, rather than each panel or show talking to upstream services. One integration, one set of credentials, one place to fix a broken feed — and the render path never touches the network, because a sign redrawing 20 times a second must read a value from memory, not make a request.",
        k: "bus websocket push poll home assistant feed" },
      { h: "Home Assistant",
        p: "Set ha.base_url and ha.token in Settings. Without them {ha:...} renders the no-value glyph and graphs draw just their baseline — the rest of the scene is unaffected. A missing integration must never blank a sign.",
        k: "ha hass token setup configure" },
    ],
  },
  {
    g: "Troubleshooting",
    blurb: "Symptom first, because that is what you have.",
    items: [
      { h: "The panel shows a frozen or corrupt picture",
        p: "Almost always the panel's pixel DMA is off. A panel boots with it OFF, and on the CPU pixel path the receive interrupt cannot drain the MAC FIFO: roughly 3 chunks of every 12 are lost, so NO frame ever completes and the panel keeps showing whatever was on it. It reads as frozen, not as slow. Marquee re-asserts the DMA every supervisor tick, so this should self-heal within a tick — if it does not, check that the panel is reachable over HTTP.",
        k: "frozen stuck corrupt garbage nothing changes dma stale image" },
      { h: "The whole panel is one colour but I can see the shapes",
        p: "The panel is in indexed output mode while the content is full colour (or the reverse). Each pixel's low byte is being read as a palette index. Check the panel's /api/display for the mode it is really in. This is a real fault, not a colour-calibration problem.",
        k: "one colour red blue monochrome wrong colours indexed" },
      { h: "A panel went offline and came back showing old content",
        p: "Expected for a streamed panel: the last complete frame stays lit. If it is showing content from a previous show, the stream stopped rather than the panel failing — check the player logs and the panel's reachability.",
        k: "offline old content previous last frame" },
      { h: "The picture stutters periodically",
        p: "Usually broadcast traffic on the panel's network segment. Every ARP the panel handles lands in the same interrupt that consumes pixels. The panel filters foreign ARP and multicast in its fast path, but a busy general-purpose subnet still costs it. A dedicated panel VLAN removes the broadcast domain instead of filtering it.",
        k: "stutter jitter glitch pause hitch periodic" },
      { h: "Frames are being rejected",
        p: "If the panel's bad_size counter is climbing, the frame geometry does not match the panel. Wrong-sized frames are refused rather than drawn as a garbled part-frame. Check the panel's width and height against its layout.",
        k: "bad_size rejected wrong size geometry mismatch" },
      { h: "A program-mode panel is not updating",
        p: "Check the panel's values — if they are stale, the push is not arriving. The value pusher skips panels that report themselves as streaming, so confirm the panel's mode is Program in its own /api/display, not just in Marquee. A panel that rebooted into stream mode will silently stop receiving values.",
        k: "program not updating values stale not changing" },
      { h: "A panel came up as one small panel, not the full wall",
        p: "It got no layout. Either Marquee did not serve one — check the player log for 'no layout for <mac>.yml', which means the panel record has no layout YAML — or the panel booted with no network at all and has no config compiled into its firmware, in which case it falls back to a single panel at the bitstream's geometry, expecting a stream.",
        k: "one panel small wrong size single panel layout missing geometry" },
      { h: "Nothing plays after assigning a show",
        p: "Check the panel is enabled, that it is in Network mode (a program-mode panel is deliberately skipped by the player), and that a schedule window is not overriding the assignment with something else.",
        k: "not playing wont start nothing happens assign show" },
    ],
  },
  {
    g: "Reference",
    blurb: "Numbers and names worth having to hand.",
    items: [
      { h: "Ports",
        p: "UDP 7000 — pixels. UDP 6969 — TFTP boot (not 69). TCP 80 on each panel — its own status page and JSON API. Marquee's API is on 8410 and this UI on 8411.",
        k: "port number udp tcp 7000 6969 firewall" },
      { h: "Wire format",
        p: "10-byte header, then pixels. 487 pixels per chunk in rgb, 1461 in indexed. 1461 is 3 x 487 and is not negotiable: 1462 is not divisible by three and would place every chunk after the first one pixel left of where the hardware writes it, which shows as shearing rather than as an error.",
        k: "protocol packet chunk header 487 1461 udp" },
      { h: "Measured frame rates",
        p: "Streaming at 128x128: ~36 fps rgb, ~103 fps indexed. On-panel programs: ~12.5 fps is the ceiling for a full redraw, because a framebuffer store costs ~190 cycles and there is no data cache. A dashboard using partial redraw reaches ~8.5 fps; a scrolling ticker using shift-and-fill reaches ~25 fps.",
        k: "fps speed performance benchmark how fast rate" },
      { h: "Where the docs are",
        p: "In this repo: doc/scenes.md is the scene language reference, ARCH.md is how the services fit together, API.md is the HTTP API. In the colorlight repo: GETTING-STARTED.md, PROGRAMMING.md, HARDWARE.md, FPGA-GUIDE.md and BENCHMARKS.md.",
        k: "documentation manual readme where find docs" },
    ],
  },
];

const ALL = GROUPS.flatMap(g => g.items.map(i => ({ ...i, g: g.g })));

export default function Help() {
  const [q, setQ] = useState("");
  const [tab, setTab] = useState<string>("All");
  const { data: panels } = useQuery({ queryKey: ["panels"], queryFn: () => api<any[]>("/panels") });
  const { data: shows } = useQuery({ queryKey: ["shows"], queryFn: () => api<any[]>("/shows") });

  const needle = q.trim().toLowerCase();
  const hits = useMemo(() => ALL.filter(e => {
    if (tab !== "All" && e.g !== tab && !needle) return false;
    if (!needle) return true;
    return (e.h + " " + e.p + " " + (e.code ?? "") + " " + (e.k ?? "")).toLowerCase().includes(needle);
  }), [needle, tab]);

  // Searching looks across everything; browsing is per-category.
  const grouped = GROUPS
    .map(g => ({ ...g, items: hits.filter(h => h.g === g.g) }))
    .filter(g => g.items.length);

  const programPanels = panels?.filter(p => p.mode === "program").length ?? 0;

  return (
    <div className="space-y-4">
      <PageHeader title="Help" icon={HelpCircle}>how Marquee works, for this install.</PageHeader>

      <div className="card flex flex-wrap gap-6 text-sm">
        <div><div className="tile-label">panels</div><div className="text-2xl font-semibold">{panels?.length ?? "—"}</div></div>
        <div><div className="tile-label">online</div><div className="text-2xl font-semibold">{panels?.filter(p => p.state === "online").length ?? "—"}</div></div>
        <div><div className="tile-label">on-panel</div><div className="text-2xl font-semibold">{panels ? programPanels : "—"}</div></div>
        <div><div className="tile-label">shows</div><div className="text-2xl font-semibold">{shows?.length ?? "—"}</div></div>
      </div>

      <div className="relative">
        <Search size={15} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-faint" aria-hidden />
        <input className="input pl-9" placeholder="search help — try 'frozen', 'indexed', 'schedule'…"
          value={q} onChange={e => setQ(e.target.value)} />
      </div>

      {!needle && (
        <div className="flex flex-wrap gap-1.5">
          {["All", ...GROUPS.map(g => g.g)].map(t => (
            <button key={t} onClick={() => setTab(t)}
              className={t === tab ? "btn-primary" : "btn"}>{t}</button>
          ))}
        </div>
      )}

      {needle && (
        <div className="text-xs text-muted">
          {hits.length} {hits.length === 1 ? "result" : "results"} for “{q.trim()}”
          {hits.length > 0 && " — searching all categories"}
        </div>
      )}

      {grouped.map(g => (
        <section key={g.g} className="space-y-2">
          <div>
            <h2 className="text-sm font-semibold uppercase tracking-wide">{g.g}</h2>
            {!needle && <p className="text-xs text-faint">{g.blurb}</p>}
          </div>
          {g.items.map(e => (
            <article key={e.h} className="card">
              <h3 className="font-semibold">{e.h}</h3>
              <p className="mt-1 text-sm leading-relaxed text-muted">{e.p}</p>
              {e.code && (
                <pre className="mt-2 overflow-x-auto rounded-lg bg-raised p-3 text-xs leading-relaxed">
                  {e.code}
                </pre>
              )}
            </article>
          ))}
        </section>
      ))}

      {!hits.length && (
        <div className="card text-sm text-muted">
          No matches for “{q.trim()}”. Try a symptom (“frozen”, “one colour”), a
          feature (“schedule”, “indexed”), or clear the search to browse by category.
        </div>
      )}
    </div>
  );
}
