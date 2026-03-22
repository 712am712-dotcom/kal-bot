/**
 * POST /api/config — Update bot_config rows in Supabase.
 * Called by the Settings page form.
 */
import { NextResponse } from "next/server";
import { createSupabaseServerClient } from "@/lib/supabase";

export async function POST(request: Request) {
  try {
    const body = await request.json();
    const config: Record<string, string> = body.config ?? {};

    if (Object.keys(config).length === 0) {
      return NextResponse.json({ error: "No config provided" }, { status: 400 });
    }

    const db = createSupabaseServerClient();

    // Upsert each key-value pair
    const rows = Object.entries(config).map(([key, value]) => ({
      key,
      value: String(value),
      updated_at: new Date().toISOString(),
    }));

    const { error } = await db
      .from("bot_config")
      .upsert(rows, { onConflict: "key" });

    if (error) {
      return NextResponse.json({ error: error.message }, { status: 500 });
    }

    return NextResponse.json({ ok: true });
  } catch (err: unknown) {
    const msg = err instanceof Error ? err.message : "Internal error";
    return NextResponse.json({ error: msg }, { status: 500 });
  }
}
