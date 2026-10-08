import { useState } from "react";
import { BASE, authPost } from "../lib/api";

export default function Login() {
  const [err, setErr] = useState<string>();
  const [busy, setBusy] = useState(false);
  return (
    <div className="flex min-h-screen items-center justify-center p-6">
      <form className="card w-full max-w-sm space-y-3"
        onSubmit={async e => {
          e.preventDefault(); setBusy(true); setErr(undefined);
          const f = new FormData(e.currentTarget as HTMLFormElement);
          try {
            await authPost("/auth/login", { username: f.get("u"), password: f.get("p"), remember: !!f.get("r") });
            location.href = BASE + "/";
          } catch (x: any) { setErr(x.message); } finally { setBusy(false); }
        }}>
        <div className="flex items-center gap-2">
          <img src="./icon.svg" width={22} height={22} alt="" />
          <div>
            <div className="font-semibold">Marquee</div>
            <div className="text-[11px] italic text-faint">what the walls are saying</div>
          </div>
        </div>
        <label className="block text-sm">username<input name="u" className="input mt-1" autoFocus required /></label>
        <label className="block text-sm">password<input name="p" type="password" className="input mt-1" required /></label>
        <label className="flex items-center gap-2 text-sm"><input type="checkbox" name="r" /> stay signed in</label>
        {err && <p className="text-sm text-bad">{err}</p>}
        <button className="btn-primary w-full" disabled={busy}>{busy ? "signing in…" : "sign in"}</button>
      </form>
    </div>
  );
}
