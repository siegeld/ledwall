import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { CircuitBoard, ExternalLink, FileCode2, Pencil, Plus, Trash2 } from "lucide-react";
import { useState } from "react";
import { api, BASE } from "../lib/api";
import { PageHeader, StatusChip, Empty } from "../components/widgets";

/** Pick which on-board program boots on this card.
 *
 * The list comes from the CARD, not from marquee: one firmware carries every
 * program, so what is available is a property of the build on that board. A
 * copy here would drift the moment a card was flashed.
 */
function ProgramPicker({ card, onPick }: { card: any; onPick: (p: string) => void }) {
  const { data } = useQuery({
    queryKey: ["card-programs", card.id],
    queryFn: () => api<any>(`/cards/${card.id}/programs`),
    staleTime: 60_000,
  });
  const list: string[] = data?.programs ?? [];
  // Keep the configured name selectable even when the card is unreachable,
  // otherwise opening this page with a board powered off would silently offer
  // to clear it.
  const options = list.length ? list : (card.program ? [card.program] : []);
  return (
    <label className="block text-sm">
      <span className="tile-label">program</span>
      <select className="input mt-1" value={card.program ?? ""}
        onChange={e => onPick(e.target.value)}>
        <option value="">— none —</option>
        {options.map(n => <option key={n} value={n}>{n}</option>)}
      </select>
      {/* Only say something when it is not the obvious case: the card is
          unreachable, or it is already running something else. */}
      {data && !data.reachable ? (
        <p className="mt-1 text-xs text-muted">Card unreachable — showing the configured program.</p>
      ) : data?.running && data?.active && data.active !== card.program ? (
        <p className="mt-1 text-xs text-muted">Running {data.active} — changes on next boot.</p>
      ) : null}
    </label>
  );
}

/** What this card is actually served at boot.
 *
 * Rendered by the same code the TFTP server uses, not a second copy. Shown
 * because `mode`, `program` and `drive` are appended at serve time and do NOT
 * appear in the layout field you edit -- so the field alone does not tell you
 * what the card gets, and guessing at that is how a board ends up stranded in
 * a mode nobody chose.
 */
function BootConfig({ cardId }: { cardId: number }) {
  const { data } = useQuery({
    queryKey: ["bootconfig", cardId],
    queryFn: () => api<any>(`/cards/${cardId}/bootconfig`),
  });
  if (!data) return null;
  return (
    <div className="space-y-1">
      <span className="tile-label inline-flex items-center gap-1">
        <FileCode2 size={11} aria-hidden /> served at boot
        {data.filename && <span className="text-muted">— {data.filename}</span>}
      </span>
      <pre className="overflow-x-auto rounded border border-line bg-black/20 p-2 text-[10px] leading-snug">{data.config}</pre>
      {!data.served && (
        <p className="text-xs text-warn">
          No layout set, so nothing is served — the card falls back to whatever
          its firmware was built with.
        </p>
      )}
    </div>
  );
}

/** The PANELS plugged into this card, drawn to scale.
 *
 * A card is one receiver board; the modules hanging off its connectors are a
 * different thing and the UI showed neither their number nor their size. The
 * card said "128x128" and the two 128x64 modules making that up appeared
 * nowhere except as raw YAML inside the edit form -- so "is this one panel or
 * two, and which connector is the top one?" could not be answered by looking.
 *
 * Parsed from the same `layout_yaml` the card is served at boot, so this is
 * what the board will actually do, not a second copy that can drift.
 */
