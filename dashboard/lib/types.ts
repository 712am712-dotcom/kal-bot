// ── Database Row Types ───────────────────────────────────────────────────────
// Mirror of supabase/schema.sql — replace with generated types after deploy.

export type ScanMode = "research" | "live";
export type Recommendation = "BUY_YES" | "BUY_NO" | "SKIP";
export type TradeStatus = "pending" | "filled" | "partial" | "cancelled" | "failed";
export type Resolution = "yes" | "no" | "void";

export interface MarketLog {
  id: string;
  market_id: string;
  ticker: string;
  title: string;
  category: string | null;
  yes_price: number;
  no_price: number;
  volume: number;
  open_interest: number;
  close_time: string | null;
  scanned_at: string;
  scan_mode: ScanMode;
}

export interface AiDecision {
  id: string;
  market_id: string;
  ticker: string;
  market_title: string;
  claude_yes_probability: number;
  kalshi_yes_price: number;
  edge: number;
  confidence: number;
  reasoning: string;
  recommendation: Recommendation;
  decision_mode: ScanMode;
  tokens_used: number;
  latency_ms: number;
  created_at: string;
}

export interface Trade {
  id: string;
  ai_decision_id: string | null;
  market_id: string;
  ticker: string;
  market_title: string;
  side: "yes" | "no";
  contracts: number;
  price_cents: number;
  amount_dollars: number;
  order_id: string | null;
  status: TradeStatus;
  filled_contracts: number;
  filled_price_cents: number | null;
  pnl_dollars: number | null;
  resolved_at: string | null;
  resolution: Resolution | null;
  demo_mode: boolean;
  created_at: string;
  updated_at: string;
}

export interface BotConfig {
  id: string;
  key: string;
  value: string;
  description: string | null;
  updated_at: string;
}

export interface DailySummary {
  id: string;
  date: string;
  markets_scanned: number;
  ai_calls_made: number;
  trades_placed: number;
  trades_filled: number;
  gross_pnl_dollars: number;
  net_pnl_dollars: number;
  total_wagered_dollars: number;
  win_rate: number | null;
  best_category: string | null;
  worst_category: string | null;
  demo_mode: boolean;
  created_at: string;
}

export interface StrategyReport {
  id: string;
  report_date: string;
  research_start: string | null;
  research_end: string | null;
  markets_analyzed: number;
  categories_analyzed: CategoryStat[] | null;
  top_categories: CategoryStat[] | null;
  recommended_bet_size: number;
  recommended_funding: number;
  overall_edge: number;
  summary: string;
  full_report: Record<string, unknown> | null;
  created_at: string;
}

export interface CategoryStat {
  category: string;
  sample_size: number;
  edge_avg: number;
  buy_yes_count: number;
  buy_no_count: number;
}
