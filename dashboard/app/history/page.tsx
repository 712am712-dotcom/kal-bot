/**
 * /history — Completed trade history with P&L.
 */
import { createSupabaseServerClient } from "@/lib/supabase";
import type { Trade } from "@/lib/types";

export const revalidate = 60;

function pct(n: number) {
  return `${(n * 100).toFixed(1)}%`;
}

export default async function HistoryPage({
  searchParams,
}: {
  searchParams: { page?: string; side?: string };
}) {
  const db = createSupabaseServerClient();
  const page = Number(searchParams.page ?? 1);
  const pageSize = 30;
  const offset = (page - 1) * pageSize;

  let query = db
    .from("trades")
    .select("*", { count: "exact" })
    .in("status", ["filled", "cancelled", "failed"])
    .order("created_at", { ascending: false })
    .range(offset, offset + pageSize - 1);

  if (searchParams.side && searchParams.side !== "all") {
    query = query.eq("side", searchParams.side);
  }

  const { data, count } = await query;
  const trades: Trade[] = data ?? [];
  const totalPages = Math.ceil((count ?? 0) / pageSize);

  const { data: allFilled } = await db
    .from("trades")
    .select("pnl_dollars, amount_dollars")
    .eq("status", "filled");

  const filled = allFilled ?? [];
  const totalPnl = filled.reduce((s, r) => s + (r.pnl_dollars ?? 0), 0);
  const totalWagered = filled.reduce((s, r) => s + r.amount_dollars, 0);
  const wins = filled.filter((r) => (r.pnl_dollars ?? 0) > 0).length;
  const winRate = filled.length > 0 ? wins / filled.length : 0;

  return (
    <div style={{ padding: "28px 32px" }}>
      <div className="page-header">
        <h1 className="page-title">Trade History</h1>
        <p className="page-subtitle">{count ?? 0} completed trades</p>
      </div>

      <div className="stats-grid" style={{ marginBottom: 24 }}>
        {[
          { label: "Total P&L", value: `${totalPnl >= 0 ? "+" : ""}$${totalPnl.toFixed(2)}`, color: totalPnl >= 0 ? "#00C076" : "#ef4444" },
          { label: "Total Wagered", value: `$${totalWagered.toFixed(2)}`, color: "#f0f4f8" },
          { label: "Win Rate", value: pct(winRate), color: winRate >= 0.5 ? "#00C076" : "#ef4444" },
          { label: "Filled Trades", value: String(filled.length), color: "#f0f4f8" },
        ].map((s) => (
          <div key={s.label} className="stat-card">
            <div style={{ fontSize: 11, color: "#4b6070", textTransform: "uppercase", letterSpacing: "0.06em", marginBottom: 8 }}>{s.label}</div>
            <div style={{ fontSize: 22, fontWeight: 700, fontFamily: "JetBrains Mono, monospace", color: s.color }}>{s.value}</div>
          </div>
        ))}
      </div>

      <div style={{ display: "flex", gap: 8, marginBottom: 16 }}>
        {["all", "yes", "no"].map((s) => (
          <a key={s} href={`/history?side=${s}`} style={{
            padding: "5px 12px", borderRadius: 6, fontSize: 12, fontWeight: 600,
            textDecoration: "none", textTransform: "uppercase",
            background: (searchParams.side ?? "all") === s ? "rgba(0,192,118,0.15)" : "#0d1320",
            color: (searchParams.side ?? "all") === s ? "#00C076" : "#4b6070",
            border: `1px solid ${(searchParams.side ?? "all") === s ? "rgba(0,192,118,0.35)" : "#1e2a3a"}`,
          }}>
            {s === "all" ? "All" : s.toUpperCase()}
          </a>
        ))}
      </div>

      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Market</th>
              <th>Side</th>
              <th>Contracts</th>
              <th>Price</th>
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
                  No completed trades yet
                </td>
              </tr>
            ) : (
              trades.map((t) => (
                <tr key={t.id}>
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
                  <td className="mono">${t.amount_dollars.toFixed(2)}</td>
                  <td>
                    <span className={t.pnl_dollars !== null && t.pnl_dollars >= 0 ? "value-profit" : t.pnl_dollars !== null ? "value-loss" : "value-neutral"}>
                      {t.pnl_dollars !== null ? `${t.pnl_dollars >= 0 ? "+" : ""}$${t.pnl_dollars.toFixed(2)}` : "—"}
                    </span>
                  </td>
                  <td>
                    {t.resolution ? (
                      <span className={`badge ${t.resolution === "yes" ? "badge-green" : t.resolution === "no" ? "badge-red" : "badge-gray"}`}>
                        {t.resolution.toUpperCase()}
                      </span>
                    ) : <span style={{ color: "#2a3a4a" }}>—</span>}
                  </td>
                  <td>
                    <span className={`badge ${t.status === "filled" ? "badge-green" : t.status === "cancelled" ? "badge-gray" : "badge-red"}`}>
                      {t.status.toUpperCase()}
                    </span>
                  </td>
                  <td style={{ color: "#4b6070", fontSize: 11 }}>
                    {new Date(t.created_at).toLocaleDateString()}
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>

      {totalPages > 1 && (
        <div style={{ display: "flex", gap: 8, marginTop: 20, justifyContent: "center" }}>
          {page > 1 && (
            <a href={`/history?page=${page - 1}`} className="btn btn-ghost">← Prev</a>
          )}
          <span style={{ padding: "8px 16px", color: "#4b6070", fontSize: 13 }}>{page} / {totalPages}</span>
          {page < totalPages && (
            <a href={`/history?page=${page + 1}`} className="btn btn-ghost">Next →</a>
          )}
        </div>
      )}
    </div>
  );
}