function PanelMap({ yaml, drive, order, moduleW, moduleH }:
  { yaml: string | null; drive?: string | null; order?: string | null; moduleW?: number; moduleH?: number }) {
  const [sel, setSel] = useState<string | null>(null);
  if (!yaml) {
    return <div className="tile-label">no connector layout — card drives one module</div>;
  }
  // Deliberately a few regexes and not a YAML parser: this is a flat file of
  // `key: value` lines written by us, and pulling a parser into the bundle to
  // read six keys would cost more than it explains.
  const num = (k: string) => {
    const m = yaml.match(new RegExp(`^\\s*${k}\\s*:\\s*(\\d+)`, "m"));
    return m ? Number(m[1]) : null;
  };
  const gm = yaml.match(/^\s*grid\s*:\s*(\d+)\s*x\s*(\d+)/m);
  const cols = gm ? Number(gm[1]) : 1;
  const rows = gm ? Number(gm[2]) : 1;
  // The panel type's module size wins over a number left in the YAML, which
  // is exactly how the firmware reads the served file (last key wins).
  const pw = moduleW ?? num("panel_width") ?? 0;
  const ph = moduleH ?? num("panel_height") ?? 0;

  // J1..J8 -> col,row. A connector with no entry simply has nothing plugged in.
  const at = new Map<string, string>();
  for (const m of yaml.matchAll(/^\s*(J\d)\s*:\s*(\d+)\s*,\s*(\d+)/gm)) {
    at.set(`${m[2]},${m[3]}`, m[1]);
  }
  if (at.size === 0) {
    return <div className="tile-label">no connector layout — card drives one module</div>;
  }

  // Scale the tallest dimension to a fixed box so a 1x2 stack and a 2x1 pair
  // are visibly different shapes rather than both being squares.
  const BOX = 78;
  const totalW = cols * pw, totalH = rows * ph;
  const scale = totalW && totalH ? BOX / Math.max(totalW, totalH) : 0;

  return (
    <div className="space-y-1">
      <div className="tile-label">
        {at.size} panel{at.size === 1 ? "" : "s"}
        {pw && ph ? <> · {pw}×{ph} each</> : null}
        {" · "}{[...at.values()].sort().join(" ")}
      </div>
      <div className="inline-grid gap-[2px] rounded border border-line p-[3px]"
        style={{ gridTemplateColumns: `repeat(${cols}, ${pw * scale}px)` }}
        role="img"
        aria-label={`${at.size} panels of ${pw} by ${ph}, arranged ${cols} by ${rows}`}>
        {Array.from({ length: rows * cols }, (_, i) => {
          const c = i % cols, r = Math.floor(i / cols);
          const j = at.get(`${c},${r}`);
          if (!j) {
            return <div key={i} style={{ height: ph * scale }}
              className="rounded-[2px] border border-dashed border-line/60" />;
          }
          return (
            <button key={i} type="button"
              style={{ height: ph * scale }}
              title={`${j} — ${pw}×${ph} at column ${c}, row ${r}`}
              aria-label={`${j}, ${pw} by ${ph}, column ${c} row ${r}`}
              onClick={() => setSel(v => v === j ? null : j)}
              className={`flex items-center justify-center rounded-[2px] border text-[9px] font-semibold transition ${
                sel === j ? "border-accent bg-accent/50 text-fg"
                          : "border-accent/50 bg-accent/25 hover:bg-accent/40"}`}>
              {j}
            </button>
          );
        })}
      </div>
      {sel && (() => {
        const e = [...at.entries()].find(([, v]) => v === sel);
        const [cc, rr] = (e ? e[0] : "0,0").split(",");
        return (
          <dl className="rounded border border-line p-2 text-xs">
            <div className="flex justify-between gap-2"><dt className="text-muted">connector</dt><dd>{sel}</dd></div>
            <div className="flex justify-between gap-2"><dt className="text-muted">module</dt><dd>{pw}×{ph}</dd></div>
            <div className="flex justify-between gap-2"><dt className="text-muted">position</dt><dd>col {cc}, row {rr}</dd></div>
            <div className="flex justify-between gap-2">
              <dt className="text-muted">driver chip</dt>
              <dd>{drive || "bitstream default"}</dd>
            </div>
            {/* The IC is physically on the MODULE, but it is set on the CARD,
                and that is not an oversight: the gateware shares ONE register
                table across all of a card's outputs, so a card cannot drive
                two chip types at once. Every module on a card must carry the
                same IC. Said here because this is where someone looks when
                they are about to mix modules. */}
            <p className="mt-1 text-[11px] leading-snug text-muted">
              set per card below — one register table is shared by every
              connector, so all modules on this card must carry the same IC.
            </p>
            <div className="flex justify-between gap-2">
              <dt className="text-muted">colour order</dt>
              <dd>{order || "RGB"}</dd>
            </div>
            {/* Also per card, and worth saying next to the per-module view so
                nobody expects to set it on one connector. */}
            <p className="mt-1 text-[11px] leading-snug text-muted">
              colour order is per card too — set it below, or try one live on
              the board with <span className="text-fg">/api/rgborder</span>.
            </p>
          </dl>
        );
      })()}
    </div>
  );
}

