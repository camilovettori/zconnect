import "./globals.css";
import type { Metadata, Viewport } from "next";
import { Manrope } from "next/font/google";
import { AppSessionProvider } from "../components/app-session";

const manrope = Manrope({ subsets: ["latin"], variable: "--font-manrope" });

// Icons come from the file conventions app/favicon.ico, app/icon.png and app/apple-icon.png.
export const metadata: Metadata = {
  title: { default: "Zconnect", template: "%s · Zconnect" },
  description: "Invoice automation from Unify to Zoho",
  applicationName: "Zconnect",
  robots: { index: false, follow: false },
};

export const viewport: Viewport = {
  themeColor: "#1f6aeb",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en" className={manrope.variable}>
      <body className="font-[family-name:var(--font-manrope)] text-slate-900 antialiased bg-[radial-gradient(circle_at_top_left,rgba(59,130,246,0.08),transparent_28%),radial-gradient(circle_at_bottom_right,rgba(15,23,42,0.08),transparent_24%),linear-gradient(180deg,#f8fafc_0%,#ffffff_38%,#f8fafc_100%)]">
        <AppSessionProvider>{children}</AppSessionProvider>
      </body>
    </html>
  );
}
