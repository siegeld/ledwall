import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { LayoutGrid, Plus, TriangleAlert } from "lucide-react";
import { useRef, useState } from "react";
import { api } from "../lib/api";
import { PageHeader, StatusChip, Empty } from "../components/widgets";

/** The wall, drawn to scale, with the cards draggable into place.
 *
 * Worth the pixels and the pointer handling both. The failure modes of a
 * multi-card wall are geometric -- a gap shows on the glass as a black band and
 * an overlap as two boards rendering the same content -- and neither reads as a
 * coordinate error when you are standing in front of it. Seeing the rectangles
 * makes both obvious; dragging them is how you fix it, because laying out a
 * wall is a spatial task and typing `x: 384` is not.
 *
 * Positions snap to the smallest card on the wall, so modules of one size tile
 * exactly with no arithmetic. Precise values are still editable on the Cards
 * page for the cases where a wall is deliberately irregular.
 */
/** Where the MODULE seams fall inside a card's rectangle.
 *
 * A card rectangle is not one screen: it is however many modules hang off that
 * board's connectors. Drawing it undivided makes a two-module card look like a
 * single 128x128 panel, which is the first thing anyone queries when the wall
 * map does not match the thing on the wall. Returns the seam offsets in WALL
 * pixels, or [] when the card has no connector layout to speak of.
 */
function seamsOf(c: any): { xs: number[]; ys: number[] } {
  const y = c?.layout_yaml;
  if (!y) return { xs: [], ys: [] };
  const g = y.match(/^\s*grid\s*:\s*(\d+)\s*x\s*(\d+)/m);
  if (!g) return { xs: [], ys: [] };
  const cols = Number(g[1]), rows = Number(g[2]);
  const pw = Number((y.match(/^\s*panel_width\s*:\s*(\d+)/m) || [])[1]) || 0;
  const ph = Number((y.match(/^\s*panel_height\s*:\s*(\d+)/m) || [])[1]) || 0;
  const xs: number[] = [], ys: number[] = [];
  for (let i = 1; i < cols; i++) if (pw) xs.push(i * pw);
  for (let i = 1; i < rows; i++) if (ph) ys.push(i * ph);
  return { xs, ys };
}

/** What this wall shows — both kinds of content in one control.
 *
 * Content used to mean "a show", with programs set per card on another page.
 * That split was an implementation detail leaking into the UI: both answer
 * "what is on this wall?". Choosing content IS the mode decision, so mode is
 * no longer a separate thing to set — it follows from what you pick.
 *
 * The two are NOT the same operation and the option labels say so. A show is
 * rendered once and each card gets its crop, so N cards show one picture. A
 * program runs on each card from that card's own width and height — the
 * firmware's program context has no wall size and no origin, so it cannot know
 * it is part of something bigger — and N cards run N independent copies.
 */
