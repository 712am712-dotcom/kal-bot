/**
 * /calendar — Monthly P&L calendar heatmap.
 */
import { createSupabaseServerClient } from "@/lib/supabase";
import type { DailySummary } from "@/lib/types";

export const revalidate = 300;

const MONTHS = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
const FULL_MONTHS = ["January","February","March","April","May","June","July","August","September","October","November","December"];
const DAYS_SHORT = ["S","M","T","W","T","F","S"];

function getDaysInMonth(y: number, m: number) { return new Date(y, m + 1, 0).getDate(); }
function getFirstDOW(y: number, m: number) { return new Date(y, m, 1).getDay(); }

export default async function CalendarPage({
  searchParams,
}: {
  searchParams: { year?: string; month?: string };
}) {
  const now = new Date();
  const year = Number(searchParams.year ?? now.getFullYear());
  const month = Number(searchParams.month ?? now.getMonth());

  const db = createSupabaseServerClient();
  const startDate = `${year}-${String(month + 1).padStart(2, "0")}-01`;
  const endDate = `${year}-${String(month + 1).padStart(2, "0")}-${String(getDaysInMonth(year, month)).padStart(2, "0")}`;

  const [monthResp, allResp] = await Promise.all([
    db.from("daily_summary").select("*").gte("date", startDate).lte("date", endDate).order("date"),
    db.from("daily_summary").select("date, net_pnl_dollars, trades_placed").order("date"),
  ]);

  const summaries: DailySummary[] = monthResp.data ?? [];
  const allSummaries = allResp.data ?? [];

  const byDate: Record<string, DailySummary> = {};
  for (const s of summaries) byDate[s.date] = s;

  const totalPnl = allSummaries.reduce((s, r) => s + (r.net_pnl_dollars ?? 0), 0);
  const totalTrades = allSummaries.reduce((s, r) => s + (r.trades_placed ?? 0), 0);
  const profitDays = allSummaries.filter((r) => (r.net_pnl_dollars ?? 0) > 0).length;
  const lossDays = allSummaries.filter((r) => (r.net_pnl_dollars ?? 0) < 0).length;
  const monthPnl = summaries.reduce((s, r) => s + (r.net_pnl_dollars ?? 0), 0);

  const daysInMonth = getDaysInMonth(year, month);
  const firstDay = getFirstDOW(year, month);
  const prevMonth = month === 0 ? 11 : month - 1;
  const prevYear = month === 0 ? year - 1 : year;
  const nextMonth = month === 11 ? 0 : month + 1;
  const nextYear = month === 11 ? year + 1 : year;

  // Generate year heatmap data
  const yearStart = `${year}-01-01`;
  const yearEnd = `${year}-12-31`;
  const { data: yearData } = await db.from("daily_summary").select("date, net_pnl_dollars").gte("date", yearStart).lte("date", yearEnd);
  const yearByDate: Record<string, number> = {};
  for (const d of yearData ?? []) yearByDate[d.date] = d.net_pnl_dollars ?? 0;

  return (
    <div style={{ padding: "28px 32px" }}>
      <div className="page-header">
        <h1 className="page-title">P&L Calendar</h1>
        <p className="page-subtitle">Daily performance heatmap</p>
      </div>

      {/* All-time stats */}
      <div className="stats-grid" style={{ marginBottom: 28 }}>
        {[
          { label: "All-Time P&L", value: `${totalPnl >= 0 ? "+" : ""}$${totalPnl.toFixed(2)}`, color: totalPnl >= 0 ? "#00C076" : "#ef4444" },
          { label: "Total Trades", value: String(totalTrades), color: "#f0f4f8" },
          { label: "Profit Days", value: String(profitDays), color: "#00C076" },
          { label: "Loss Days", value: String(lossDays), color: lossDays > 0 ? "#ef4444" : "#4b6070" },
        ].map((s) => (
          <div key={s.label} className="stat-card">
            <div style={{ fontSize: 11, color: "#4b6070", textTransform: "uppercase", letterSpacing: "0.06em", marginBottom: 8 }}>{s.label}</div>
            <div style={{ fontSize: 22, fontWeight: 700, fontFamily: "JetBrains Mono, monospace", color: s.color }}>{s.value}</div>
          </div>
        ))}
      </div>

      {/* Year heatmap */}
      <div className="card" style={{ marginBottom: 20 }}>
        <div className="section-title">{year} Activity</div>
        <div style={{ display: "flex", gap: 3, flexWrap: "wrap" }}>
          {Array.from({ length: 365 }).map((_, i) => {
            const d = new Date(year, 0, i + 1);
            const ds = `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,"0")}-${String(d.getDate()).padStart(2,"0")}`;
            const pnl = yearByDate[ds] ?? null;
            let bg = "#0d1320";
            if (pnl !== null) {
              if (pnl > 0) bg = `rgba(0,192,118,${Math.min(0.15 + Math.abs(pnl)/50*0.7, 0.85)})`;
              else if (pnl < 0) bg = `rgba(239,68,68,${Math.min(0.15 + Math.abs(pnl)/50*0.7, 0.85)})`;
              else bg = "#111827";
            }
            return (
              <div key={ds} title={`${ds}${pnl !== null ? `: ${pnl >= 0 ? "+" : ""}$${pnl.toFixed(2)}` : ""}`}
                style={{ width: 11, height: 11, borderRadius: 2, background: bg, flexShrink: 0 }} />
            );
          })}
        </div>
        <div style={{ display: "flex", gap: 12, marginTop: 10, fontSize: 11, color: "#4b6070", alignItems: "center" }}>
          <span>Less</span>
          {[0.15, 0.35, 0.55, 0.75, 0.9].map((a) => (
            <div key={a} style={{ width: 11, height: 11, borderRadius: 2, background: `rgba(0,192,118,${a})` }} />
          ))}
          <span>More profit</span>
          <span style={{ marginLeft: 12 }}>Less</span>
          {[0.15, 0.35, 0.55, 0.75, 0.9].map((a) => (
            <div key={a} style={{ width: 11, height: 11, borderRadius: 2, background: `rgba(239,68,68,${a})` }} />
          ))}
          <span>More loss</span>
        </div>
      </div>

      {/* Monthly calendar */}
      <div className="card">
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 20 }}>
          <a href={`/calendar?year=${prevYear}&month=${prevMonth}`} className="btn btn-ghost" style={{ padding: "6px 12px", fontSize: 13 }}>
            ← {MONTHS[prevMonth]}
          </a>
          <div style={{ textAlign: "center" }}>
            <div style={{ fontSize: 17, fontWeight: 700, color: "#f0f4f8" }}>{FULL_MONTHS[month]} {year}</div>
            <div style={{ fontSize: 13, fontFamily: "JetBrains Mono, monospace", color: monthPnl >= 0 ? "#00C076" : "#ef4444", marginTop: 3 }}>
              {monthPnl >= 0 ? "+" : ""}${monthPnl.toFixed(2)} this month
            </div>
          </div>
          <a href={`/calendar?year=${nextYear}&month=${nextMonth}`} className="btn btn-ghost" style={{ padding: "6px 12px", fontSize: 13 }}>
            {MONTHS[nextMonth]} →
          </a>
        </div>

        <div style={{ display: "grid", gridTemplateColumns: "repeat(7, 1fr)", gap: 3, marginBottom: 3 }}>
          {DAYS_SHORT.map((d, i) => (
            <div key={i} style={{ textAlign: "center", fontSize: 10, fontWeight: 600, color: "#2a3a4a", padding: "4px 0", textTransform: "uppercase" }}>{d}</div>
          ))}
        </div>

        <div style={{ display: "grid", gridTemplateColumns: "repeat(7, 1fr)", gap: 3 }}>
          {Array.from({ length: firstDay }).map((_, i) => (
            <div key={`e${i}`} style={{ minHeight: 64 }} />
          ))}
          {Array.from({ length: daysInMonth }).map((_, i) => {
            const day = i + 1;
            const dateStr = `${year}-${String(month + 1).padStart(2, "0")}-${String(day).padStart(2, "0")}`;
            const s = byDate[dateStr];
            const pnl = s?.net_pnl_dollars ?? null;
            const isToday = now.getFullYear() === year && now.getMonth() === month && now.getDate() === day;
            let bg = "#0d1320"; let border = "#1e2a3a"; let pnlColor = "#4b6070";
            if (pnl !== null) {
              const intensity = Math.min(Math.abs(pnl) / 50, 1);
              if (pnl > 0) { bg = `rgba(0,192,118,${0.07+intensity*0.25})`; border = `rgba(0,192,118,${0.2+intensity*0.3})`; pnlColor = "#00C076"; }
              else if (pnl < 0) { bg = `rgba(239,68,68,${0.07+intensity*0.25})`; border = `rgba(239,68,68,${0.2+intensity*0.3})`; pnlColor = "#ef4444"; }
              else { bg = "#111827"; }
            }
            return (
              <div key={day} style={{
                minHeight: 64, background: bg,
                border: `1px solid ${isToday ? "#00C076" : border}`,
                borderRadius: 6, padding: "7px 8px",
                display: "flex", flexDirection: "column", justifyContent: "space-between",
                boxShadow: isToday ? "0 0 0 1px rgba(0,192,118,0.25)" : "none",
              }}>
                <div style={{ fontSize: 11, fontWeight: isToday ? 700 : 500, color: isToday ? "#00C076" : "#4b6070" }}>{day}</div>
                {pnl !== null && (
                  <div>
                    <div style={{ fontSize: 12, fontWeight: 700, fontFamily: "JetBrains Mono, monospace", color: pnlColor }}>
                      {pnl >= 0 ? "+" : ""}${pnl.toFixed(2)}
                    </div>
                    {s?.trades_placed > 0 && (
                      <div style={{ fontSize: 9, color: "#4b6070", marginTop: 1 }}>{s.trades_placed}t</div>
                    )}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
