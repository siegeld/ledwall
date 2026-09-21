import { Link, useLocation } from "react-router-dom";
import { HelpCircle, LayoutGrid, CircuitBoard, Monitor, Settings, CalendarClock, LogOut, Sun, Moon } from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";
import { BASE, authPost } from "../lib/api";

const NAV = [
  { href: "/", label: "Walls", icon: Monitor },
  { href: "/cards", label: "Cards", icon: CircuitBoard },
  { href: "/shows", label: "Content", icon: LayoutGrid },
  { href: "/schedule", label: "Schedule", icon: CalendarClock },
  { href: "/settings", label: "Settings", icon: Settings },
  { href: "/help", label: "Help", icon: HelpCircle },
];

function ThemeToggle() {
  const [dark, setDark] = useState(() => document.documentElement.classList.contains("dark"));
  useEffect(() => {
    document.documentElement.classList.toggle("dark", dark);
    try { localStorage.setItem("marquee.theme", dark ? "dark" : "light"); } catch {}
  }, [dark]);
  return (
    <button className="btn" onClick={() => setDark(d => !d)} aria-label="toggle theme">
      {dark ? <Sun size={15} /> : <Moon size={15} />}
    </button>
  );
}

function TopBar({ me, version, online }: { me: any; version: string; online: boolean }) {
  return (
    <header className="flex h-12 items-center justify-end gap-3 border-b border-border px-4">
      <span className="flex items-center gap-1.5 text-xs">
        <span className={`inline-block h-2 w-2 rounded-full ${online ? "bg-good" : "bg-bad"}`} aria-hidden />
        <span className="text-muted">{online ? "online" : "offline"}</span>
      </span>
      <ThemeToggle />
      {me && (
        <span className="text-xs text-muted">
          {me.username} <span className="ml-1 rounded bg-raised px-1.5 py-0.5">{me.role}</span>
        </span>
      )}
      <button className="btn inline-flex items-center gap-1"
        onClick={async () => { await authPost("/auth/logout"); location.href = `${BASE}/login`; }}>
        <LogOut size={14} aria-hidden /> log out
      </button>
    </header>
  );
}

export default function Shell({ children }: { children: ReactNode }) {
  const loc = useLocation();
  const [me, setMe] = useState<any>(null);
  const [version, setVersion] = useState("—");
  const [online, setOnline] = useState(true);

  useEffect(() => {
    fetch(`${BASE}/auth/me`, { credentials: "include" })
      .then(r => (r.ok ? r.json() : null)).then(setMe).catch(() => setMe(null));
    fetch(`${BASE}/healthz`).then(r => r.json())
      .then(d => { setVersion(d.version); setOnline(true); })
      .catch(() => setOnline(false));
  }, []);

  return (
    <div className="flex min-h-screen">
      <aside className="flex w-52 flex-col border-r border-border bg-surface">
        <div className="px-4 py-3">
          <div className="flex items-center gap-2">
            <img src="./icon.svg" width={20} height={20} alt="" />
            <span className="font-semibold">Marquee</span>
          </div>
          <div className="text-[11px] italic text-faint">what the walls are saying</div>
        </div>
        <nav className="flex flex-col gap-0.5 px-2">
          {NAV.map(({ href, label, icon: Icon }) => {
            const active = loc.pathname === href;
            return (
              <Link key={href} to={href}
                className={`flex items-center gap-2 rounded-lg px-2 py-1.5 text-sm ${
                  active ? "bg-accent text-accent-fg" : "hover:bg-raised"}`}>
                <Icon size={15} aria-hidden /> {label}
              </Link>
            );
          })}
        </nav>
        <div className="mt-auto px-4 py-3 text-[11px] text-faint">v{version}</div>
      </aside>
      <div className="flex min-w-0 flex-1 flex-col">
        <TopBar me={me} version={version} online={online} />
        <main className="flex-1 p-6">{children}</main>
      </div>
    </div>
  );
}
