import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Check, Download, LayoutGrid, Play, Save, Upload } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { api, BASE } from "../lib/api";
import { PageHeader, Empty } from "../components/widgets";

const STARTER = `display: {width: 256, height: 128, fps: 24}
scenes:
  - name: hello
    duration: 10s
    layers:
      - {type: gradient, start: "#0B0E13", end: "#1a2433", box: [0,0,256,128]}
      - {type: text, text: "{clock:%H:%M}", box: [0,20,256,48], size: 40, align: center, color: "#C9A84C"}
      - {type: text, text: "welcome — {date:%A}", box: [0,88,256,22], size: 14, scroll: auto, color: "#e6edf3"}
`;

/** Live preview: posts the document to the SAME renderer the player uses, so
 *  what is shown here is literally what a panel receives. */
function Preview({ body, t }: { body: string; t: number }) {
  const [url, setUrl] = useState<string>();
  const [err, setErr] = useState<string>();
  const last = useRef<string>("");
  useEffect(() => {
    const key = body + ":" + t.toFixed(1);
    if (key === last.current) return;
    last.current = key;
    let dead = false;
    (async () => {
      const res = await fetch(`${BASE}/api/v1/preview.png`, {
        method: "POST", credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ body, t, scale: 3 }),
      });
      if (dead) return;
      if (!res.ok) { setErr((await res.json().catch(() => ({}))).detail ?? "render failed"); setUrl(undefined); return; }
      setErr(res.headers.get("X-Render-Errors") !== "none" ? res.headers.get("X-Render-Errors")! : undefined);
      setUrl(URL.createObjectURL(await res.blob()));
    })();
    return () => { dead = true; };
  }, [body, t]);
  return (
    <div className="space-y-2">
      <div className="tile-label">preview</div>
      <div className="rounded-xl border border-border bg-black p-2">
        {url ? <img src={url} alt="preview" className="w-full [image-rendering:pixelated]" />
             : <div className="p-8 text-center text-muted">rendering…</div>}
      </div>
      {err && <p className="text-sm text-warn">{err}</p>}
    </div>
  );
}


/** Minimal YAML tokeniser -- enough for a scene document, and no dependency.
 *
 *  A scene document is ~150 lines of keys, quoted strings, numbers and
 *  {bus:...} bindings. A full editor component (CodeMirror, Monaco) is several
 *  hundred KB to tell those four apart, so this paints them itself: a
 *  highlighted <pre> sits behind a transparent <textarea>, which keeps native
 *  editing, selection, undo and spellcheck-off intact. The two must share font
 *  metrics exactly or the cursor drifts from the glyphs, which is why both use
 *  the same leading and size.
 */
