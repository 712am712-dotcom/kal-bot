/**
 * /positions — Open positions.
 * Shows all trades with status pending or partial.
 */
import { createSupabaseServerClient } from "@/lib/supabase";
import type { Trade } from "@/lib/types";

export const revalidate = 30;

function pct(n: number) {
  return `${(n * 100).toFixed(1)}%`;
}

export default async function PositionsPage() {
  const db = createSupabaseServerClient();

  const { data } = await db
    .from("trades")
    .select("*")
    .in("status", ["pending", "partial"])
    .order("created_at", { ascending: false });

  const positions: Trade[] = data ?? [];
  const totalExposure = positions.reduce((sum, p) => sum + p.amount_dollars, 0);

  return (
    <div style={{ padding: "28px 32px" }}>
      <div className="page-header">
        <h1 className="page-title">Open Positions</h1>
        <p className="page-subtitle">{positions.length} open · ${totalExposure.toFixed(2)} total exposure</p>
      </div>

      {positions.length === 0 ? (
        <div className="card" style={{ textAlign: "center", color: "#4b6070", padding: "64px 0", fontSize: 14 }}>
          No open positions
        </div>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Market</th>
                <th>Side</th>
                <th>Contracts</th>
                <th>Entry Price</th>
                <th>Cost</th>
                <th>Status</th>
                <th>Mode</th>
                <th>Opened</th>
              </tr>
            </thead>
            <tbody>
              {positions.map((p) => (
                <tr key={p.id}>
                  <td>
                    <div style={{ color: "#f0f4f8", fontSize: 12, maxWidth: 300, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                      {p.market_title}
                    </div>
                    <div style={{ fontSize: 10, color: "#2a3a4a", marginTop: 2, fontFamily: "JetBrains Mono, monospace" }}>{p.ticker}</div>
                  </td>
                  <td>
                    <span className={`badge ${p.side === "yes" ? "badge-green" : "badge-red"}`}>{p.side.toUpperCase()}</span>
                  </td>
                  <td className="mono">{p.contracts}</td>
                  <td className="mono">{pct(p.price_cents / 100)}</td>
                  <td style={{ color: "#f0f4f8" }} className="mono">${p.amount_dollars.toFixed(2)}</td>
                  <td>
                    <span className={`badge ${p.status === "partial" ? "badge-yellow" : "badge-blue"}`}>
                      {p.status.toUpperCase()}
                    </span>
                  </td>
                  <td>
                    <span className={`badge ${p.demo_mode ? "badge-yellow" : "badge-green"}`}>
                      {p.demo_mode ? "DEMO" : "LIVE"}
                    </span>
                  </td>
                  <td style={{ color: "#4b6070", fontSize: 11 }}>
                    {new Date(p.created_at).toLocaleString()}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {positions.length > 0 && (
        <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 14, marginTop: 20 }}>
          {[
            { label: "Total Exposure", value: `$${totalExposure.toFixed(2)}` },
            { label: "YES Exposure", value: `$${positions.filter((p) => p.side === "yes").reduce((s, p) => s + p.amount_dollars, 0).toFixed(2)}` },
            { label: "NO Exposure", value: `$${positions.filter((p) => p.side === "no").reduce((s, p) => s + p.amount_dollars, 0).toFixed(2)}` },
          ].map((s) => (
            <div key={s.label} className="stat-card">
              <div style={{ fontSize: 11, color: "#4b6070", textTransform: "uppercase", letterSpacing: "0.06em", marginBottom: 6 }}>{s.label}</div>
              <div style={{ fontSize: 20, fontWeight: 700, fontFamily: "JetBrains Mono, monospace", color: "#f0f4f8" }}>{s.value}</div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
