import "@fontsource-variable/inter";

import type { Metadata } from "next";
import type { ReactNode } from "react";

import { Nav } from "@/components/nav";

import "./globals.css";

export const metadata: Metadata = { title: "Nova", description: "Dashboards from a sentence or two" };

// Applied before the first paint, so dark mode doesn't flash white.
const THEME_SCRIPT = `if(localStorage.getItem("theme")==="dark")document.documentElement.classList.add("dark")`;

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en" suppressHydrationWarning>
      <head><script dangerouslySetInnerHTML={{ __html: THEME_SCRIPT }} /></head>
      <body className="flex h-screen overflow-hidden">
        <Nav />
        <main className="min-h-0 min-w-0 flex-1 overflow-auto">{children}</main>
      </body>
    </html>
  );
}
