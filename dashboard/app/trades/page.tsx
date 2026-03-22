/**
 * /trades — Full trade log with filtering and P&L summary.
 */
import { createSupabaseServerClient } from "@/lib/supabase";
import type { Trade } from "@/lib/types";

export const revalidate = 30;

function pct(n: number) { return `${(n * 100).toFixed(1)}%`; }
function fmt(n: number) { return n.toFixed(2); }

export default async function TradesPage({
  searchParams,
}: {
  searchParams: { page?: string; coin?: string; result?: string };
}) {
  const db = createSupabaseServerClient();
  const page = Number(searchParams.page ?? 1);
  const pageSize = 40;
  const offset = (page - 1) * pageSize;

  let q = db
    .from("trades")
    .select("*", { count: "exact" })
    .order("created_at", { ascending: false })
    .range(offset, offset + pageSize - 1);

  const coin = searchParams.coin;
  if (coin && coin !== "all") {
    q = q.ilike("ticker", `%${coin}%`);
  }

  const result = searchParams.result;
  if (result === "wins") q = q.gt("pnl_dollars", 0);
  else if (result === "losses") q = q.lt("pnl_dollars", 0);
  else if (result === "pending") q = q.eq("status", "pending");

  const { data, count } = await q;
  const trades: Trade[] = data ?? [];
  const totalPages = Math.ceil((count ?? 0) / pageSize);

  // All-time stats
  const { data: allFilled } = await db
    .from("trades")
    .select("pnl_dollars, amount_dollars, status")
    .eq("status", "filled");

  const filled = allFilled ?? [];
  const totalPnl = filled.reduce((s, r) => s + (r.pnl_dollars ?? 0), 0);
  const totalWagered = filled.reduce((s, r) => s + r.amount_dollars, 0);
  const wins = filled.filter((r) => (r.pnl_dollars ?? 0) > 0).length;
  const winRate = filled.length > 0 ? wins / filled.length : 0;
  const avgWin = wins > 0 ? filled.filter((r) => (r.pnl_dollars ?? 0) > 0).reduce((s, r) => s + (r.pnl_dollars ?? 0), 0) / wins : 0;
  const lossCount = filled.filter((r) => (r.pnl_dollars ?? 0) < 0).length;
  const avgLoss = lossCount > 0 ? Math.abs(filled.filter((r) => (r.pnl_dollars ?? 0) < 0).reduce((s, r) => s + (r.pnl_dollars ?? 0), 0) / lossCount) : 0;

  const COINS = ["all", "BTC", "ETH", "SOL"];
  const RESULTS = ["all", "wins", "losses", "pending"];

  return (
    <div style={{ padding: "28px 32px" }}>
      <div className="page-header">
        <h1 className="page-title">Trades</h1>
        <p className="page-subtitle">{count ?? 0} total trades</p>
      </div>

      {/* Stats */}
      <div className="stats-grid" style={{ marginBottom: 24 }}>
        {[
          { label: "Total P&L", value: `${totalPnl >= 0 ? "+" : ""}$${fmt(totalPnl)}`, color: totalPnl >= 0 ? "#00C076" : "#ef4444" },
          { label: "Win Rate", value: pct(winRate), color: winRate >= 0.5 ? "#00C076" : "#ef4444" },
          { label: "Total Wagered", value: `$${fmt(totalWagered)}`, color: "#f0f4f8" },
          { label: "Avg Win", value: `$${fmt(avgWin)}`, color: "#00C076" },
          { label: "Avg Loss", value: `$${fmt(avgLoss)}`, color: "#ef4444" },
          { label: "Filled", value: String(filled.length), color: "#8899aa" },
        ].map((s) => (
          <div key={s.label} className="stat-card">
            <div style={{ fontSize: 11, color: "#4b6070", textTransform: "uppercase", letterSpacing: "0.06em", marginBottom: 8 }}>{s.label}</div>
            <div style={{ fontSize: 20, fontWeight: 700, fontFamily: "JetBrains Mono, monospace", color: s.color }}>{s.value}</div>
          </div>
        ))}
      </div>

      {/* Filters */}
      <div style={{ display: "flex", gap: 8, marginBottom: 16, flexWrap: "wrap" }}>
        <div style={{ display: "flex", gap: 6 }}>
          {COINS.map((c) => {
            const active = (coin ?? "all") === c;
            return (
              <a key={c} href={`/trades?coin=${c}&result=${result ?? "all"}`} style={{
                padding: "5px 12px", borderRadius: 6, fontSize: 12, fontWeight: 600,
                textDecoration: "none",
                background: active ? "rgba(0,192,118,0.15)" : "#0d1320",
                color: active ? "#00C076" : "#4b6070",
                border: `1px solid ${active ? "rgba(0,192,118,0.35)" : "#1e2a3a"}`,
              }}>{c === "all" ? "All Coins" : c}</a>
            );
          })}
        </div>
        <div style={{ display: "flex", gap: 6 }}>
          {RESULTS.map((r) => {
            const active = (result ?? "all") === r;
            return (
              <a key={r} href={`/trades?coin=${coin ?? "all"}&result=${r}`} style={{
                padding: "5px 12px", borderRadius: 6, fontSize: 12, fontWeight: 600,
                textDecoration: "none", textTransform: "capitalize",
                background: active ? "rgba(0,192,118,0.15)" : "#0d1320",
                color: active ? "#00C076" : "#4b6070",
                border: `1px solid ${active ? "rgba(0,192,118,0.35)" : "#1e2a3a"}`,
              }}>{r}</a>
            );
          })}
        </div>
      </div>

      {/* Table */}
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Market</th>
              <th>Side</th>
              <th>Contracts</th>
              <th>Entry</th>
              <th>Amount</th>
              <th>P&L</th>
              <th>Result</th>
              <th>Status</th>
              <th>Date</th>
            </tr>
          </thead>
          <tbody>
            {trades.length === 0 ? (
              <tr>
                <td colSpan={9} style={{ textAlign: "center", color: "#4b6070", padding: "48px 0" }}>
                  No trades found
                </td>
              </tr>
            ) : (
              trades.map((t) => {
                const pnl = t.pnl_dollars;
                const isPending = t.status === "pending";
                return (
                  <tr key={t.id} style={{
                    background: isPending ? "rgba(59,130,246,0.03)" : pnl !== null && pnl > 0 ? "rgba(0,192,118,0.03)" : pnl !== null && pnl < 0 ? "rgba(239,68,68,0.03)" : "transparent",
                  }}>
                    <td>
                      <div style={{ color: "#f0f4f8", fontSize: 12, maxWidth: 260, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                        {t.market_title}
                      </div>
                      <div style={{ fontSize: 10, color: "#2a3a4a", marginTop: 2, fontFamily: "JetBrains Mono, monospace" }}>{t.ticker}</div>
                    </td>
                    <td>
                      <span className={`badge ${t.side === "yes" ? "badge-green" : "badge-red"}`}>{t.side.toUpperCase()}</span>
                    </td>
                    <td className="mono">{t.filled_contracts ?? t.contracts}</td>
                    <td className="mono">{pct(t.price_cents / 100)}</td>
                    <td className="mono">${fmt(t.amount_dollars)}</td>
                    <td>
                      {isPending ? (
                        <span className="value-pending">—</span>
                      ) : pnl !== null ? (
                        <span className={pnl >= 0 ? "value-profit" : "value-loss"}>
                          {pnl >= 0 ? "+" : ""}${fmt(Math.abs(pnl))}
                        </span>
                      ) : (
                        <span style={{ color: "#4b6070" }}>—</span>
                      )}
                    </td>
                    <td>
                      {t.resolution ? (
                        <span className={`badge ${t.resolution === "yes" ? "badge-green" : "badge-red"}`}>
                          {t.resolution.toUpperCase()}
                        </span>
                      ) : <span style={{ color: "#2a3a4a" }}>—</span>}
                    </td>
                    <td>
                      <span className={`badge ${isPending ? "badge-blue" : t.status === "filled" ? "badge-green" : "badge-gray"}`}>
                        {t.status.toUpperCase()}
                      </span>
                    </td>
                    <td style={{ color: "#4b6070", fontSize: 11 }}>
                      {new Date(t.created_at).toLocaleDateString("en-US", { timeZone: "America/New_York" })}<br />
                      <span style={{ color: "#2a3a4a" }}>{new Date(t.created_at).toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", timeZone: "America/New_York" })} ET</span>
                    </td>
                  </tr>
                );
              })
            )}
          </tbody>
        </table>
      </div>

      {totalPages > 1 && (
        <div style={{ display: "flex", gap: 8, marginTop: 20, justifyContent: "center" }}>
          {page > 1 && (
            <a href={`/trades?page=${page - 1}&coin=${coin ?? "all"}&result=${result ?? "all"}`} className="btn btn-ghost">← Prev</a>
          )}
          <span style={{ padding: "8px 16px", color: "#4b6070", fontSize: 13 }}>{page} / {totalPages}</span>
          {page < totalPages && (
            <a href={`/trades?page=${page + 1}&coin=${coin ?? "all"}&result=${result ?? "all"}`} className="btn btn-ghost">Next →</a>
          )}
        </div>
      )}
    </div>
  );
}
