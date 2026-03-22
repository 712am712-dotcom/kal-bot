import type { Metadata } from "next";
import Link from "next/link";
import "./globals.css";

export const metadata: Metadata = {
  title: "Kal — Trading Dashboard",
  description: "Kal AI prediction market trading dashboard",
};

const NAV = [
  { href: "/",          label: "Overview",   icon: "📊" },
  { href: "/feed",      label: "Live Feed",  icon: "⚡" },
  { href: "/trades",    label: "Trades",     icon: "📋" },
  { href: "/calendar",  label: "Calendar",   icon: "📅" },
  { href: "/analytics", label: "Analytics",  icon: "📈" },
  { href: "/settings",  label: "Settings",   icon: "⚙️" },
];

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body style={{ display: "flex", minHeight: "100vh", background: "#0a0e1a" }}>
        {/* Desktop Sidebar */}
        <aside style={{
          width: 210, minHeight: "100vh", background: "#080d18",
          borderRight: "1px solid #1e2a3a", display: "flex",
          flexDirection: "column", flexShrink: 0, position: "sticky",
          top: 0, height: "100vh", overflowY: "auto",
        }} className="hide-mobile">
          {/* Logo */}
          <div style={{ padding: "20px 18px 16px", borderBottom: "1px solid #1e2a3a" }}>
            <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
              <div style={{
                width: 34, height: 34, borderRadius: 8,
                background: "rgba(0,192,118,0.15)",
                border: "1px solid rgba(0,192,118,0.35)",
                display: "flex", alignItems: "center", justifyContent: "center",
                fontSize: 16, fontWeight: 700, color: "#00C076",
              }}>K</div>
              <div>
                <div style={{ fontWeight: 700, fontSize: 15, color: "#f0f4f8" }}>Kal</div>
                <div style={{ fontSize: 10, color: "#4b6070", letterSpacing: "0.05em", textTransform: "uppercase" }}>Trading Bot</div>
              </div>
            </div>
          </div>

          {/* Nav links */}
          <nav style={{ padding: "10px 0", flex: 1 }}>
            {NAV.map((link) => (
              <Link key={link.href} href={link.href} style={{
                display: "flex", alignItems: "center", gap: 9,
                padding: "9px 18px", color: "#4b6070", textDecoration: "none",
                fontSize: 13, fontWeight: 500, transition: "all 0.15s",
              }} className="nav-link">
                <span style={{ fontSize: 14 }}>{link.icon}</span>
                {link.label}
              </Link>
            ))}
          </nav>

          <div style={{ padding: "14px 18px", borderTop: "1px solid #1e2a3a", fontSize: 11, color: "#2a3a4a" }}>
            v0.1.0 · paper mode
          </div>
        </aside>

        {/* Main */}
        <main style={{ flex: 1, overflowY: "auto", paddingBottom: 70 }}>{children}</main>

        {/* Mobile Bottom Nav */}
        <nav style={{
          position: "fixed", bottom: 0, left: 0, right: 0,
          background: "#080d18", borderTop: "1px solid #1e2a3a",
          display: "none", justifyContent: "space-around", alignItems: "center",
          padding: "8px 0", zIndex: 50,
        }} className="show-mobile" id="mobile-nav">
          {NAV.map((link) => (
            <Link key={link.href} href={link.href} style={{
              display: "flex", flexDirection: "column", alignItems: "center",
              gap: 2, color: "#4b6070", textDecoration: "none",
              fontSize: 10, fontWeight: 500, padding: "4px 8px",
            }}>
              <span style={{ fontSize: 18 }}>{link.icon}</span>
              {link.label}
            </Link>
          ))}
        </nav>

        <style>{`
          .nav-link:hover { background: rgba(0,192,118,0.06); color: #f0f4f8 !important; }
          .show-mobile { display: none; }
          @media (max-width: 768px) {
            .hide-mobile { display: none !important; }
            .show-mobile { display: flex !important; }
          }
        `}</style>
      </body>
    </html>
  );
}