function WallContent({ wall, cards }: { wall: any; cards: any[] }) {
  const qc = useQueryClient();
  const { data } = useQuery({
    queryKey: ["wall-content", wall.id],
    queryFn: () => api<any>(`/walls/${wall.id}/content`),
  });
  const set = useMutation({
    mutationFn: (body: any) =>
      api(`/walls/${wall.id}/content`, { method: "PUT", body: JSON.stringify(body) }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["walls"] });
      qc.invalidateQueries({ queryKey: ["cards"] });
      qc.invalidateQueries({ queryKey: ["wall-layout", wall.id] });
      qc.invalidateQueries({ queryKey: ["wall-content", wall.id] });
    },
  });

  const modes = new Set(cards.map(c => c.mode || "stream"));
  const allProgram = modes.size === 1 && [...modes][0] === "program";
  const progs = new Set(cards.map(c => c.program || ""));
  const current = allProgram && progs.size === 1
    ? `program:${[...progs][0]}`
    : (wall.show_id ? `show:${wall.show_id}` : "");
  const odd = cards.filter(c => (c.mode || "stream") === "program");
  const n = data?.cards ?? cards.length;

  return (
    <div className="space-y-1">
      <label className="block text-sm">
        <span className="tile-label">content</span>
        <select className="input mt-1" value={current} disabled={set.isPending}
          onChange={e => {
            const [kind, rest] = e.target.value.split(/:(.*)/);
            if (!kind) return;
            set.mutate(kind === "show"
              ? { kind: "show", show_id: Number(rest) }
              : { kind: "program", name: rest });
          }}>
          <option value="">— none —</option>
          <optgroup label="Streamed — one picture across the whole wall">
            {data?.shows?.map((s: any) =>
              <option key={`s${s.id}`} value={`show:${s.id}`}>{s.name}</option>)}
          </optgroup>
          {data?.programs?.length > 0 && (
            <optgroup label={n > 1
              ? `On-board programs — runs on each card, ${n} copies`
              : "On-board programs — runs on the card itself"}>
              {data.programs.map((p: any) =>
                <option key={`p${p.name}`} value={`program:${p.name}`}>{p.name}</option>)}
            </optgroup>
          )}
        </select>
      </label>
      {allProgram && n > 1 && (
        <p className="text-xs text-muted">
          Each card draws its own copy — this is {n} independent displays, not one picture.
        </p>
      )}
      {modes.size > 1 && (
        <p className="text-xs text-warn">
          {odd.map(c => c.name).join(", ")} still run{odd.length === 1 ? "s" : ""} an
          on-board program and will not show this content.
        </p>
      )}
      {data?.unreachable?.length > 0 && (
        <p className="text-xs text-warn">
          Could not ask {data.unreachable.join(", ")} which programs it carries —
          the list may be short.
        </p>
      )}
      {set.error && <p className="text-xs text-bad">{String(set.error)}</p>}
    </div>
  );
}

