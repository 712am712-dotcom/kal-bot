/**
 * /feed — Live stream of all AI decisions.
 * Server component with 15s revalidation.
 */
import { createSupabaseServerClient } from "@/lib/supabase";
import type { AiDecision } from "@/lib/types";

export const revalidate = 15;

function pct(n: number) { return `${(n * 100).toFixed(1)}%`; }
function ago(dateStr: string) {
  const diff = Date.now() - new Date(dateStr).getTime();
  const m = Math.floor(diff / 60000);
  if (m < 1) return "just now";
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.floor(h / 24)}d ago`;
}

const REC_LABEL: Record<string, string> = {
  BUY_YES: "BUY YES",
  BUY_NO:  "BUY NO",
  SKIP:    "SKIP",
};

export default async function FeedPage({
  searchParams,
}: {
  searchParams: { rec?: string; page?: string };
}) {
  const db = createSupabaseServerClient();
  const page = Number(searchParams.page ?? 1);
  const pageSize = 40;
  const offset = (page - 1) * pageSize;

  let q = db
    .from("ai_decisions")
    .select("*", { count: "exact" })
    .order("created_at", { ascending: false })
    .range(offset, offset + pageSize - 1);

  if (searchParams.rec && searchParams.rec !== "all") {
    q = q.eq("recommendation", searchParams.rec.toUpperCase());
  }

  const { data, count } = await q;
  const decisions: AiDecision[] = data ?? [];
  const totalPages = Math.ceil((count ?? 0) / pageSize);

  // Quick stats
  const { data: statsData } = await db
    .from("ai_decisions")
    .select("recommendation")
    .order("created_at", { ascending: false })
    .limit(200);

  const stats = statsData ?? [];
  const buyCount = stats.filter((d) => d.recommendation !== "SKIP").length;
  const skipCount = stats.filter((d) => d.recommendation === "SKIP").length;
  const buyRate = stats.length > 0 ? buyCount / stats.length : 0;

  return (
    <div style={{ padding: "28px 32px" }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 24 }}>
        <div>
          <h1 style={{ fontSize: 20, fontWeight: 700, color: "#f0f4f8", marginBottom: 4 }}>Live Feed</h1>
          <p style={{ color: "#4b6070", fontSize: 13 }}>Every market Kal analyzed — real time</p>
        </div>
        <div style={{ display: "flex", gap: 12, fontSize: 13, color: "#4b6070" }}>
          <span><span style={{ color: "#00C076", fontWeight: 600 }}>{buyCount}</span> calls</span>
          <span><span style={{ color: "#4b6070", fontWeight: 600 }}>{skipCount}</span> skips</span>
          <span><span style={{ color: "#f0f4f8", fontWeight: 600 }}>{pct(buyRate)}</span> action rate</span>
        </div>
      </div>

      {/* Filters */}
      <div style={{ display: "flex", gap: 8, marginBottom: 20 }}>
        {["all", "BUY_YES", "BUY_NO", "SKIP"].map((r) => {
          const active = (searchParams.rec ?? "all") === r;
          return (
            <a key={r} href={`/feed?rec=${r}`} style={{
              padding: "6px 14px", borderRadius: 6, fontSize: 12, fontWeight: 600,
              textDecoration: "none", textTransform: "capitalize",
              background: active ? "rgba(0,192,118,0.15)" : "#0d1320",
              color: active ? "#00C076" : "#4b6070",
              border: `1px solid ${active ? "rgba(0,192,118,0.35)" : "#1e2a3a"}`,
            }}>
              {r === "all" ? "All" : REC_LABEL[r] ?? r}
            </a>
          );
        })}
      </div>

      {/* Feed cards */}
      <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
        {decisions.length === 0 ? (
          <div className="card" style={{ textAlign: "center", color: "#4b6070", padding: "48px 0" }}>
            No decisions match this filter
          </div>
        ) : (
          decisions.map((d) => {
            const isSkip = d.recommendation === "SKIP";
            const isBuyYes = d.recommendation === "BUY_YES";
            const edge = d.edge ?? 0;
            const conf = d.confidence ?? 0;
            const kalProb = d.claude_yes_probability ?? 0;
            const kalshiPrice = d.kalshi_yes_price ?? 0;
            return (
              <div key={d.id} style={{
                background: "#111827", border: `1px solid ${isSkip ? "#1e2a3a" : isBuyYes ? "rgba(0,192,118,0.2)" : "rgba(239,68,68,0.2)"}`,
                borderRadius: 10, padding: "14px 16px",
                borderLeft: `3px solid ${isSkip ? "#1e2a3a" : isBuyYes ? "#00C076" : "#ef4444"}`,
              }}>
                <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: 16 }}>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 6, flexWrap: "wrap" }}>
                      <span className={`badge ${isSkip ? "badge-gray" : isBuyYes ? "badge-green" : "badge-red"}`}>
                        {REC_LABEL[d.recommendation ?? "SKIP"] ?? d.recommendation}
                      </span>
                      <span style={{ fontSize: 13, fontWeight: 600, color: "#f0f4f8" }}>
                        {d.ticker}
                      </span>
                      {d.decision_mode && (
                        <span className="badge badge-gray" style={{ fontSize: 10 }}>{d.decision_mode}</span>
                      )}
                    </div>
                    <div style={{ fontSize: 12, color: "#8899aa", marginBottom: 8, lineHeight: 1.4 }}>
                      {d.market_title ?? "—"}
                    </div>
                    <div style={{ display: "flex", gap: 18, fontSize: 12, flexWrap: "wrap" }}>
                      <span style={{ color: "#4b6070" }}>
                        Edge: <span style={{ color: edge > 0 ? "#00C076" : "#ef4444", fontWeight: 600 }}>{pct(edge)}</span>
                      </span>
                      <span style={{ color: "#4b6070" }}>
                        Conf: <span style={{ color: conf >= 0.5 ? "#f0f4f8" : "#8899aa", fontWeight: 600 }}>{pct(conf)}</span>
                      </span>
                      <span style={{ color: "#4b6070" }}>
                        Kal: <span style={{ color: "#60a5fa", fontWeight: 600 }}>{pct(kalProb)}</span>
                      </span>
                      <span style={{ color: "#4b6070" }}>
                        Crowd: <span style={{ color: "#8899aa", fontWeight: 600 }}>{pct(kalshiPrice)}</span>
                      </span>
                    </div>
                    {d.reasoning && !isSkip && (
                      <div style={{ fontSize: 11, color: "#4b6070", marginTop: 8, fontStyle: "italic", lineHeight: 1.5, maxWidth: 640 }}>
                        &ldquo;{d.reasoning.split("|")[0].trim().slice(0, 200)}{d.reasoning.length > 200 ? "…" : ""}&rdquo;
                      </div>
                    )}
                  </div>
                  <div style={{ flexShrink: 0, textAlign: "right" }}>
                    <div style={{ fontSize: 11, color: "#4b6070" }}>
                      {new Date(d.created_at).toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", timeZone: "America/New_York" })} ET
                    </div>
                    <div style={{ fontSize: 10, color: "#2a3a4a", marginTop: 2 }}>
                      {new Date(d.created_at).toLocaleDateString("en-US", { timeZone: "America/New_York" })}
                    </div>
                  </div>
                </div>
              </div>
            );
          })
        )}
      </div>

      {/* Pagination */}
      {totalPages > 1 && (
        <div style={{ display: "flex", gap: 8, marginTop: 24, justifyContent: "center" }}>
          {page > 1 && (
            <a href={`/feed?rec=${searchParams.rec ?? "all"}&page=${page - 1}`} className="btn btn-ghost">← Prev</a>
          )}
          <span style={{ padding: "8px 16px", color: "#4b6070", fontSize: 13 }}>{page} / {totalPages}</span>
          {page < totalPages && (
            <a href={`/feed?rec=${searchParams.rec ?? "all"}&page=${page + 1}`} className="btn btn-ghost">Next →</a>
          )}
        </div>
      )}
    </div>
  );
}
