import { AlertTriangle, Check, OctagonAlert, type LucideIcon } from "lucide-react";
import type { ReactNode } from "react";

/** Status is ALWAYS icon + text + colour — never colour alone, so a
 *  red/green-colour-blind operator reads the same state as everyone. */
export function StatusChip({ status, label }: { status: "good" | "warn" | "bad"; label?: string }) {
  const map = {
    good: { Icon: Check, cls: "text-good", text: label ?? "ok" },
    warn: { Icon: AlertTriangle, cls: "text-warn", text: label ?? "warn" },
    bad: { Icon: OctagonAlert, cls: "text-bad", text: label ?? "down" },
  } as const;
  const { Icon, cls, text } = map[status];
  return (
    <span className={`inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-xs ${cls}`}>
      <Icon size={13} aria-hidden /> {text}
    </span>
  );
}

export function PageHeader({ title, icon: Icon, actions, children }:
  { title: string; icon?: LucideIcon; actions?: ReactNode; children?: ReactNode }) {
  return (
    <div className="flex items-start justify-between gap-4">
      <div>
        <h1 className="flex items-center gap-2 text-xl font-semibold">
          {Icon && <Icon size={20} aria-hidden />} {title}
        </h1>
        {children && <p className="mt-1 text-sm text-muted">{children}</p>}
      </div>
      {actions}
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="card text-muted">{children}</div>;
}