function highlight(src: string) {
  const esc = (t: string) => t.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  const span = (cls: string, t: string) => `<span class="${cls}">${esc(t)}</span>`;
  // ONE pass, emitting as it goes. The obvious implementation -- a chain of
  // .replace() calls over the whole line -- is wrong, and wrong in a way that
  // renders: the string rule matches the class="..." inside the spans the key
  // rule just emitted, wraps them again, and the reader sees `"text-accent">`
  // as literal text. Never re-scan your own markup.
  return src.split("\n").map(line => {
    if (/^\s*#/.test(line)) return line.trim() ? span("text-muted", line) : "&nbsp;";
    let out = "", i = 0;
    const key = /^(\s*(?:-\s*)?)([A-Za-z_][\w-]*)(:)/.exec(line);
    if (key) { out += esc(key[1]) + span("text-accent", key[2]) + esc(key[3]); i = key[0].length; }
    while (i < line.length) {
      const rest = line.slice(i);
      let m: RegExpExecArray | null;
      if (rest[0] === "#" && /\s$/.test(line.slice(0, i))) { out += span("text-muted", rest); break; }
      if ((m = /^"[^"]*"/.exec(rest)) || (m = /^'[^']*'/.exec(rest))) {
        out += span("text-warn", m[0]); i += m[0].length; continue;
      }
      if ((m = /^\{[a-z]+:[^}]*\}/.exec(rest))) { out += span("text-good", m[0]); i += m[0].length; continue; }
      if ((m = /^#[0-9a-fA-F]{3,8}\b/.exec(rest))) { out += span("text-warn", m[0]); i += m[0].length; continue; }
      if ((m = /^\d+(?:\.\d+)?(?:s|ms|%)?/.exec(rest))) { out += span("text-good", m[0]); i += m[0].length; continue; }
      out += esc(rest[0]); i++;
    }
    return out || "&nbsp;";
  }).join("\n");
}

function YamlEditor({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  const ta = useRef<HTMLTextAreaElement>(null);
  const pre = useRef<HTMLPreElement>(null);
  const gut = useRef<HTMLDivElement>(null);
  const lines = value.split("\n").length;
  // One scroll position, three panes. Without this the gutter and the paint
  // slide out from under the text the moment the document is taller than the box.
  const sync = () => {
    const el = ta.current; if (!el) return;
    if (pre.current) { pre.current.scrollTop = el.scrollTop; pre.current.scrollLeft = el.scrollLeft; }
    if (gut.current) gut.current.scrollTop = el.scrollTop;
  };
  const shared = "font-mono text-xs leading-[1.45rem]";
  return (
    <div className="mt-1 flex overflow-hidden rounded-lg border border-border bg-raised">
      <div ref={gut} aria-hidden
        className={`${shared} select-none overflow-hidden border-r border-border px-2 py-2 text-right text-muted`}
        style={{ minWidth: "3ch" }}>
        {Array.from({ length: lines }, (_, i) => <div key={i}>{i + 1}</div>)}
      </div>
      <div className="relative flex-1">
        <pre ref={pre} aria-hidden
          className={`${shared} pointer-events-none absolute inset-0 overflow-auto whitespace-pre px-3 py-2 text-fg`}
          dangerouslySetInnerHTML={{ __html: highlight(value) }} />
        <textarea ref={ta} spellCheck={false} value={value} onScroll={sync}
          onChange={e => { onChange(e.target.value); sync(); }}
          onKeyDown={e => {
            // Tab indents instead of leaving the field. YAML is indentation, so
            // a tab key that escapes the editor makes it unusable for its one job.
            if (e.key !== "Tab") return;
            e.preventDefault();
            const el = e.currentTarget, a = el.selectionStart, b = el.selectionEnd;
            const next = value.slice(0, a) + "  " + value.slice(b);
            onChange(next);
            requestAnimationFrame(() => { el.selectionStart = el.selectionEnd = a + 2; });
          }}
          className={`${shared} relative h-[30rem] w-full resize-y bg-transparent px-3 py-2 text-transparent caret-fg outline-none`} />
      </div>
    </div>
  );
}

export default function Shows() {
  const qc = useQueryClient();
  const { data: shows, isLoading } = useQuery({ queryKey: ["shows"], queryFn: () => api<any[]>("/shows") });
  const [sel, setSel] = useState<number | "new">("new");
  const [name, setName] = useState("");
  const [body, setBody] = useState(STARTER);
  const [t, setT] = useState(0);

  const { data: files, refetch: refetchFiles } = useQuery({
    queryKey: ["shows-files"], queryFn: () => api<any>("/shows/files"),
  });
  const [check, setCheck] = useState<any>(null);

  const current = useMemo(() => shows?.find(s => s.id === sel), [shows, sel]);
  const drift = useMemo(
    () => files?.shows?.find((f: any) => f.id === sel),
    [files, sel]);

  // Validate as you type, debounced. The endpoint already existed and nothing
  // called it, so a malformed document was discovered by the preview failing or
  // the save being rejected -- after the mistake rather than at it.
  useEffect(() => {
    const id = setTimeout(async () => {
      try {
        setCheck(await api("/shows/validate", {
          method: "POST", body: JSON.stringify({ name: name || "draft", body }),
        }));
      } catch (e: any) {
        setCheck({ ok: false, error: String(e?.message ?? e) });
      }
    }, 600);
    return () => clearTimeout(id);
  }, [body, name]);
  useEffect(() => {
    if (current) { setName(current.name); setBody(current.body); }
    else { setName(""); setBody(STARTER); }
  }, [current]);

  const save = useMutation({
    mutationFn: () => sel === "new"
      ? api("/shows", { method: "POST", body: JSON.stringify({ name, body }) })
      : api(`/shows/${sel}`, { method: "PUT", body: JSON.stringify({ name, body }) }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["shows"] }); refetchFiles(); },
  });

  const exportAll = useMutation({
    mutationFn: () => api("/shows/export", { method: "POST" }),
    onSuccess: () => refetchFiles(),
  });
  const importAll = useMutation({
    mutationFn: () => api("/shows/import", { method: "POST" }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["shows"] }); refetchFiles(); },
  });

  return (
    <div className="space-y-4">
      <PageHeader title="Content" icon={LayoutGrid}
        actions={<div className="flex items-center gap-2">
          <button className="btn inline-flex items-center gap-1" title={`write every show to ${files?.dir ?? "custom/<site>/shows"}`}
            onClick={() => exportAll.mutate()} disabled={exportAll.isPending}>
            <Download size={15} aria-hidden /> export to files
          </button>
          <button className="btn inline-flex items-center gap-1" title={`load ${files?.dir ?? "custom/<site>/shows"} into the database`}
            onClick={() => importAll.mutate()} disabled={importAll.isPending}>
            <Upload size={15} aria-hidden /> import from files
          </button>
          <button className="btn-primary inline-flex items-center gap-1"
            onClick={() => save.mutate()} disabled={!name}><Save size={15} aria-hidden /> save</button>
        </div>}>
        compose what a panel shows. video, images, live cameras, text and data, in boxes.
      </PageHeader>

      {/* Files are the source of truth; the database is what the player reads.
          Nothing keeps them in step, so say which way round things are. */}
      {(exportAll.isSuccess || importAll.isSuccess || !!files?.orphans?.length) && (
        <div className="card flex flex-wrap items-center gap-x-3 gap-y-1 text-sm">
          {exportAll.isSuccess && <span className="text-good">wrote {(exportAll.data as any)?.written} file(s) to {files?.dir}</span>}
          {importAll.isSuccess && <span className="text-good">imported {(importAll.data as any)?.created} new, {(importAll.data as any)?.updated} updated</span>}
          {!!files?.orphans?.length && (
            <span className="text-warn inline-flex items-center gap-1">
              <AlertTriangle size={14} aria-hidden />
              {files.orphans.length} file(s) on disk not in the database ({files.orphans.map((o: any) => o.name).join(", ")}) — import to load them
            </span>
          )}
        </div>
      )}

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-[14rem_1fr_1fr]">
        <div className="card space-y-1">
          <div className="tile-label">shows</div>
          <button className={`w-full rounded px-2 py-1 text-left text-sm ${sel === "new" ? "bg-accent text-accent-fg" : "hover:bg-raised"}`}
            onClick={() => setSel("new")}>+ new show</button>
          {isLoading && <div className="text-muted">loading…</div>}
          {shows?.map(s => (
            <button key={s.id}
              className={`flex w-full items-center justify-between gap-1 rounded px-2 py-1 text-left text-sm ${sel === s.id ? "bg-accent text-accent-fg" : "hover:bg-raised"}`}
              onClick={() => setSel(s.id)}>
              <span className="truncate">{s.name}</span>
              <span className="flex shrink-0 items-center gap-1">
                {!!s.walls?.length && <span className="text-xs opacity-70" title={`live on ${s.walls.join(", ")}`}>●</span>}
                {files?.shows?.find((f: any) => f.id === s.id && f.state !== "in-sync") &&
                  <AlertTriangle size={12} aria-hidden className="text-warn" />}
              </span>
            </button>
          ))}
          {!isLoading && !shows?.length && <Empty>none yet</Empty>}
        </div>

        <div className="space-y-2">
          <label className="block text-sm">
            <span className="tile-label">name</span>
            <input className="input mt-1" value={name} onChange={e => setName(e.target.value)} placeholder="evening-loop" />
          </label>
          {/* Editing a show changes what is on the glass now. Say so. */}
          {!!current?.walls?.length && (
            <p className="text-sm text-warn inline-flex items-center gap-1">
              <AlertTriangle size={14} aria-hidden />
              live on {current.walls.join(", ")} — saving changes what is showing
            </p>
          )}

          <div>
            <div className="flex items-center justify-between">
              <span className="tile-label">scene document</span>
              {drift && drift.state !== "in-sync" && (
                <span className="text-xs text-warn inline-flex items-center gap-1">
                  <AlertTriangle size={13} aria-hidden />
                  {drift.state === "no-file" ? "not in version control — export to write it"
                    : drift.state === "db-newer" ? `differs from ${drift.file} — export to save it`
                    : `${drift.file} is newer — import to load it`}
                </span>
              )}
              {drift && drift.state === "in-sync" && (
                <span className="text-xs text-muted inline-flex items-center gap-1">
                  <Check size={13} aria-hidden /> matches {drift.file}
                </span>
              )}
            </div>
            <YamlEditor value={body} onChange={setBody} />
          </div>

          {/* What the document actually parses to, live. */}
          {check && (check.ok
            ? <p className="text-sm text-good">
                {check.scenes?.length} scene{check.scenes?.length === 1 ? "" : "s"} ·{" "}
                {Math.round(check.duration_s)}s ·{" "}
                {check.display?.width ?? "auto"}x{check.display?.height ?? "auto"} ·{" "}
                {check.display?.fps} fps · {check.display?.format}
              </p>
            : <p className="text-sm text-bad">{String(check.error).replace(/^Error:\s*/, "")}</p>)}
          {save.error && <p className="text-sm text-bad">{String(save.error)}</p>}
          {save.isSuccess && <p className="text-sm text-good">saved</p>}
        </div>

        <div className="space-y-3">
          <Preview body={body} t={t} />
          <label className="block text-sm">
            <span className="tile-label">scrub — preview at t = {t.toFixed(1)}s</span>
            <input type="range" min={0} max={60} step={0.5} value={t}
              className="mt-1 w-full" onChange={e => setT(Number(e.target.value))} />
          </label>
          <button className="btn inline-flex items-center gap-1" onClick={() => setT(x => x + 0.5)}>
            <Play size={14} aria-hidden /> step
          </button>
        </div>
      </div>
    </div>
  );
}
