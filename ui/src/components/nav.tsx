"use client";

import { BookOpen, Database, LayoutDashboard, LogOut, Sparkles, Users } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";

import { ThemeToggle } from "@/components/theme-toggle";
import { Button } from "@/components/ui/button";
import { api, type Role } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { cn } from "@/lib/utils";

const LINKS: { href: string; label: string; icon: typeof Sparkles; roles: Role[] }[] = [
  { href: "/builder", label: "Build", icon: Sparkles, roles: ["admin", "analyst"] },
  { href: "/dashboards", label: "Dashboards", icon: LayoutDashboard, roles: ["admin", "analyst", "viewer"] },
  { href: "/skills", label: "Skills", icon: BookOpen, roles: ["admin"] },
  { href: "/sources", label: "Sources", icon: Database, roles: ["admin"] },
  { href: "/users", label: "People", icon: Users, roles: ["admin"] },
];

export function Nav() {
  const path = usePathname();
  const { user, signOut } = useAuth();
  const [model, setModel] = useState<string | null>(null);
  useEffect(() => { api.config().then((config) => setModel(config.model)).catch(() => undefined); }, []);

  return (
    <aside className="flex w-52 shrink-0 flex-col border-r border-border bg-card">
      <Link href="/builder" className="flex h-14 items-center gap-2.5 px-4">
        <span className="flex size-7 items-center justify-center rounded-lg bg-primary text-[13px] font-bold text-primary-foreground">N</span>
        <span className="text-[15px] font-semibold tracking-tight">Nova</span>
      </Link>
      <nav className="flex flex-1 flex-col gap-0.5 px-2.5 pt-1">
        {LINKS.filter((link) => link.roles.includes(user.role)).map(({ href, label, icon: Icon }) => {
          const active = path.startsWith(href);
          return (
            <Link key={href} href={href} aria-current={active ? "page" : undefined}
              className={cn("flex h-9 items-center gap-2.5 rounded-lg px-2.5 text-[13px] font-medium text-muted-foreground transition-colors hover:bg-muted hover:text-foreground",
                active && "bg-accent text-primary hover:bg-accent hover:text-primary")}>
              <Icon className="size-4" /> {label}
            </Link>
          );
        })}
      </nav>
      <div className="flex items-center gap-1 border-t border-border px-3 py-2">
        <div className="min-w-0 flex-1">
          <p className="truncate text-xs font-medium" title={user.email}>{user.display_name || user.email}</p>
          <p className="text-[11px] capitalize text-muted-foreground">{user.role}</p>
        </div>
        <Button variant="ghost" size="icon" onClick={signOut} aria-label="Sign out" title="Sign out"><LogOut className="size-4" /></Button>
      </div>
      <div className="flex items-center justify-between border-t border-border px-3 py-2.5">
        <span className="truncate text-[11px] text-muted-foreground" title="The model doing the work">{model}</span>
        <ThemeToggle />
      </div>
    </aside>
  );
}
