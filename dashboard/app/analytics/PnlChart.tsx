"use client";
import { LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, ReferenceLine } from "recharts";

interface Point { date: string; cumPnl: number; balance: number; }
interface Props { data: Point[]; startBalance: number; }

export default function PnlChart({ data, startBalance }: Props) {
  if (data.length === 0) {
    return <div style={{ height: 220, display: "flex", alignItems: "center", justifyContent: "center", color: "#4b6070" }}>No data yet</div>;
  }
  return (
    <ResponsiveContainer width="100%" height={220}>
      <LineChart data={data} margin={{ top: 5, right: 10, left: 0, bottom: 0 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="#1e2a3a" />
        <XAxis dataKey="date" tick={{ fontSize: 10, fill: "#4b6070" }} tickLine={false} axisLine={false} />
        <YAxis tick={{ fontSize: 10, fill: "#4b6070" }} tickLine={false} axisLine={false}
          tickFormatter={(v: number) => `$${v.toFixed(0)}`} width={55} />
        <Tooltip
          contentStyle={{ background: "#111827", border: "1px solid #1e2a3a", borderRadius: 8, fontSize: 12 }}
          labelStyle={{ color: "#8899aa" }}
          formatter={(v: number) => [`$${v.toFixed(2)}`, "Balance"]}
        />
        <ReferenceLine y={startBalance} stroke="#1e2a3a" strokeDasharray="3 3" />
        <Line type="monotone" dataKey="balance" stroke="#00C076" strokeWidth={2} dot={false} activeDot={{ r: 4, fill: "#00C076" }} />
      </LineChart>
    </ResponsiveContainer>
  );
}
