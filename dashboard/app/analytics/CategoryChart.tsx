"use client";
import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
} from "recharts";
import type { CategoryStat } from "@/lib/types";

export default function CategoryChart({ data }: { data: CategoryStat[] }) {
  const chartData = data.slice(0, 10).map((c) => ({
    name: c.category.length > 12 ? c.category.slice(0, 12) + "…" : c.category,
    edge: parseFloat((c.edge_avg * 100).toFixed(1)),
    n: c.sample_size,
  }));

  return (
    <ResponsiveContainer width="100%" height={220}>
      <BarChart data={chartData} margin={{ top: 4, right: 8, left: 0, bottom: 20 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="#1f2937" />
        <XAxis
          dataKey="name"
          tick={{ fill: "#6b7280", fontSize: 10 }}
          tickLine={false}
          axisLine={{ stroke: "#1f2937" }}
          angle={-30}
          textAnchor="end"
        />
        <YAxis
          tick={{ fill: "#6b7280", fontSize: 11 }}
          tickLine={false}
          axisLine={false}
          tickFormatter={(v: number) => `${v}%`}
        />
        <Tooltip
          contentStyle={{
            background: "#111827",
            border: "1px solid #1f2937",
            borderRadius: 6,
            fontSize: 12,
            color: "#f9fafb",
          }}
          formatter={(value: number) => [`${value}%`, "Avg Edge"]}
        />
        <Bar dataKey="edge" fill="#3b82f6" radius={[3, 3, 0, 0]} />
      </BarChart>
    </ResponsiveContainer>
  );
}