function LayoutMap({ wallId }: { wallId: number }) {
  const qc = useQueryClient();
  const { data } = useQuery({
    queryKey: ["wall-layout", wallId],
    queryFn: () => api<any>(`/walls/${wallId}/layout`),
  });
  const move = useMutation({
    mutationFn: ({ cid, x, y }: any) =>
      api(`/cards/${cid}`, { method: "PATCH", body: JSON.stringify({ x, y }) }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["wall-layout", wallId] });
      qc.invalidateQueries({ queryKey: ["cards"] });
      qc.invalidateQueries({ queryKey: ["walls"] });
    },
  });

  // Where a card is being dragged to, in WALL pixels.
  //
  // The REF is the source of truth and the state exists only to re-render.
  // Gating the move handler on the state instead was a real bug: pointerdown,
  // pointermove and pointerup can all arrive before React has processed the
  // setState from the first of them, so the guard `drag?.id !== c.id` saw a
  // stale null, every move returned early, and the drop compared the origin
  // against itself and wrote nothing. A slow human drag hid it; a fast one did
  // not. Refs update synchronously, so they cannot be stale.
  const [drag, setDrag] = useState<{ id: number; x: number; y: number } | null>(null);
  const origin = useRef<{ id: number; px: number; py: number; x: number; y: number } | null>(null);
  const pos = useRef<{ x: number; y: number } | null>(null);

  if (!data) return null;
  const { wall, cards, problems } = data;
  // Fixed drawing width; height follows the wall's aspect so the picture is
  // never distorted -- a squashed map would hide exactly the shape errors this
  // exists to show.
  const W = 260;
  const scale = W / Math.max(1, wall.width);
  const H = Math.max(24, Math.round(wall.height * scale));

  // Snap each axis to the smallest card's extent on THAT axis, so a wall of
  // identical modules tiles exactly with no arithmetic. Using one grid for both
  // would snap a 256x128 module's x to 128 and allow half-module offsets, which
  // is precisely the misalignment this is meant to prevent.
  const snapOf = (vals: number[]) => (vals.length ? Math.max(8, Math.min(...vals)) : 8);
  const gridX = snapOf(cards.map((c: any) => c.width));
  const gridY = snapOf(cards.map((c: any) => c.height));

  const clamp = (v: number, hi: number) => Math.max(0, Math.min(v, hi));

  const onDown = (e: React.PointerEvent, c: any) => {
    // Capture is an optimisation, not a requirement: it keeps the moves coming
    // when the pointer leaves the rectangle mid-drag. It can throw for a
    // pointer id the browser does not recognise, and a throw here would abort
    // the whole gesture before the drag state was ever set, so a failure to
    // capture must not be a failure to drag.
    try {
      (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
    } catch { /* drag still works, just without capture */ }
    origin.current = { id: c.id, px: e.clientX, py: e.clientY, x: c.box[0], y: c.box[1] };
    pos.current = { x: c.box[0], y: c.box[1] };
    setDrag({ id: c.id, x: c.box[0], y: c.box[1] });
  };
  const onMove = (e: React.PointerEvent, c: any) => {
    const o = origin.current;
    if (!o || o.id !== c.id) return;
    const snap = (v: number, g: number) => Math.round(v / g) * g;
    const next = {
      x: clamp(snap(o.x + (e.clientX - o.px) / scale, gridX),
               Math.max(0, wall.width - c.width)),
      y: clamp(snap(o.y + (e.clientY - o.py) / scale, gridY),
               Math.max(0, wall.height - c.height)),
    };
    pos.current = next;
    setDrag({ id: c.id, ...next });
  };
  const onUp = (c: any) => {
    const o = origin.current, p = pos.current;
    origin.current = null;
    pos.current = null;
    setDrag(null);
    if (o && p && o.id === c.id && (p.x !== o.x || p.y !== o.y)) {
      move.mutate({ cid: c.id, x: p.x, y: p.y });
    }
  };
  // Arrow keys nudge by one grid step, so the layout is reachable without a
  // pointer at all.
  const onKey = (e: React.KeyboardEvent, c: any) => {
    const d: Record<string, [number, number]> = {
      ArrowLeft: [-gridX, 0], ArrowRight: [gridX, 0],
      ArrowUp: [0, -gridY], ArrowDown: [0, gridY],
    };
    const step = d[e.key];
    if (!step) return;
    e.preventDefault();
    move.mutate({
      cid: c.id,
      x: clamp(c.box[0] + step[0], Math.max(0, wall.width - c.width)),
      y: clamp(c.box[1] + step[1], Math.max(0, wall.height - c.height)),
    });
  };

  return (
    <div className="space-y-2">
      <div className="relative select-none rounded border border-line bg-black/20"
           style={{ width: W, height: H }}>
        {cards.map((c: any) => {
          const d = drag?.id === c.id ? drag : null;
          const x = d ? d.x : c.box[0];
          const y = d ? d.y : c.box[1];
          return (
            <div key={c.id}
              role="button" tabIndex={0}
              aria-label={`${c.name} at ${x}, ${y}. Drag or use arrow keys to move.`}
              title={`${c.name} — ${c.host} — ${c.width}×${c.height} at (${x}, ${y})`
                + (() => { const s = seamsOf(c);
                     const n = (s.xs.length + 1) * (s.ys.length + 1);
                     return n > 1 ? `\n${n} modules on this card` : ""; })()
                + `\nDrag to move; arrow keys nudge`}
              onPointerDown={e => onDown(e, c)}
              onPointerMove={e => onMove(e, c)}
              onPointerUp={() => onUp(c)}
              onPointerCancel={() => onUp(c)}
              onKeyDown={e => onKey(e, c)}
              className={`absolute flex cursor-grab items-center justify-center overflow-hidden border text-[10px] touch-none focus:outline-none focus:ring-1 focus:ring-accent ${
                d ? "z-10 cursor-grabbing border-accent bg-accent/30 text-fg shadow-lg"
                  : c.enabled ? "border-accent/60 bg-accent/15 text-fg"
                              : "border-line bg-transparent text-muted"}`}
              style={{ left: x * scale, top: y * scale,
                       width: c.width * scale, height: c.height * scale }}>
              {/* Module seams. Pointer-events off so they never eat a drag. */}
              {(() => { const s = seamsOf(c); return (<>
                {s.xs.map(px => <span key={`x${px}`} aria-hidden
                  className="pointer-events-none absolute inset-y-0 border-l border-dashed border-accent/40"
                  style={{ left: px * scale }} />)}
                {s.ys.map(py => <span key={`y${py}`} aria-hidden
                  className="pointer-events-none absolute inset-x-0 border-t border-dashed border-accent/40"
                  style={{ top: py * scale }} />)}
              </>); })()}
              <span className="pointer-events-none relative">{c.name}</span>
            </div>
          );
        })}
      </div>
      <p className="text-xs text-muted">
        drag to arrange · snaps to {gridX}×{gridY} · arrow keys nudge
        {move.isPending && <span className="text-accent"> · saving…</span>}
      </p>
      {problems?.length > 0 && (
        <ul className="space-y-0.5 text-xs text-warn">
          {problems.map((p: any, i: number) => (
            <li key={i} className="flex items-start gap-1">
              <TriangleAlert size={12} className="mt-0.5 shrink-0" aria-hidden />
              <span>{p.kind}{p.card ? `: ${p.card}` : ""}{p.other ? ` overlaps ${p.other}` : ""}
                {p.detail ? ` — ${p.detail}` : ""}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}


export default function Walls() {
  const qc = useQueryClient();
  const { data: walls, isLoading } = useQuery({ queryKey: ["walls"], queryFn: () => api<any[]>("/walls") });
  const { data: cards } = useQuery({ queryKey: ["cards"], queryFn: () => api<any[]>("/cards") });
  const [adding, setAdding] = useState(false);

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["walls"] });
    qc.invalidateQueries({ queryKey: ["wall-layout"] });
  };
  // Content and mode are set together, by WallContent, through
  // PUT /walls/{id}/content -- choosing what a wall shows IS the mode
  // decision. The old per-wall `assign` and `setMode` verbs are gone from this
  // page; their endpoints remain for API callers.
  const add = useMutation({
    mutationFn: (body: any) => api("/walls", { method: "POST", body: JSON.stringify(body) }),
    onSuccess: () => { invalidate(); setAdding(false); },
  });

  return (
    <div className="space-y-4">
      <PageHeader title="Walls" icon={LayoutGrid}
        actions={<button className="btn-primary inline-flex items-center gap-1" onClick={() => setAdding(a => !a)}>
          <Plus size={15} aria-hidden /> add wall</button>}>
        a wall is what a viewer sees — one picture, however many receiver cards it takes to drive it.
      </PageHeader>

      {adding && (
        <form className="card grid grid-cols-2 gap-3 md:grid-cols-4"
          onSubmit={e => { e.preventDefault();
            const f = new FormData(e.currentTarget as HTMLFormElement);
            add.mutate({ name: f.get("name"), width: Number(f.get("width")),
                         height: Number(f.get("height")) }); }}>
          <label className="text-sm">name<input name="name" className="input" required /></label>
          <div className="grid grid-cols-2 gap-2">
            <label className="text-sm">width<input name="width" className="input" defaultValue={256} /></label>
            <label className="text-sm">height<input name="height" className="input" defaultValue={128} /></label>
          </div>
          <div className="col-span-full flex gap-2">
            <button className="btn-primary" type="submit">create</button>
            <button className="btn" type="button" onClick={() => setAdding(false)}>cancel</button>
          </div>
          <p className="col-span-full text-xs text-muted">
            Size the wall in pixels — 3×3 of 256×128 modules is 768×384. Add cards on the
            Cards page and give each one its position within this wall.
          </p>
          {add.error && <p className="col-span-full text-sm text-bad">{String(add.error)}</p>}
        </form>
      )}

      {isLoading && <div className="text-muted">loading…</div>}
      {!isLoading && !walls?.length && <Empty>No walls yet. Add the first one above.</Empty>}

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2 xl:grid-cols-3">
        {walls?.map(w => {
          const mine = cards?.filter(c => c.wall_id === w.id) ?? [];
          const online = mine.filter(c => c.state === "online").length;
          const full = w.covered >= w.width * w.height;
          return (
            <div key={w.id} className="card space-y-3">
              <div className="flex items-center justify-between">
                <div className="font-semibold">{w.name}</div>
                <StatusChip status={mine.length === 0 ? "warn" : online === mine.length ? "good" : online ? "warn" : "bad"}
                  label={`${online}/${mine.length} card${mine.length === 1 ? "" : "s"}`} />
              </div>
              <div className="tile-label">
                {w.width}×{w.height} · {(w.width * w.height).toLocaleString()} px
                {!full && <span className="text-warn"> · not fully covered</span>}
              </div>

              <LayoutMap wallId={w.id} />

              <WallContent wall={w} cards={mine} />

              {mine.length === 0 && (
                <p className="text-xs text-muted">
                  No cards yet — nothing will be displayed until one is placed on this wall.
                </p>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