/* Where a card is cabled. Cards daisy-chain through their second RJ45 and a
   whole chain is ONE IP address, the head's: the card at hop h answers at
   <head>:8000+h and takes pixels on 7000+h. So "behind <head> at hop h" is
   what an operator knows, and the host follows from the head -- typing the
   head's IP into a member by hand is how the two drift apart. Renders plain
   form fields (host, chain_hop) so the surrounding FormData picks them up. */
function ChainPosition({ cards, self, host, hop, compact }:
    { cards: any[] | undefined; self?: number; host?: string; hop?: number; compact?: boolean }) {
  const heads = (cards ?? []).filter(c => !c.chain_hop && c.id !== self);
  const found = hop ? heads.find(c => c.host === host) : undefined;
  // A member whose head record is gone (deleted, or its host edited before
  // heads carried their chain along) must KEEP its position: falling back to
  // "on the switch" would silently re-save it as a hop-0 card on the head's IP.
  const orphan = !!hop && !found;
  const [head, setHead] = useState<string>(found ? String(found.id) : orphan ? "orphan" : "");
  // A string while typing: coercing on every keystroke snapped "" to 1, so
  // typing "3" into a cleared field gave "13".
  const [h, setH] = useState<string>(String(hop || 1));
  const [ip, setIp] = useState<string>(hop ? "" : (host ?? ""));
  const hc = heads.find(c => String(c.id) === head);
  const hopN = Math.max(1, Math.min(63, Number(h) || 1));
  const label = compact ? "block text-xs" : "text-sm";
  const chained = !!hc || head === "orphan";
  const chainHost = hc ? hc.host : host;
  return (
    <>
      <label className={label} title="A card on the switch has its own IP. A card cabled into another card's second port has none: it is reached through that card.">
        connected
        <select className="input" value={head} onChange={e => setHead(e.target.value)}>
          <option value="">on the switch (own IP)</option>
          {heads.map(c => <option key={c.id} value={String(c.id)}>behind {c.name}</option>)}
          {orphan && <option value="orphan">behind {host} (no card record for that head)</option>}
        </select>
      </label>
      {chained ? (
        <label className={label} title="1 = cabled straight into the head; 2 = the card after that; ...">
          hop
          <input name="chain_hop" type="number" min={1} max={63} required className="input"
            value={h} onChange={e => setH(e.target.value)} />
          <input type="hidden" name="host" value={chainHost} />
          <span className={head === "orphan" ? "text-xs text-warn" : "text-xs text-muted"}>
            {head === "orphan" ? "its head is not registered -- add it, or pick another. " : ""}
            reached at {chainHost}:{8000 + hopN}, pixels on :{7000 + hopN}</span>
        </label>
      ) : (
        <label className={label}>host / ip
          <input name="host" className="input" required value={ip} onChange={e => setIp(e.target.value)} />
          <input type="hidden" name="chain_hop" value={0} />
        </label>
      )}
    </>
  );
}

/* What is actually cabled behind a head, asked of the hardware (the API walks
   <head>:8001, :8002 ... from the sender, since the panel bus is private).
   Matches each answering card to its record by MAC, and offers to register
   the ones marquee does not know yet. */
