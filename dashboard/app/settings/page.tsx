/**
 * /settings — Bot configuration editor.
 */
import { createSupabaseServerClient } from "@/lib/supabase";
import SettingsForm from "./SettingsForm";

export const revalidate = 0;

export default async function SettingsPage() {
  const db = createSupabaseServerClient();
  const { data } = await db.from("bot_config").select("key, value");
  const config: Record<string, string> = {};
  for (const row of data ?? []) config[row.key] = row.value;

  return (
    <div style={{ padding: "28px 32px", maxWidth: 680 }}>
      <div className="page-header">
        <h1 className="page-title">Settings</h1>
        <p className="page-subtitle">Bot configuration — changes take effect on next scan cycle</p>
      </div>
      <SettingsForm config={config} />
    </div>
  );
}
