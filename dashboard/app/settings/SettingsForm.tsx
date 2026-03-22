"use client";
import { useState } from "react";

interface Props { config: Record<string, string>; }

const RISK_FIELDS = [
  { key: "max_bet_dollars", label: "Max Bet Per Trade ($)", type: "number", min: 1, max: 1000, step: 1, desc: "Max dollars risked on any single trade." },
  { key: "daily_loss_limit_dollars", label: "Daily Loss Limit ($)", type: "number", min: 1, max: 10000, step: 1, desc: "Bot stops trading after this dollar loss per day." },
  { key: "min_confidence", label: "Min Confidence (%)", type: "number", min: 30, max: 99, step: 1, scale: 100, desc: "Min Kal confidence to place a trade." },
  { key: "min_edge", label: "Min Edge Required (%)", type: "number", min: 1, max: 50, step: 1, scale: 100, desc: "Min edge between Kal estimate and crowd price." },
  { key: "volume_floor", label: "Volume Floor ($)", type: "number", min: 0, max: 5000, step: 25, desc: "Skip markets below this volume before calling Claude. Lower = more analysis calls." },
  { key: "min_liquidity_dollars", label: "Min Market Liquidity ($)", type: "number", min: 0, max: 100000, step: 100, desc: "Skip markets with less volume than this." },
  { key: "max_open_positions", label: "Max Open Positions", type: "number", min: 1, max: 50, step: 1, desc: "Do not open new positions beyond this count." },
];

const TOGGLE_FIELDS = [
  { key: "demo_mode", label: "Demo Mode", desc: "ON = no real orders. OFF = real capital at risk.", danger: true },
  { key: "research_mode", label: "Research Mode", desc: "ON = analyze only, no orders. OFF = full trading loop.", danger: false },
  { key: "paper_trading", label: "Paper Trading", desc: "ON = paper mode with lower thresholds. Real market data.", danger: false },
];

export default function SettingsForm({ config }: Props) {
  const [values, setValues] = useState<Record<string, string>>(config);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const getNum = (key: string, scale = 1) => {
    const raw = values[key];
    if (!raw) return "";
    return String(parseFloat(raw) * scale);
  };
  const setNum = (key: string, val: string, scale = 1) => {
    setValues((p) => ({ ...p, [key]: scale === 1 ? val : String(parseFloat(val) / scale) }));
  };
  const getBool = (key: string) => values[key] !== "false";
  const setBool = (key: string, val: boolean) => setValues((p) => ({ ...p, [key]: val ? "true" : "false" }));

  async function handleSave() {
    setSaving(true); setError(null);
    try {
      const res = await fetch("/api/config", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ config: values }),
      });
      if (!res.ok) throw new Error(await res.text());
      setSaved(true);
      setTimeout(() => setSaved(false), 2500);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Unknown error");
    } finally { setSaving(false); }
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
      {/* Risk Controls */}
      <div className="card">
        <div style={{ fontSize: 13, fontWeight: 600, color: "#f0f4f8", marginBottom: 18, paddingBottom: 14, borderBottom: "1px solid #1e2a3a" }}>
          Risk Controls
        </div>
        <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
          {RISK_FIELDS.map((f) => (
            <div key={f.key}>
              <label style={{ fontSize: 12, fontWeight: 600, color: "#8899aa", display: "block", marginBottom: 6 }}>{f.label}</label>
              <input
                className="input" type="number" min={f.min} max={f.max} step={f.step}
                value={getNum(f.key, f.scale ?? 1)}
                onChange={(e) => setNum(f.key, e.target.value, f.scale ?? 1)}
                style={{ maxWidth: 200 }}
              />
              <p style={{ fontSize: 11, color: "#4b6070", marginTop: 4 }}>{f.desc}</p>
            </div>
          ))}
        </div>
      </div>

      {/* Mode Toggles */}
      <div className="card">
        <div style={{ fontSize: 13, fontWeight: 600, color: "#f0f4f8", marginBottom: 18, paddingBottom: 14, borderBottom: "1px solid #1e2a3a" }}>
          Mode Toggles
        </div>
        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          {TOGGLE_FIELDS.map((f) => {
            const isOn = getBool(f.key);
            const isDanger = f.danger && !isOn;
            return (
              <div key={f.key} className="toggle-wrap" style={{
                background: isDanger ? "rgba(239,68,68,0.06)" : "#0d1320",
                border: `1px solid ${isDanger ? "rgba(239,68,68,0.2)" : "#1e2a3a"}`,
              }}>
                <div>
                  <div style={{ fontSize: 13, fontWeight: 600, color: isDanger ? "#ef4444" : "#f0f4f8", marginBottom: 3 }}>
                    {f.label}
                    {isDanger && <span style={{ marginLeft: 8, fontSize: 10, background: "rgba(239,68,68,0.15)", color: "#ef4444", padding: "2px 7px", borderRadius: 4, fontWeight: 700 }}>⚠ REAL MONEY</span>}
                  </div>
                  <p style={{ fontSize: 11, color: "#4b6070" }}>{f.desc}</p>
                </div>
                <label className="toggle" style={{ marginLeft: 20 }}>
                  <input type="checkbox" checked={isOn} onChange={(e) => setBool(f.key, e.target.checked)} />
                  <span className="toggle-slider" />
                </label>
              </div>
            );
          })}
        </div>
      </div>

      {/* Save */}
      <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
        <button className="btn btn-primary" onClick={handleSave} disabled={saving} style={{ minWidth: 130 }}>
          {saving ? "Saving…" : "Save Settings"}
        </button>
        {saved && <span style={{ fontSize: 13, color: "#00C076", fontWeight: 600 }}>✓ Saved</span>}
        {error && <span style={{ fontSize: 13, color: "#ef4444" }}>Error: {error}</span>}
      </div>
    </div>
  );
}