function ChainPanel({ card, cards, onAdd, addError }:
    { card: any; cards: any[] | undefined; onAdd: (body: any, done: () => void) => void; addError?: unknown }) {
  const [on, setOn] = useState(false);
  const { data, isFetching, refetch, error } = useQuery({
    queryKey: ["chain-scan", card.id], enabled: on,
    queryFn: () => api<any>(`/cards/${card.id}/chain/scan`),
    // A scan walks the hardware hop by hop; it runs when asked, not on every
    // window focus.
    refetchOnWindowFocus: false, staleTime: Infinity,
  });
  const members = (cards ?? []).filter(c => c.chain_hop && c.host === card.host)
    .sort((a, b) => a.chain_hop - b.chain_hop);
  return (
    <div className="rounded border border-line p-2 text-xs space-y-1">
      <div className="flex items-center justify-between gap-2">
        <span className="tile-label">
          chain{members.length ? ` · ${members.length} behind: ${members.map(m => m.name).join(" → ")}` : " · none registered"}
        </span>
        {/* Enabling the query runs the first scan; refetch() as well ran it twice. */}
        <button className="btn text-xs" onClick={() => { on ? refetch() : setOn(true); }}>
          {isFetching ? "scanning…" : "scan"}</button>
      </div>
      {error && <p className="text-bad">{String(error)}</p>}
      {addError ? <p className="text-bad">could not add: {String(addError)}</p> : null}
      {data && !data.reachable && <p className="text-bad">{card.name} did not answer.</p>}
      {data && data.reachable && !data.chain_supported &&
        <p className="text-warn">firmware has no chain support -- update the bitstream and firmware.</p>}
      {data?.head_status && (
        <p className="text-muted">downstream port: {data.head_status.port1_link
          ? `link ${data.head_status.port1_speed} Mb/s` : "no link"} · {data.head_status.fwd_frames == null
          // null = the gateware has no counters (the four-output E320 build);
          // a 0 there would read as "nothing is flowing" on a working chain.
          ? "frames not counted by this gateware"
          : <>{data.head_status.fwd_frames} frames down,{" "}{data.head_status.up_frames ?? 0} up{data.head_status.up_dropped ? `, ${data.head_status.up_dropped} up dropped` : ""}</>}</p>
      )}
      {data?.hops?.length > 0 && (
        <table className="w-full">
          <thead><tr className="text-muted text-left"><th>hop</th><th>card</th><th>size</th><th>fw</th><th></th></tr></thead>
          <tbody>
            {data.hops.map((h: any) => (
              <tr key={h.hop}>
                <td>{h.hop}</td>
                <td>{h.answered ? (h.registered?.name ?? <span className="text-warn">{h.mac}</span>) : <span className="text-bad">no answer</span>}
                  {h.mismatch && <span className="text-bad"> (record says {h.registered_here?.name})</span>}</td>
                <td>{h.answered ? `${h.width}×${h.height}` : ""}</td>
                <td>{h.version ?? ""}</td>
                <td>{h.answered && !h.registered && (
                  <button className="btn text-xs" onClick={() => onAdd({
                    // A new member starts as a copy of its head's module and
                    // pacing -- same hardware, usually -- placed to the head's
                    // right. Adjust x/y on the card afterwards.
                    name: `${card.name}-hop${h.hop}`, host: card.host, chain_hop: h.hop,
                    mac: h.mac, wall_id: card.wall_id, width: h.width || card.width,
                    height: h.height || card.height,
                    x: card.x + card.width * h.hop, y: card.y,
                    panel_type: card.panel_type, layout_yaml: card.layout_yaml,
                    packet_delay: card.packet_delay, rotate: card.rotate ?? 0,
                    drive: card.drive, rgb_order: card.rgb_order },
                    () => refetch())}>add</button>)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {data && data.chain_supported && !data.hops?.length && <p className="text-muted">nothing cabled behind it.</p>}
      {data?.missing?.length > 0 && (
        <p className="text-bad">registered but not answering: {data.missing.map((m: any) => `${m.name} (hop ${m.hop})`).join(", ")}</p>
      )}
    </div>
  );
}

export default function Cards() {
  const qc = useQueryClient();
  const { data: cards, isLoading } = useQuery({ queryKey: ["cards"], queryFn: () => api<any[]>("/cards") });
  const { data: walls } = useQuery({ queryKey: ["walls"], queryFn: () => api<any[]>("/walls") });
  const { data: drives } = useQuery({ queryKey: ["drive-types"],
    queryFn: () => api<any>("/drive-types"), staleTime: Infinity });
  const { data: orders } = useQuery({ queryKey: ["rgb-orders"],
    queryFn: () => api<any>("/rgb-orders"), staleTime: Infinity });
  // The module types marquee knows (geometry, group arrangement, driver chip,
  // bitstream preset) -- one table, served into the boot config, so the YAML
  // below only has to place connectors.
  const { data: ptypes } = useQuery({ queryKey: ["panel-types"],
    queryFn: () => api<any>("/panel-types"), staleTime: Infinity });
  const ptype = (key?: string | null) =>
    (ptypes?.panel_types ?? []).find((t: any) => t.key === key);
  const [adding, setAdding] = useState(false);

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["bootconfig"] });
    qc.invalidateQueries({ queryKey: ["cards"] });
    qc.invalidateQueries({ queryKey: ["walls"] });
    qc.invalidateQueries({ queryKey: ["wall-layout"] });
  };
  // Mode is persisted here rather than only toggled on the card, because the
  // board reads it from its TFTP config at boot -- a runtime-only switch did not
  // survive a power cycle. The player also stops streaming at a program-mode
  // card, so this is what stops the two fighting over the framebuffer.
  const patch = useMutation({
    mutationFn: ({ cid, body }: any) =>
      api(`/cards/${cid}`, { method: "PATCH", body: JSON.stringify(body) }),
    onSuccess: invalidate,
  });
  const add = useMutation({
    mutationFn: (body: any) => api("/cards", { method: "POST", body: JSON.stringify(body) }),
    onSuccess: () => { invalidate(); setAdding(false); },
  });
  const remove = useMutation({
    mutationFn: (cid: number) => api(`/cards/${cid}`, { method: "DELETE" }),
    onSuccess: invalidate,
  });
  // Which card is open for editing. Position and size have to be changeable
  // after the fact: a wall is laid out by trying coordinates and looking at the
  // map, and a typo in x that can only be fixed with curl is not a UI.
  const [editing, setEditing] = useState<number | null>(null);

  const wallName = (id: number) => walls?.find(w => w.id === id)?.name ?? `wall ${id}`;

  return (
    <div className="space-y-4">
      <PageHeader title="Cards" icon={CircuitBoard}
        actions={<button className="btn-primary inline-flex items-center gap-1" onClick={() => setAdding(a => !a)}>
          <Plus size={15} aria-hidden /> add card</button>}>
        every receiver board in the fleet, which wall it drives part of, and how it is performing.
      </PageHeader>

      {adding && (
        <form className="card grid grid-cols-2 gap-3 md:grid-cols-4"
          onSubmit={e => { e.preventDefault();
            const f = new FormData(e.currentTarget as HTMLFormElement);
            add.mutate({ name: f.get("name"), host: f.get("host"), mac: f.get("mac") || null,
                         chain_hop: Number(f.get("chain_hop") || 0),
                         wall_id: Number(f.get("wall_id")),
                         width: Number(f.get("width")), height: Number(f.get("height")),
                         x: Number(f.get("x")), y: Number(f.get("y")) }); }}>
          <label className="text-sm">name<input name="name" className="input" required /></label>
          <ChainPosition cards={cards} />
          <label className="text-sm">mac (for tftp)<input name="mac" className="input" placeholder="02:78:…" /></label>
          <label className="text-sm">wall
            <select name="wall_id" className="input" required>
              {walls?.map(w => <option key={w.id} value={w.id}>{w.name}</option>)}
            </select>
          </label>
          <div className="grid grid-cols-2 gap-2">
            <label className="text-sm">w<input name="width" className="input" defaultValue={256} /></label>
            <label className="text-sm">h<input name="height" className="input" defaultValue={128} /></label>
          </div>
          <div className="grid grid-cols-2 gap-2">
            <label className="text-sm">x<input name="x" className="input" defaultValue={0} /></label>
            <label className="text-sm">y<input name="y" className="input" defaultValue={0} /></label>
          </div>
          <div className="col-span-full flex gap-2">
            <button className="btn-primary" type="submit">create</button>
            <button className="btn" type="button" onClick={() => setAdding(false)}>cancel</button>
          </div>
          <p className="col-span-full text-xs text-muted">
            x and y place this card's top-left corner within its wall. A wall driven by a
            single card is 0, 0.
          </p>
          {add.error && <p className="col-span-full text-sm text-bad">{String(add.error)}</p>}
        </form>
      )}

      {isLoading && <div className="text-muted">loading…</div>}
      {!isLoading && !walls?.length && <Empty>Create a wall first — a card has to belong to one.</Empty>}
      {!isLoading && !!walls?.length && !cards?.length && <Empty>No cards yet. Add the first one above.</Empty>}

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
        {cards?.map(p => (
          <div key={p.id} className="card space-y-2">
            <div className="flex items-center justify-between">
              <div className="font-semibold">{p.name}</div>
              <StatusChip status={p.state === "online" ? "good" : p.state === "offline" ? "bad" : "warn"}
                label={p.state} />
            </div>
            <div className="flex items-start justify-between gap-2">
              <div className="tile-label">{p.chain_hop
                ? `hop ${p.chain_hop} behind ${cards?.find(c => !c.chain_hop && c.host === p.host)?.name ?? p.host} · ${p.host}:${8000 + p.chain_hop}`
                : p.host} · {p.width}×{p.height}</div>
              <button className="shrink-0 text-muted hover:text-accent transition"
                title={editing === p.id ? "close" : "edit placement"}
                aria-label={`Edit placement for ${p.name}`}
                onClick={() => setEditing(e => e === p.id ? null : p.id)}>
                <Pencil size={13} aria-hidden />
              </button>
            </div>
            {/* Where this board sits in the picture. Shown on the device card
                because "which one is the left half?" is the first question
                anyone asks when a multi-card wall looks wrong. */}
            <div className="tile-label">{wallName(p.wall_id)} @ ({p.x}, {p.y})</div>
            {!p.chain_hop && <ChainPanel card={p} cards={cards}
              // Only the panel whose add failed shows the error.
              addError={add.variables?.chain_hop && add.variables?.host === p.host ? add.error : null}
              onAdd={(body, done) => add.mutate(body, { onSuccess: done })} />}
            {/* The modules on this board's connectors. Always visible: it was
                previously only readable as YAML inside the edit form, so the
                page could not answer "one panel or two?". */}
            <PanelMap yaml={p.layout_yaml ?? null} drive={p.drive || ptype(p.panel_type)?.drive}
              order={p.rgb_order} moduleW={ptype(p.panel_type)?.module_w} moduleH={ptype(p.panel_type)?.module_h} />
            {editing === p.id && (
              <form className="space-y-2 rounded border border-line p-2"
                onSubmit={e => { e.preventDefault();
                  const f = new FormData(e.currentTarget as HTMLFormElement);
                  patch.mutate({ cid: p.id, body: {
                    host: f.get("host"), wall_id: Number(f.get("wall_id")),
                    chain_hop: Number(f.get("chain_hop") || 0),
                    width: Number(f.get("width")), height: Number(f.get("height")),
                    x: Number(f.get("x")), y: Number(f.get("y")),
                    packet_delay: Number(f.get("packet_delay")),
                    rotate: Number(f.get("rotate")),
                    layout_yaml: f.get("layout_yaml") || null,
                  }});
                  setEditing(null); }}>
                <ChainPosition cards={cards} self={p.id} host={p.host} hop={p.chain_hop} compact />
                <label className="block text-xs">wall
                  <select name="wall_id" className="input" defaultValue={p.wall_id}>
                    {walls?.map(w => <option key={w.id} value={w.id}>{w.name}</option>)}
                  </select></label>
                <div className="grid grid-cols-4 gap-1">
                  <label className="text-xs">x<input name="x" className="input" defaultValue={p.x} /></label>
                  <label className="text-xs">y<input name="y" className="input" defaultValue={p.y} /></label>
                  <label className="text-xs">w<input name="width" className="input" defaultValue={p.width} /></label>
                  <label className="text-xs">h<input name="height" className="input" defaultValue={p.height} /></label>
                </div>
                <label className="block text-xs">packet delay (s)
                  <input name="packet_delay" className="input" defaultValue={p.packet_delay} /></label>
                {/* How the modules hang, not how the card scans them: w and h
                    above stay the framebuffer's, and a quarter turn swaps the
                    card's shape on the wall. Streamed content only. */}
                <label className="block text-xs">mounted turned (clockwise)
                  <select name="rotate" className="input" defaultValue={p.rotate ?? 0}
                          title="How the modules are mounted, clockwise from the card's framebuffer. Marquee turns each streamed frame to match; the card turns its own programs and boot screen (card firmware 5.7.0+, at its next boot).">
                    <option value={0}>0° — as scanned</option>
                    <option value={90}>90°</option>
                    <option value={180}>180°</option>
                    <option value={270}>270°</option>
                  </select></label>
                {/* The connector map. `grid` is the arrangement of modules on
                    THIS card and `J1:`..`J8:` place each connector in it as
                    col,row -- so J1: 0,0 means connector J1 drives the
                    top-left module. Served to the card at boot.
                    mode/program/drive are appended at serve time and are set
                    by the controls above, not here. */}
                <label className="block text-xs">connector layout (yaml)
                  <textarea name="layout_yaml" rows={7} spellCheck={false}
                    className="input font-mono text-[10px] leading-snug"
                    defaultValue={p.layout_yaml ?? ""}
                    placeholder={"grid: 1x2\npanel_width: 128\npanel_height: 64\nJ1: 0,0\nJ2: 0,1"} /></label>
                <p className="text-xs text-muted">
                  <span className="text-fg">grid</span> is this card's module arrangement;
                  <span className="text-fg"> J1</span>…<span className="text-fg">J8</span> place each
                  connector in it as col,row. Takes effect when the card next boots.
                </p>
                <div className="flex items-center justify-between gap-2">
                  <div className="flex gap-2">
                    <button className="btn-primary" type="submit">save</button>
                    <button className="btn" type="button" onClick={() => setEditing(null)}>cancel</button>
                  </div>
                  {/* Removing the record does not touch the board. Said plainly
                      because "delete card" reads like it might. */}
                  <button className="btn text-bad" type="button"
                    title="Remove this card's record. The board itself is untouched."
                    onClick={() => { if (confirm(`Remove ${p.name} from marquee? The board itself is not changed.`)) { remove.mutate(p.id); setEditing(null); } }}>
                    <Trash2 size={13} aria-hidden />
                  </button>
                </div>
              </form>
            )}
            {/* Which MODULE type is on this board's connectors.
              *
              * The one place a module's facts live: its size, how its RGB
              * groups are arranged, which driver chip it carries, which
              * bitstream preset drives it. Choosing it here fills those into
              * the boot config from marquee's table, so the YAML below only
              * has to say which connector goes where -- and the same module
              * never has its numbers typed twice. The authority on each type
              * is docs/PANELS.md in the colorlight repo. */}
            <label className="block text-sm">
              <span className="tile-label">module type</span>
              <select className="input mt-1" value={p.panel_type ?? ""}
                onChange={e => patch.mutate({ cid: p.id, body: { panel_type: e.target.value || null } })}>
                <option value="">— from the layout yaml —</option>
                {(ptypes?.panel_types ?? []).map((t: any) =>
                  <option key={t.key} value={t.key}>{t.label}</option>)}
              </select>
              {ptype(p.panel_type) && (
                <span className="tile-label block mt-1">
                  {ptype(p.panel_type).module_w}×{ptype(p.panel_type).module_h}
                  {ptype(p.panel_type).drive ? ` · ${ptype(p.panel_type).drive}` : ""}
                  {" · bitstream "}{ptype(p.panel_type).bitstream_preset}
                </span>
              )}
            </label>
            {/* Which driver chip the modules carry.
              *
              * NOT a switch between HUB75 and S-PWM -- that is a different
              * bitstream, not a setting. This picks which S-PWM chip an S-PWM
              * card is talking to, and is served in the boot config so it
              * survives a reboot, exactly like mode and program.
              *
              * A card whose bitstream has no chip table ignores it. That is
              * why the default is "bitstream default" rather than a chip: it
              * lights, rather than showing nothing while someone works out
              * why. */}
            <label className="block text-sm">
              <span className="tile-label">driver chip</span>
              <select className="input mt-1" value={p.drive ?? ""}
                onChange={e => patch.mutate({ cid: p.id, body: { drive: e.target.value } })}>
                {(drives?.drive_types ?? [""]).map((d: string) =>
                  <option key={d} value={d}>{d || "— bitstream default —"}</option>)}
              </select>
            </label>
            {/* Which logical colour each pin group carries.
              *
              * The panel's own wiring decides it, and a wrong value shows as
              * wrong HUES with every counter clean -- nothing in the stats to
              * find. Changing it here records the answer in the boot config so
              * it survives a reboot; the card also takes it live on
              * /api/rgborder, which is how you find the answer in the first
              * place, glancing at the panel between tries.
              *
              * Per card, like the driver chip and for the same reason: the
              * outputs run in lockstep off one engine. */}
            <label className="block text-sm">
              <span className="tile-label">colour order</span>
              <select className="input mt-1" value={p.rgb_order || "RGB"}
                onChange={e => patch.mutate({ cid: p.id, body: { rgb_order: e.target.value } })}>
                {(orders?.rgb_orders ?? ["RGB"]).map((o: string) =>
                  <option key={o} value={o}>{o}{o === "RGB" ? " — standard" : ""}</option>)}
              </select>
            </label>
            <label className="block text-sm">
              <span className="tile-label">mode</span>
              {/* Two words, because it is a two-way switch. The stored values
                  stay "stream"/"program"; "Network" is the honest label for the
                  card end of it -- what arrives over the wire is a picture,
                  whoever rendered it. */}
              <select className="input mt-1" value={p.mode ?? "stream"}
                onChange={e => patch.mutate({ cid: p.id, body: { mode: e.target.value } })}>
                <option value="stream">Network</option>
                <option value="program">Program</option>
              </select>
            </label>
            {/* Content is assigned to the WALL, so a streaming card shows what
                its wall is playing and links there rather than offering a
                second, conflicting place to set it. */}
            {(p.mode ?? "stream") !== "program" ? (
              <p className="text-xs text-muted">
                Showing part of <span className="text-fg">{wallName(p.wall_id)}</span> — content is
                set on the Walls page.
              </p>
            ) : (
              <ProgramPicker card={p} onPick={(program: string) =>
                patch.mutate({ cid: p.id, body: { program } })} />
            )}
            {editing === p.id && <BootConfig cardId={p.id} />}
            <div className="flex items-center justify-between gap-2 text-xs text-muted">
              <span>last seen {p.last_seen ? new Date(p.last_seen).toLocaleTimeString() : "—"}</span>
              {/* The board's own status page, served by the firmware on the card
                  itself. The card is on the private panel bus, so it is reached
                  THROUGH the API (a relay on the sender), never by p.host. */}
              <a href={`${BASE}/api/v1/cards/${p.id}/controller/`} target="_blank" rel="noreferrer"
                 title={`Open the board controller on ${p.host}`}
                 aria-label={`Open the board controller for ${p.name} at ${p.host} in a new tab`}
                 className="inline-flex shrink-0 items-center gap-1 hover:text-accent transition">
                <ExternalLink size={12} aria-hidden /> controller
              </a>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
