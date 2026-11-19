import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { CalendarClock, Trash2 } from "lucide-react";
import { api } from "../lib/api";
import { PageHeader, Empty } from "../components/widgets";

const DAYS = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"];
const hhmm = (m: number) => `${String(Math.floor(m/60)).padStart(2,"0")}:${String(m%60).padStart(2,"0")}`;
const toMin = (s: string) => { const [h,m] = s.split(":").map(Number); return h*60+m; };

export default function Schedule() {
  const qc = useQueryClient();
  const { data: rows, isLoading } = useQuery({ queryKey: ["schedules"], queryFn: () => api<any[]>("/schedules") });
  const { data: walls } = useQuery({ queryKey: ["walls"], queryFn: () => api<any[]>("/walls") });
  const { data: shows } = useQuery({ queryKey: ["shows"], queryFn: () => api<any[]>("/shows") });
  const inv = () => qc.invalidateQueries({ queryKey: ["schedules"] });
  const add = useMutation({ mutationFn: (b: any) => api("/schedules", { method: "POST", body: JSON.stringify(b) }), onSuccess: inv });
  const del = useMutation({ mutationFn: (id: number) => api(`/schedules/${id}`, { method: "DELETE" }), onSuccess: inv });

  return (
    <div className="space-y-4">
      <PageHeader title="Schedule" icon={CalendarClock}>
        dayparting. the highest-priority window covering now wins; otherwise the wall's standing assignment.
      </PageHeader>

      <form className="card grid grid-cols-2 gap-3 md:grid-cols-6"
        onSubmit={e => { e.preventDefault(); const f = new FormData(e.currentTarget as HTMLFormElement);
          add.mutate({ wall_id: Number(f.get("wall")), show_id: Number(f.get("show")),
            name: f.get("name"), start_min: toMin(String(f.get("start"))), end_min: toMin(String(f.get("end"))),
            days: DAYS.map((_,i)=> f.get(`d${i}`) ? String(i) : "").join(""), priority: Number(f.get("priority")||0) }); }}>
        <label className="text-sm">name<input name="name" className="input" required /></label>
        <label className="text-sm">wall<select name="wall" className="input">{walls?.map(w=><option key={w.id} value={w.id}>{w.name}</option>)}</select></label>
        <label className="text-sm">show<select name="show" className="input">{shows?.map(s=><option key={s.id} value={s.id}>{s.name}</option>)}</select></label>
        <label className="text-sm">from<input name="start" type="time" className="input" defaultValue="18:00" /></label>
        <label className="text-sm">to<input name="end" type="time" className="input" defaultValue="23:00" /></label>
        <label className="text-sm">priority<input name="priority" type="number" className="input" defaultValue={1} /></label>
        <div className="col-span-full flex flex-wrap items-center gap-3">
          {DAYS.map((d,i) => (
            <label key={d} className="flex items-center gap-1 text-sm">
              <input type="checkbox" name={`d${i}`} defaultChecked /> {d}
            </label>
          ))}
          <button className="btn-primary ml-auto" type="submit">add window</button>
        </div>
      </form>

      {isLoading && <div className="text-muted">loading…</div>}
      {!isLoading && !rows?.length && <Empty>No schedules. Panels play their standing assignment.</Empty>}
      {!!rows?.length && (
        <div className="card overflow-x-auto p-0">
          <table className="w-full text-sm">
            <thead className="border-b border-border text-left">
              <tr className="text-muted">
                <th className="p-2">window</th><th className="p-2">wall</th><th className="p-2">show</th>
                <th className="p-2">when</th><th className="p-2">days</th>
                <th className="p-2 text-right">priority</th><th className="p-2"></th>
              </tr>
            </thead>
            <tbody>
              {rows.map(r => (
                <tr key={r.id} className="border-b border-border/50">
                  <td className="p-2">{r.name}</td>
                  <td className="p-2">{walls?.find(w=>w.id===r.wall_id)?.name ?? r.wall_id}</td>
                  <td className="p-2">{shows?.find(s=>s.id===r.show_id)?.name ?? r.show_id}</td>
                  <td className="p-2">{hhmm(r.start_min)}–{hhmm(r.end_min)}{r.end_min<=r.start_min && <span className="text-faint"> (+1d)</span>}</td>
                  <td className="p-2">{[...r.days].map((d:string)=>DAYS[Number(d)]).join(" ")}</td>
                  <td className="p-2 text-right">{r.priority}</td>
                  <td className="p-2 text-right">
                    <button className="btn" onClick={() => del.mutate(r.id)} aria-label="delete"><Trash2 size={14} /></button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
