/**
 * Overview — main dashboard with key metrics and recent activity.
 */
import { createSupabaseServerClient } from "@/lib/supabase";
import type { AiDecision, Trade } from "@/lib/types";

const STARTING_BALANCE = 500;

function fmt(n: number, d = 2) { return n.toFixed(d); }
function pct(n: number) { return `${(n * 100).toFixed(1)}%`; }
function pnlSign(n: number) { return n >= 0 ? "+" : ""; }
function ago(dateStr: string) {
  const diff = Date.now() - new Date(dateStr).getTime();
  const m = Math.floor(diff / 60000);
  if (m < 1) return "just now";
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.floor(h / 24)}d ago`;
}

export const revalidate = 30;

export default async function OverviewPage() {
  const db = createSupabaseServerClient();

  const [tradesResp, decisionsResp, positionsResp, todayResp] = await Promise.all([
    db.from("trades").select("*").order("created_at", { ascending: false }),
    db.from("ai_decisions").select("*").order("created_at", { ascending: false }).limit(5),
    db.from("trades").select("id").eq("status", "pending"),
    db.from("daily_summary").select("*").order("date", { ascending: false }).limit(1).single(),
  ]);

  const trades: Trade[] = tradesResp.data ?? [];
  const decisions: AiDecision[] = decisionsResp.data ?? [];
  const openCount = positionsResp.data?.length ?? 0;
  const today = todayResp.data;

  const filled = trades.filter((t) => t.status === "filled");
  const wins = filled.filter((t) => (t.pnl_dollars ?? 0) > 0).length;
  const losses = filled.filter((t) => (t.pnl_dollars ?? 0) < 0).length;
  const totalPnl = filled.reduce((s, t) => s + (t.pnl_dollars ?? 0), 0);
  const winRate = filled.length > 0 ? wins / filled.length : 0;
  const balance = STARTING_BALANCE + totalPnl;
  const todayPnl = today?.net_pnl_dollars ?? 0;

  const stats = [
    {
      label: "Balance",
      value: `$${fmt(balance)}`,
      sub: `started at $${STARTING_BALANCE}`,
      color: balance >= STARTING_BALANCE ? "#00C076" : "#ef4444",
      big: true,
    },
    {
      label: "Total P&L",
      value: `${pnlSign(totalPnl)}$${fmt(Math.abs(totalPnl))}`,
      sub: `${pnlSign(totalPnl)}${fmt(totalPnl / STARTING_BALANCE * 100, 1)}% ROI`,
      color: totalPnl >= 0 ? "#00C076" : "#ef4444",
      big: true,
    },
    {
      label: "Win Rate",
      value: pct(winRate),
      sub: `${wins}W / ${losses}L`,
      color: winRate >= 0.5 ? "#00C076" : "#ef4444",
      big: false,
    },
    {
      label: "Total Trades",
      value: String(filled.length),
      sub: `${trades.length} all time`,
      color: "#f0f4f8",
      big: false,
    },
    {
      label: "Open Positions",
      value: String(openCount),
      sub: "pending resolution",
      color: openCount > 0 ? "#60a5fa" : "#4b6070",
      big: false,
    },
    {
      label: "Today's P&L",
      value: `${pnlSign(todayPnl)}$${fmt(Math.abs(todayPnl))}`,
      sub: today ? `${today.trades_placed} trades today` : "no data yet",
      color: todayPnl > 0 ? "#00C076" : todayPnl < 0 ? "#ef4444" : "#4b6070",
      big: false,
    },
  ];

  return (
    <div style={{ padding: "28px 32px" }}>
      {/* Header */}
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 28 }}>
        <div>
          <h1 style={{ fontSize: 22, fontWeight: 700, color: "#f0f4f8", marginBottom: 4 }}>Overview</h1>
          <p style={{ color: "#4b6070", fontSize: 13 }}>Paper trading · Live market data</p>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 8, padding: "6px 14px", background: "#0d1320", border: "1px solid #1e2a3a", borderRadius: 8 }}>
          <span className="dot-online" />
          <span style={{ fontSize: 12, color: "#00C076", fontWeight: 600 }}>Online</span>
        </div>
      </div>

      {/* Stats */}
      <div className="stats-grid" style={{ marginBottom: 28 }}>
        {stats.map((s) => (
          <div key={s.label} className="stat-card">
            <div style={{ fontSize: 11, color: "#4b6070", textTransform: "uppercase", letterSpacing: "0.06em", marginBottom: 10 }}>
              {s.label}
            </div>
            <div style={{ fontSize: s.big ? 26 : 22, fontWeight: 700, fontFamily: "JetBrains Mono, monospace", color: s.color, marginBottom: 4 }}>
              {s.value}
            </div>
            <div style={{ fontSize: 11, color: "#4b6070" }}>{s.sub}</div>
          </div>
        ))}
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 20 }}>
        {/* Recent Decisions */}
        <div className="card">
          <div className="section-title">Recent AI Decisions</div>
          {decisions.length === 0 ? (
            <div style={{ color: "#4b6070", fontSize: 13, padding: "20px 0", textAlign: "center" }}>No decisions yet</div>
          ) : (
            <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
              {decisions.map((d) => {
                const isSkip = d.recommendation === "SKIP";
                const isBuyYes = d.recommendation === "BUY_YES";
                return (
                  <div key={d.id} style={{
                    display: "flex", alignItems: "flex-start", justifyContent: "space-between",
                    padding: "10px 12px", borderRadius: 8,
                    background: isSkip ? "#0d1320" : isBuyYes ? "rgba(0,192,118,0.06)" : "rgba(239,68,68,0.06)",
                    border: `1px solid ${isSkip ? "#1e2a3a" : isBuyYes ? "rgba(0,192,118,0.2)" : "rgba(239,68,68,0.2)"}`,
                    gap: 12,
                  }}>
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <div style={{ fontSize: 12, color: "#8899aa", marginBottom: 3, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
                        {d.market_title ?? d.ticker}
                      </div>
                      <div style={{ fontSize: 11, color: "#4b6070" }}>
                        Edge: <span style={{ color: (d.edge ?? 0) > 0 ? "#00C076" : "#ef4444" }}>{pct(d.edge ?? 0)}</span>
                        {" · "}Conf: {pct(d.confidence ?? 0)}
                      </div>
                    </div>
                    <div style={{ display: "flex", flexDirection: "column", alignItems: "flex-end", gap: 4, flexShrink: 0 }}>
                      <span className={`badge ${isSkip ? "badge-gray" : isBuyYes ? "badge-green" : "badge-red"}`}>
                        {d.recommendation ?? "SKIP"}
                      </span>
                      <span style={{ fontSize: 10, color: "#4b6070" }}>{ago(d.created_at)}</span>
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>

        {/* Recent Trades */}
        <div className="card">
          <div className="section-title">Recent Trades</div>
          {trades.length === 0 ? (
            <div style={{ color: "#4b6070", fontSize: 13, padding: "20px 0", textAlign: "center" }}>No trades yet</div>
          ) : (
            <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
              {trades.slice(0, 5).map((t) => {
                const pnl = t.pnl_dollars;
                const isPending = t.status === "pending";
                return (
                  <div key={t.id} style={{
                    display: "flex", alignItems: "center", justifyContent: "space-between",
                    padding: "10px 12px", borderRadius: 8,
                    background: isPending ? "rgba(59,130,246,0.05)" : pnl !== null && pnl > 0 ? "rgba(0,192,118,0.05)" : pnl !== null && pnl < 0 ? "rgba(239,68,68,0.05)" : "#0d1320",
                    border: `1px solid ${isPending ? "rgba(59,130,246,0.2)" : pnl !== null && pnl > 0 ? "rgba(0,192,118,0.15)" : pnl !== null && pnl < 0 ? "rgba(239,68,68,0.15)" : "#1e2a3a"}`,
                    gap: 12,
                  }}>
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <div style={{ fontSize: 12, color: "#8899aa", marginBottom: 2, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
                        {t.market_title}
                      </div>
                      <div style={{ fontSize: 11, color: "#4b6070" }}>
                        {t.side.toUpperCase()} · ${fmt(t.amount_dollars)} · {t.contracts} contracts
                      </div>
                    </div>
                    <div style={{ display: "flex", flexDirection: "column", alignItems: "flex-end", gap: 4, flexShrink: 0 }}>
                      {isPending ? (
                        <span className="badge badge-blue">PENDING</span>
                      ) : pnl !== null ? (
                        <span className={pnl >= 0 ? "value-profit" : "value-loss"} style={{ fontSize: 13 }}>
                          {pnlSign(pnl)}${fmt(Math.abs(pnl))}
                        </span>
                      ) : (
                        <span style={{ color: "#4b6070", fontSize: 12 }}>—</span>
                      )}
                      <span style={{ fontSize: 10, color: "#4b6070" }}>{ago(t.created_at)}</span>
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
