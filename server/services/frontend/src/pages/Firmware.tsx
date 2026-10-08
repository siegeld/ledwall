import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { HardDriveDownload, Trash2, Upload, Rocket, Star, Cpu } from "lucide-react";
import { useRef, useState } from "react";
import { api, BASE, fmt } from "../lib/api";
import { PageHeader, StatusChip, Empty } from "../components/widgets";

/** What each card is running, for BOTH of the things it runs.
 *
 * A card runs two separate artifacts and they are updated by different paths.
 * Calling them both "firmware" is how this page became confusing, so it names
 * them apart everywhere:
 *
 *   FIRMWARE   the Rust code on the card's CPU — boot.bin. Refetched over TFTP
 *              on EVERY boot and never stored on the card, so an update is
 *              staging an image here and rebooting. Has a semver.
 *   BITSTREAM  the FPGA logic — the gateware in the card's SPI flash. The card
 *              writes it over the network, and it is live only at the next
 *              POWER CYCLE. Has no semver of its own, so its identity is the
 *              repo version plus board, panel, outputs, clock and build time,
 *              read back out of the gateware itself.
 *
 * The other distinction shown is ASSIGNED versus RUNNING, for each of them.
 * They differ exactly when it matters — an assignment nothing has rebooted into,
 * a card that fell back to the default image, a bitstream written but not yet
 * power-cycled — and a view showing only the assignment calls all of those fine.
 */

type Image = {
  name: string; size: number; modified: number; sha256: string;
  is_default: boolean; used_by: string[];
};
type Bits = { name: string; size: number; modified: number; used_by: string[] };
type Running = {
  card_id: number; card: string; host: string; assigned: string;
  is_default: boolean; online: boolean; version: string | null; isr_count: number | null;
  bitstream: string | null; bitstream_version: string | null;
  bitstream_detail: string | null; bitstream_assigned: string | null;
};

const kb = (n: number) => `${(n / 1024).toFixed(0)} KB`;

function UploadBox({ onDone }: { onDone: () => void }) {
  const file = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function send() {
    const f = file.current?.files?.[0];
    if (!f) return;
    setBusy(true); setErr(null);
    try {
      const body = new FormData();
      body.append("file", f, f.name);
      // Not through api(): that sets a JSON Content-Type, and multipart needs
      // the browser to set its own boundary.
      const res = await fetch(`${BASE}/api/v1/firmware`, {
        method: "POST", credentials: "include", body,
      });
      if (!res.ok) {
        let d = res.statusText;
        try { d = (await res.json()).detail ?? d; } catch { /* keep statusText */ }
        throw new Error(d);
      }
      if (file.current) file.current.value = "";
      onDone();
    } catch (e: any) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="card">
      <div className="tile-label">stage an image</div>
      <div className="mt-2 flex items-center gap-2">
        <input ref={file} type="file" className="input" accept=".bin" />
        <button className="btn-primary whitespace-nowrap" onClick={send} disabled={busy}>
          <Upload size={14} className="mr-1 inline" aria-hidden />
          {busy ? "uploading…" : "Upload"}
        </button>
      </div>
      <p className="mt-2 text-xs text-muted">
        A <code>boot.bin</code> built by <code>./build.sh firmware</code> in the colorlight
        repo. Staging an image changes nothing on its own — assign it to a card below.
      </p>
      {err && <p className="mt-2 text-xs text-bad">{err}</p>}
    </div>
  );
}

