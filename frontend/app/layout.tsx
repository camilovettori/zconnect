import "./globals.css";
import type { Metadata } from "next";
import { Manrope } from "next/font/google";
import { AppSessionProvider } from "../components/app-session";

const manrope = Manrope({ subsets: ["latin"], variable: "--font-manrope" });

export const metadata: Metadata = {
  title: "Zconnect",
  description: "Invoice automation for Unify → Zoho",
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
