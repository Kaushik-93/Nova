"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

import { ThemeToggle } from "@/components/theme-toggle";
import { cn } from "@/lib/utils";

const LINKS = [
  { href: "/builder", label: "Builder" },
  { href: "/dashboards", label: "Dashboards" },
  { href: "/skills", label: "Skills" },
  { href: "/sources", label: "Sources" },
];

export function Nav() {
  const path = usePathname();
  return (
    <header className="flex h-12 shrink-0 items-center gap-6 border-b border-border bg-card px-4">
      <Link href="/builder" className="text-sm font-semibold tracking-tight">Nova</Link>
      <nav className="flex gap-1">
        {LINKS.map((link) => (
          <Link key={link.href} href={link.href}
            className={cn("rounded-md px-3 py-1.5 text-sm text-muted-foreground hover:bg-muted",
              path.startsWith(link.href) && "bg-muted font-medium text-foreground")}>
            {link.label}
          </Link>
        ))}
      </nav>
      <div className="ml-auto"><ThemeToggle /></div>
    </header>
  );
}