function Images({ images, onChange }: { images: Image[]; onChange: () => void }) {
  const del = useMutation({
    mutationFn: (name: string) => api<void>(`/firmware/${encodeURIComponent(name)}`, { method: "DELETE" }),
    onSuccess: onChange,
  });

  if (!images.length) return <Empty>No firmware images staged yet.</Empty>;

  return (
    <div className="card overflow-x-auto">
      <table className="w-full text-sm">
        <thead className="text-left tile-label">
          <tr>
            <th className="pb-2 pr-3">image</th>
            <th className="pb-2 pr-3">size</th>
            <th className="pb-2 pr-3">sha256</th>
            <th className="pb-2 pr-3">in use by</th>
            <th className="pb-2" />
          </tr>
        </thead>
        <tbody>
          {images.map(i => (
            <tr key={i.name} className="border-t border-border">
              <td className="py-1.5 pr-3">
                <span className="font-mono text-xs">{i.name}</span>
                {i.is_default && (
                  <span className="ml-2 inline-flex items-center gap-1 text-xs text-accent">
                    <Star size={12} aria-hidden /> fleet default
                  </span>
                )}
              </td>
              <td className="py-1.5 pr-3 text-muted">{kb(i.size)}</td>
              <td className="py-1.5 pr-3 font-mono text-xs text-faint">{i.sha256.slice(0, 12)}</td>
              <td className="py-1.5 pr-3 text-muted">{i.used_by.join(", ") || "—"}</td>
              <td className="py-1.5 text-right">
                {/* The API refuses both of these; disabling them here explains
                    why instead of surfacing a 400 after the fact. */}
                <button
                  className="btn"
                  title={
                    i.is_default ? "the fleet default cannot be deleted"
                      : i.used_by.length ? `assigned to ${i.used_by.join(", ")}`
                        : "delete this image"
                  }
                  disabled={i.is_default || i.used_by.length > 0 || del.isPending}
                  onClick={() => del.mutate(i.name)}>
                  <Trash2 size={14} aria-hidden />
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {del.isError && <p className="mt-2 text-xs text-bad">{(del.error as Error).message}</p>}
    </div>
  );
}

function Fleet({ rows, images, bits, onChange }:
  { rows: Running[]; images: Image[]; bits: Bits[]; onChange: () => void }) {
  const [pick, setPick] = useState<Record<number, string>>({});
  const [pickBit, setPickBit] = useState<Record<number, string>>({});
  const [note, setNote] = useState<string | null>(null);

  const push = useMutation({
    mutationFn: ({ id, name }: { id: number; name: string }) =>
      api<any>(`/cards/${id}/firmware`, {
        method: "POST",
        body: JSON.stringify({ firmware: name }),
      }),
    onSuccess: (r) => {
      setNote(
        r.back
          ? `${r.card}: now running ${fmt(r.now_running)} (was ${fmt(r.was_running)})` +
            (r.went_down ? "" : " — the card never stopped answering, so it may not have rebooted")
          : `${r.card}: did not come back. ${r.hint ?? ""}`,
      );
      onChange();
    },
    onError: (e) => setNote((e as Error).message),
  });

  // Writing a bitstream is a different operation from pushing firmware, not a
  // variant of it: it rewrites the card's flash and does NOT reboot, because
  // rebooting would restart only the firmware and leave the impression the new
  // gateware was live.
  const pushBit = useMutation({
    mutationFn: ({ id, name }: { id: number; name: string }) =>
      api<any>(`/cards/${id}/bitstream`, {
        method: "POST", body: JSON.stringify({ bitstream: name }),
      }),
    onSuccess: (r) => {
      setNote(
        r.state === "done"
          ? `${r.card}: wrote ${r.written} bytes in ${r.sectors} sectors — ${r.note}`
          : `${r.card}: ${r.state}. ${r.error ?? ""} ${r.note ?? ""}`,
      );
      onChange();
    },
    onError: (e) => setNote((e as Error).message),
  });

  if (!rows.length) return <Empty>No cards registered.</Empty>;

  return (
    <div className="card overflow-x-auto">
      <table className="w-full text-sm">
        <thead className="text-left tile-label">
          <tr>
            <th className="pb-2 pr-3">card</th>
            <th className="pb-2 pr-3">state</th>
            <th className="pb-2 pr-3">firmware assigned</th>
            <th className="pb-2 pr-3">firmware running</th>
            <th className="pb-2 pr-3">push firmware (rust)</th>
            <th className="pb-2 pr-3">bitstream running (fpga)</th>
            <th className="pb-2">push bitstream (fpga)</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(r => (
            <tr key={r.card_id} className="border-t border-border">
              <td className="py-1.5 pr-3">
                {r.card}
                <div className="font-mono text-xs text-faint">{r.host}</div>
              </td>
              <td className="py-1.5 pr-3">
                {r.online
                  ? <StatusChip status="good" label="up" />
                  : <StatusChip status="bad" label="no answer" />}
              </td>
              <td className="py-1.5 pr-3">
                <span className="font-mono text-xs">{r.assigned}</span>
                {r.is_default && <span className="ml-1 text-xs text-muted">(default)</span>}
              </td>
              <td className="py-1.5 pr-3">
                {/* A card that answers without a version is running something
                    older than colorlight 2.15.0, which is not the same as
                    being unreachable. */}
                {r.online
                  ? r.version
                    ? <span className="font-mono text-xs">v{r.version}</span>
                    : <span className="text-xs text-warn">pre-2.15.0 (no version reported)</span>
                  : <span className="text-muted">—</span>}
              </td>
              <td className="py-1.5 pr-3">
                <div className="flex items-center gap-2">
                  <select
                    className="input max-w-[13rem]"
                    value={pick[r.card_id] ?? r.assigned}
                    onChange={e => setPick({ ...pick, [r.card_id]: e.target.value })}>
                    {images.map(i => <option key={i.name} value={i.name}>{i.name}</option>)}
                  </select>
                  <button
                    className="btn-primary whitespace-nowrap"
                    disabled={push.isPending}
                    onClick={() => push.mutate({ id: r.card_id, name: pick[r.card_id] ?? r.assigned })}>
                    <Rocket size={14} className="mr-1 inline" aria-hidden />
                    {push.isPending ? "pushing…" : "Push + reboot"}
                  </button>
                </div>
              </td>
              <td className="py-1.5 pr-3">
                {/* Read out of the FPGA itself, so it is what is configured --
                    not what we recorded pushing. Disagreement with the pushed
                    name means the card has not been power-cycled since, which is
                    the normal state right after a write. */}
                {r.online
                  ? r.bitstream
                    ? <span className="font-mono text-xs" title={r.bitstream_detail ?? ""}>
                        {r.bitstream}
                      </span>
                    : <span className="text-xs text-warn">no version (built before 2.19.0)</span>
                  : <span className="text-muted">—</span>}
                {/* "Running" is read from the FPGA; "pushed" is the file we sent.
                    They are the same string by design -- marquee refuses any
                    bitstream filename that is not "<board>-v<semver>.bit" -- so a
                    difference means one specific thing: written to flash, not yet
                    power-cycled. Saying that is the whole point of showing both. */}
                {r.bitstream_assigned && (() => {
                  const pushed = r.bitstream_assigned.replace(/\.bit$/, "");
                  if (!r.bitstream) {
                    return <div className="text-xs text-faint">pushed: {pushed}</div>;
                  }
                  return pushed === r.bitstream
                    ? <div className="text-xs text-faint">pushed: {pushed} — live</div>
                    : <div className="text-xs text-warn">
                        pushed: {pushed} — awaiting power cycle
                      </div>;
                })()}
              </td>
              <td className="py-1.5">
                {bits.length === 0
                  ? <span className="text-xs text-faint">upload a .bit below</span>
                  : (
                    <div className="flex items-center gap-2">
                      <select
                        className="input max-w-[13rem]"
                        value={pickBit[r.card_id] ?? bits[0].name}
                        onChange={e => setPickBit({ ...pickBit, [r.card_id]: e.target.value })}>
                        {bits.map(b => <option key={b.name} value={b.name}>{b.name}</option>)}
                      </select>
                      <button
                        className="btn whitespace-nowrap"
                        disabled={!r.online || pushBit.isPending}
                        title={r.online
                          ? "write this into the card's SPI flash; live at its next power cycle"
                          : "card is not answering"}
                        onClick={() => pushBit.mutate({ id: r.card_id, name: pickBit[r.card_id] ?? bits[0].name })}>
                        <Cpu size={14} className="mr-1 inline" aria-hidden />
                        {pushBit.isPending ? "writing…" : "Write to flash"}
                      </button>
                    </div>
                  )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {note && <p className="mt-3 text-xs text-muted">{note}</p>}
      <div className="mt-2 space-y-1 text-xs text-faint">
        <p>
          <strong>Push firmware</strong> (Rust code): assigns the image, reboots the board, and
          waits for it to go down and come back. Nothing is written to the card — it netboots
          this image every power cycle, so recovery is pushing a different one.
        </p>
        <p>
          <strong>Write to flash</strong> (FPGA bitstream): rewrites the card's SPI flash a
          sector at a time, which takes a minute or two, and does <strong>not</strong> reboot —
          the new gateware is live at the card's next <strong>power cycle</strong>. The card
          keeps running its current gateware throughout. A bad image cannot strand it: the
          golden recovery bitstream is written once over JTAG and the FPGA falls back to it.
        </p>
      </div>
    </div>
  );
}

function Rollout({ images, walls, onChange }:
  { images: Image[]; walls: any[]; onChange: () => void }) {
  const [name, setName] = useState("");
  const [wall, setWall] = useState("");
  const [out, setOut] = useState<any | null>(null);

  const run = useMutation({
    mutationFn: () => api<any>("/firmware/rollout", {
      method: "POST",
      body: JSON.stringify({
        firmware: name || images[0]?.name,
        wall_id: wall ? Number(wall) : null,
      }),
    }),
    onSuccess: (r) => { setOut(r); onChange(); },
    onError: (e) => setOut({ error: (e as Error).message }),
  });

  return (
    <div className="card">
      <div className="tile-label">roll out to many</div>
      <div className="mt-2 flex flex-wrap items-center gap-2">
        <select className="input max-w-[16rem]" value={name} onChange={e => setName(e.target.value)}>
          <option value="">select an image…</option>
          {images.map(i => <option key={i.name} value={i.name}>{i.name}</option>)}
        </select>
        <select className="input max-w-[14rem]" value={wall} onChange={e => setWall(e.target.value)}>
          <option value="">every enabled card</option>
          {walls.map(w => <option key={w.id} value={w.id}>{w.name}</option>)}
        </select>
        <button className="btn-primary whitespace-nowrap" disabled={!name || run.isPending}
                onClick={() => run.mutate()}>
          {run.isPending ? "rolling out…" : "Roll out"}
        </button>
      </div>
      <p className="mt-2 text-xs text-muted">
        One card at a time, stopping at the first that does not come back — so a bad image
        takes out one board, not the wall.
      </p>
      {out && (
        <pre className="mt-3 overflow-x-auto rounded-lg bg-raised p-3 text-xs">
          {JSON.stringify(out, null, 2)}
        </pre>
      )}
    </div>
  );
}

/** Staging for bitstreams. Pushing one to a card lives in the Cards table above,
 *  beside the firmware push, because "which of the two am I sending?" is the
 *  question this page kept failing to answer. */
function Bitstreams({ bits, cards, onChange }:
  { bits: Bits[]; cards: Running[]; onChange: () => void }) {
  const file = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  async function upload() {
    const f = file.current?.files?.[0];
    if (!f) return;
    setBusy(true); setNote(null);
    try {
      const body = new FormData();
      body.append("file", f, f.name);
      const res = await fetch(`${BASE}/api/v1/bitstreams`, {
        method: "POST", credentials: "include", body,
      });
      if (!res.ok) {
        let d = res.statusText;
        try { d = (await res.json()).detail ?? d; } catch { /* keep statusText */ }
        throw new Error(d);
      }
      if (file.current) file.current.value = "";
      onChange();
    } catch (e: any) { setNote(e.message); } finally { setBusy(false); }
  }

  const push = useMutation({
    mutationFn: ({ id, name }: { id: number; name: string }) =>
      api<any>(`/cards/${id}/bitstream`, {
        method: "POST", body: JSON.stringify({ bitstream: name }),
      }),
    onSuccess: (r) => {
      setNote(
        r.state === "done"
          ? `${r.card}: wrote ${r.written} bytes in ${r.sectors} sectors. ${r.note}`
          : `${r.card}: ${r.state}. ${r.error ?? ""} ${r.note ?? ""}`,
      );
      onChange();
    },
    onError: (e) => setNote((e as Error).message),
  });

  const del = useMutation({
    mutationFn: (name: string) =>
      api<void>(`/bitstreams/${encodeURIComponent(name)}`, { method: "DELETE" }),
    onSuccess: onChange,
    onError: (e) => setNote((e as Error).message),
  });

  return (
    <div className="card space-y-3">
      <div>
        <div className="tile-label">fpga bitstreams</div>
        <p className="mt-1 text-xs text-muted">
          The gateware in the card's SPI flash — a <strong>different artifact</strong> from the
          firmware above, on a different path. The card writes this into its own flash over the
          network, so replacing it needs no JTAG; it becomes live at the card's{" "}
          <strong>next power cycle</strong>, because nothing can reconfigure the FPGA from
          software. A bad image cannot strand a card: if it does not configure, the card boots
          the golden recovery image and comes back to accept another.
        </p>
      </div>

      <div className="flex items-center gap-2">
        <input ref={file} type="file" className="input" accept=".bit" />
        <button className="btn-primary whitespace-nowrap" onClick={upload} disabled={busy}>
          <Upload size={14} className="mr-1 inline" aria-hidden />
          {busy ? "uploading…" : "Upload .bit"}
        </button>
      </div>

      {bits.length === 0
        ? <p className="text-xs text-faint">No bitstreams staged.</p>
        : (
          <table className="w-full text-sm">
            <thead className="text-left tile-label">
              <tr>
                <th className="pb-2 pr-3">bitstream</th>
                <th className="pb-2 pr-3">size</th>
                <th className="pb-2 pr-3">pushed to</th>
                <th className="pb-2" />
              </tr>
            </thead>
            <tbody>
              {bits.map(b => (
                <tr key={b.name} className="border-t border-border">
                  <td className="py-1.5 pr-3 font-mono text-xs">{b.name}</td>
                  <td className="py-1.5 pr-3 text-muted">{kb(b.size)}</td>
                  <td className="py-1.5 pr-3 text-muted">{b.used_by.join(", ") || "—"}</td>
                  <td className="py-1.5 text-right">
                    <button className="btn" disabled={b.used_by.length > 0 || del.isPending}
                            title={b.used_by.length ? `pushed to ${b.used_by.join(", ")}` : "delete"}
                            onClick={() => del.mutate(b.name)}>
                      <Trash2 size={14} aria-hidden />
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}

      {note && <p className="text-xs text-muted">{note}</p>}
    </div>
  );
}

export default function Firmware() {
  const qc = useQueryClient();
  const images = useQuery({ queryKey: ["firmware"], queryFn: () => api<Image[]>("/firmware") });
  const running = useQuery({ queryKey: ["firmware-running"], queryFn: () => api<Running[]>("/firmware/running") });
  const walls = useQuery({ queryKey: ["walls"], queryFn: () => api<any[]>("/walls") });
  const bits = useQuery({ queryKey: ["bitstreams"], queryFn: () => api<Bits[]>("/bitstreams") });

  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["firmware"] });
    qc.invalidateQueries({ queryKey: ["firmware-running"] });
    qc.invalidateQueries({ queryKey: ["bitstreams"] });
    qc.invalidateQueries({ queryKey: ["cards"] });
  };

  const list = images.data ?? [];

  return (
    <div className="space-y-5">
      <PageHeader title="Updates" icon={HardDriveDownload}>
        A card runs <strong>two</strong> separate things, and they are not the same artifact.
        <strong> Firmware</strong> is the Rust program on its CPU — refetched over the network
        on every boot, live as soon as it reboots. The <strong>bitstream</strong> is the FPGA
        logic in its SPI flash — written over the network too, but live only at the card's next
        power cycle. Neither needs JTAG.
      </PageHeader>

      <UploadBox onDone={refresh} />

      <div>
        <h2 className="mb-2 text-sm font-semibold">Cards — what each one is running</h2>
        {running.isLoading
          ? <Empty>Asking the cards what they are running…</Empty>
          : <Fleet rows={running.data ?? []} images={list} bits={bits.data ?? []} onChange={refresh} />}
      </div>

      <div>
        <h2 className="mb-2 text-sm font-semibold">Staged firmware images (Rust code)</h2>
        {images.isLoading ? <Empty>Loading…</Empty> : <Images images={list} onChange={refresh} />}
      </div>

      <Rollout images={list} walls={walls.data ?? []} onChange={refresh} />

      <Bitstreams bits={bits.data ?? []} cards={running.data ?? []} onChange={refresh} />
    </div>
  );
}
