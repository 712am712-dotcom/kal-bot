"use client";
import { BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer } from "recharts";

interface Props { data: { date: string; trades: number; wins: number; losses: number; }[]; }

export default function TradesBarChart({ data }: Props) {
  if (data.length === 0) {
    return <div style={{ height: 180, display: "flex", alignItems: "center", justifyContent: "center", color: "#4b6070" }}>No data yet</div>;
  }
  return (
    <ResponsiveContainer width="100%" height={180}>
      <BarChart data={data} margin={{ top: 0, right: 10, left: 0, bottom: 0 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="#1e2a3a" vertical={false} />
        <XAxis dataKey="date" tick={{ fontSize: 10, fill: "#4b6070" }} tickLine={false} axisLine={false} />
        <YAxis tick={{ fontSize: 10, fill: "#4b6070" }} tickLine={false} axisLine={false} width={25} />
        <Tooltip
          contentStyle={{ background: "#111827", border: "1px solid #1e2a3a", borderRadius: 8, fontSize: 12 }}
          labelStyle={{ color: "#8899aa" }}
        />
        <Bar dataKey="wins" stackId="a" fill="rgba(0,192,118,0.7)" radius={[0,0,0,0]} name="Wins" />
        <Bar dataKey="losses" stackId="a" fill="rgba(239,68,68,0.7)" radius={[3,3,0,0]} name="Losses" />
      </BarChart>
    </ResponsiveContainer>
  );
}
