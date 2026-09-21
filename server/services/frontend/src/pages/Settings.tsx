import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Settings as Cog } from "lucide-react";
import { useState } from "react";
import { api } from "../lib/api";
import { PageHeader, Empty } from "../components/widgets";

export default function SettingsPage() {
  const qc = useQueryClient();
  const { data: rows } = useQuery({ queryKey: ["settings"], queryFn: () => api<any[]>("/settings") });
  const { data: tokens } = useQuery({ queryKey: ["tokens"], queryFn: () => api<any[]>("/tokens") });
  const [minted, setMinted] = useState<string>();
  const inv = (k: string) => () => qc.invalidateQueries({ queryKey: [k] });

  const put = useMutation({ mutationFn: (b: any) => api("/settings", { method: "PUT", body: JSON.stringify(b) }), onSuccess: inv("settings") });
  const mk = useMutation({
    mutationFn: (b: any) => api<any>("/tokens", { method: "POST", body: JSON.stringify(b) }),
    onSuccess: (d: any) => { setMinted(d.token); inv("tokens")(); },
  });
  const revoke = useMutation({ mutationFn: (id: number) => api(`/tokens/${id}`, { method: "DELETE" }), onSuccess: inv("tokens") });

  return (
    <div className="space-y-4">
      <PageHeader title="Settings" icon={Cog}>
        cameras, wowza, active directory, and API tokens for other homelab apps.
      </PageHeader>

      <div className="card space-y-3">
        <div className="tile-label">add / update a setting</div>
        <p className="text-sm text-muted">
          camera URLs use <code>camera.&lt;name&gt;</code> (e.g. <code>camera.front-door</code>),
          the Wowza base is <code>wowza.base</code>. Live URLs live here, not in scene
          documents, so a show stays safe to share and a re-addressed camera is one edit.
        </p>
        <form className="flex flex-wrap gap-2"
          onSubmit={e => { e.preventDefault(); const f = new FormData(e.currentTarget as HTMLFormElement);
            put.mutate({ key: f.get("key"), value: f.get("value") }); (e.currentTarget as HTMLFormElement).reset(); }}>
          <input name="key" className="input flex-1" placeholder="camera.front-door" required />
          <input name="value" className="input flex-[2]" placeholder="rtsp://user:pw@camera.local/Streaming/Channels/101" />
          <button className="btn-primary">save</button>
        </form>
        <div className="space-y-1">
          {rows?.map(r => (
            <div key={r.key} className="flex justify-between border-b border-border/50 py-1 text-sm">
              <span className="font-mono">{r.key}</span>
              <span className="text-muted">{r.value ?? "—"}</span>
            </div>
          ))}
          {!rows?.length && <Empty>nothing configured yet</Empty>}
        </div>
      </div>

      <div className="card space-y-3">
        <div className="tile-label">API tokens</div>
        <p className="text-sm text-muted">
          scoped and individually revocable, so one integration can be cut off without
          breaking the others. the secret is shown once.
        </p>
        {minted && (
          <div className="rounded-lg border border-warn/40 bg-raised p-3 text-sm">
            <div className="font-semibold text-warn">copy this now — it cannot be shown again</div>
            <code className="break-all">{minted}</code>
          </div>
        )}
        <form className="flex flex-wrap gap-2"
          onSubmit={e => { e.preventDefault(); const f = new FormData(e.currentTarget as HTMLFormElement);
            mk.mutate({ name: f.get("name"), scopes: f.get("scopes") }); }}>
          <input name="name" className="input flex-1" placeholder="lobby-sign" required />
          <select name="scopes" className="input w-40"><option>read</option><option>read,write</option><option>read,write,admin</option></select>
          <button className="btn-primary">mint</button>
        </form>
        <div className="space-y-1">
          {tokens?.map(t => (
            <div key={t.id} className="flex items-center justify-between border-b border-border/50 py-1 text-sm">
              <span>{t.name} <span className="ml-2 font-mono text-xs text-faint">{t.prefix}…</span></span>
              <span className="flex items-center gap-3 text-muted">
                <span className="text-xs">{t.scopes}</span>
                <span className="text-xs">{t.last_used ? new Date(t.last_used).toLocaleString() : "never used"}</span>
                {t.revoked ? <span className="text-bad text-xs">revoked</span>
                  : <button className="btn" onClick={() => revoke.mutate(t.id)}>revoke</button>}
              </span>
            </div>
          ))}
          {!tokens?.length && <Empty>no tokens</Empty>}
        </div>
      </div>
    </div>
  );
}
