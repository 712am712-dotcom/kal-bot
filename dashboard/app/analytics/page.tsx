/**
 * /analytics — Performance charts and breakdowns.
 */
import { createSupabaseServerClient } from "@/lib/supabase";
import type { DailySummary, Trade } from "@/lib/types";
import PnlChart from "./PnlChart";
import TradesBarChart from "./TradesBarChart";

export const revalidate = 300;
const STARTING_BALANCE = 500;

function pct(n: number) { return `${(n * 100).toFixed(1)}%`; }
function fmt(n: number) { return n.toFixed(2); }

export default async function AnalyticsPage() {
  const db = createSupabaseServerClient();

  const [summariesResp, tradesResp, decisionsResp] = await Promise.all([
    db.from("daily_summary").select("*").order("date", { ascending: true }).limit(90),
    db.from("trades").select("*").order("created_at", { ascending: true }),
    db.from("ai_decisions").select("recommendation, edge, confidence, ticker").order("created_at", { ascending: false }).limit(500),
  ]);

  const summaries: DailySummary[] = summariesResp.data ?? [];
  const trades: Trade[] = tradesResp.data ?? [];
  const decisions = decisionsResp.data ?? [];

  const filled = trades.filter((t) => t.status === "filled");
  const wins = filled.filter((t) => (t.pnl_dollars ?? 0) > 0);
  const losses = filled.filter((t) => (t.pnl_dollars ?? 0) < 0);
  const totalPnl = filled.reduce((s, t) => s + (t.pnl_dollars ?? 0), 0);
  const winRate = filled.length > 0 ? wins.length / filled.length : 0;
  const actionDecisions = decisions.filter((d) => d.recommendation !== "SKIP");
  const avgEdge = actionDecisions.length > 0
    ? actionDecisions.reduce((s, d) => s + (d.edge ?? 0), 0) / actionDecisions.length
    : 0;

  const bestTrade = filled.length > 0
    ? filled.reduce((best, t) => (t.pnl_dollars ?? -Infinity) > (best.pnl_dollars ?? -Infinity) ? t : best, filled[0] as Trade)
    : undefined;
  const worstTrade = filled.length > 0
    ? filled.reduce((worst, t) => (t.pnl_dollars ?? Infinity) < (worst.pnl_dollars ?? Infinity) ? t : worst, filled[0] as Trade)
    : undefined;

  const avgWinAmt = wins.length > 0 ? wins.reduce((s, t) => s + (t.pnl_dollars ?? 0), 0) / wins.length : 0;
  const avgLossAmt = losses.length > 0 ? Math.abs(losses.reduce((s, t) => s + (t.pnl_dollars ?? 0), 0) / losses.length) : 0;
  const profitFactor = avgLossAmt > 0 ? avgWinAmt / avgLossAmt : 0;

  // Cumulative P&L chart data
  let cum = STARTING_BALANCE;
  const pnlChartData = summaries.map((s) => {
    cum += s.net_pnl_dollars ?? 0;
    return {
      date: s.date.slice(5), // MM-DD
      cumPnl: cum - STARTING_BALANCE,
      balance: cum,
    };
  });

  // Trades per day (last 14)
  const tradesByDay = summaries.slice(-14).map((s) => ({
    date: s.date.slice(5),
    trades: s.trades_placed ?? 0,
    wins: s.trades_filled ?? 0,
    losses: Math.max(0, (s.trades_placed ?? 0) - (s.trades_filled ?? 0)),
  }));

  // Coin breakdown
  const coins = ["BTC", "ETH", "SOL"];
  const coinStats = coins.map((coin) => {
    const coinTrades = filled.filter((t) => t.ticker.includes(coin));
    const coinWins = coinTrades.filter((t) => (t.pnl_dollars ?? 0) > 0).length;
    const coinPnl = coinTrades.reduce((s, t) => s + (t.pnl_dollars ?? 0), 0);
    return {
      coin,
      trades: coinTrades.length,
      winRate: coinTrades.length > 0 ? coinWins / coinTrades.length : 0,
      pnl: coinPnl,
    };
  });

  return (
    <div style={{ padding: "28px 32px" }}>
      <div className="page-header">
        <h1 className="page-title">Analytics</h1>
        <p className="page-subtitle">Performance deep-dive</p>
      </div>

      {/* Key metrics */}
      <div className="stats-grid" style={{ marginBottom: 24 }}>
        {[
          { label: "Total P&L", value: `${totalPnl >= 0 ? "+" : ""}$${fmt(totalPnl)}`, color: totalPnl >= 0 ? "#00C076" : "#ef4444" },
          { label: "Win Rate", value: pct(winRate), color: winRate >= 0.5 ? "#00C076" : "#ef4444" },
          { label: "Avg Edge Captured", value: pct(avgEdge), color: avgEdge > 0 ? "#00C076" : "#ef4444" },
          { label: "Profit Factor", value: fmt(profitFactor), color: profitFactor >= 1.5 ? "#00C076" : profitFactor >= 1 ? "#f59e0b" : "#ef4444" },
          { label: "Avg Win", value: `$${fmt(avgWinAmt)}`, color: "#00C076" },
          { label: "Avg Loss", value: `$${fmt(avgLossAmt)}`, color: "#ef4444" },
        ].map((s) => (
          <div key={s.label} className="stat-card">
            <div style={{ fontSize: 11, color: "#4b6070", textTransform: "uppercase", letterSpacing: "0.06em", marginBottom: 8 }}>{s.label}</div>
            <div style={{ fontSize: 20, fontWeight: 700, fontFamily: "JetBrains Mono, monospace", color: s.color }}>{s.value}</div>
          </div>
        ))}
      </div>

      {/* Balance chart */}
      <div className="card" style={{ marginBottom: 20 }}>
        <div className="section-title">Balance Over Time</div>
        <PnlChart data={pnlChartData} startBalance={STARTING_BALANCE} />
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 20, marginBottom: 20 }}>
        {/* Trades per day */}
        <div className="card">
          <div className="section-title">Trades Per Day (last 14d)</div>
          <TradesBarChart data={tradesByDay} />
        </div>

        {/* Coin breakdown */}
        <div className="card">
          <div className="section-title">Win Rate by Coin</div>
          <div style={{ display: "flex", flexDirection: "column", gap: 12, marginTop: 8 }}>
            {coinStats.map((c) => (
              <div key={c.coin}>
                <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 5, fontSize: 13 }}>
                  <span style={{ color: "#f0f4f8", fontWeight: 600 }}>{c.coin}</span>
                  <span style={{ color: c.winRate >= 0.5 ? "#00C076" : "#ef4444", fontFamily: "JetBrains Mono, monospace", fontSize: 12 }}>
                    {pct(c.winRate)} · {c.trades} trades · ${fmt(c.pnl)}
                  </span>
                </div>
                <div style={{ height: 6, background: "#0d1320", borderRadius: 3, overflow: "hidden" }}>
                  <div style={{ height: "100%", width: `${c.winRate * 100}%`, background: c.winRate >= 0.5 ? "#00C076" : "#ef4444", borderRadius: 3, transition: "width 0.5s" }} />
                </div>
              </div>
            ))}
          </div>
        </div>
      </div>

      {/* Best / worst trades */}
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 20 }}>
        <div className="card">
          <div className="section-title">Best Trade</div>
          {bestTrade ? (
            <div>
              <div style={{ fontSize: 28, fontWeight: 700, color: "#00C076", fontFamily: "JetBrains Mono, monospace", marginBottom: 6 }}>
                +${fmt(bestTrade.pnl_dollars ?? 0)}
              </div>
              <div style={{ fontSize: 12, color: "#8899aa", marginBottom: 4 }}>{bestTrade.market_title}</div>
              <div style={{ fontSize: 11, color: "#4b6070" }}>
                {bestTrade.side.toUpperCase()} · {bestTrade.contracts} contracts · {new Date(bestTrade.created_at).toLocaleDateString("en-US", { timeZone: "America/New_York" })}
              </div>
            </div>
          ) : <div style={{ color: "#4b6070", fontSize: 13 }}>No resolved trades yet</div>}
        </div>
        <div className="card">
          <div className="section-title">Worst Trade</div>
          {worstTrade && (worstTrade.pnl_dollars ?? 0) < 0 ? (
            <div>
              <div style={{ fontSize: 28, fontWeight: 700, color: "#ef4444", fontFamily: "JetBrains Mono, monospace", marginBottom: 6 }}>
                -${fmt(Math.abs(worstTrade.pnl_dollars ?? 0))}
              </div>
              <div style={{ fontSize: 12, color: "#8899aa", marginBottom: 4 }}>{worstTrade.market_title}</div>
              <div style={{ fontSize: 11, color: "#4b6070" }}>
                {worstTrade.side.toUpperCase()} · {worstTrade.contracts} contracts · {new Date(worstTrade.created_at).toLocaleDateString("en-US", { timeZone: "America/New_York" })}
              </div>
            </div>
          ) : <div style={{ color: "#4b6070", fontSize: 13 }}>No losing trades yet</div>}
        </div>
      </div>
    </div>
  );
}
