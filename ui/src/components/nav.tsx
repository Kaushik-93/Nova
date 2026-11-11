"use client";

import { Database, FolderOpen, Library, LogOut, Moon, Sparkles, Sun, Users } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useRef, useState, type ReactNode } from "react";

import { api, type Role } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { useTheme } from "@/components/theme-toggle";
import { cn } from "@/lib/utils";

type Item = { href: string; label: string; icon: typeof Sparkles; roles: Role[] };

const GROUPS: Item[][] = [
  [
    { href: "/builder", label: "Build a dashboard", icon: Sparkles, roles: ["admin", "analyst"] },
    { href: "/projects", label: "Projects", icon: FolderOpen, roles: ["admin", "analyst", "viewer"] },
    { href: "/catalog", label: "Catalog", icon: Library, roles: ["admin", "analyst"] },
  ],
  [
    { href: "/sources", label: "Sources", icon: Database, roles: ["admin"] },
    { href: "/users", label: "People", icon: Users, roles: ["admin"] },
  ],
];

/** A square rail button with its label in a tooltip, which also names it for screen readers. */
function RailButton({ label, active, children, ...props }: { label: string; active?: boolean; children: ReactNode }
  & ({ href: string; onClick?: never } | { href?: never; onClick: () => void })) {
  const className = cn("group relative flex size-10 items-center justify-center rounded-lg text-muted-foreground transition-colors",
    "hover:bg-muted hover:text-foreground focus-visible:outline-2 focus-visible:outline-primary",
    active && "bg-accent text-primary hover:bg-accent hover:text-primary");
  const tip = (
    <span role="tooltip" className="pointer-events-none absolute left-full z-50 ml-2 whitespace-nowrap rounded-md bg-foreground px-2 py-1 text-xs font-medium text-background opacity-0 shadow-raised transition-opacity group-hover:opacity-100 group-focus-visible:opacity-100">
      {label}
    </span>
  );
  return "href" in props && props.href ? (
    <Link href={props.href} aria-label={label} aria-current={active ? "page" : undefined} className={className}>{children}{tip}</Link>
  ) : (
    <button type="button" aria-label={label} onClick={props.onClick} className={className}>{children}{tip}</button>
  );
}

function initials(name: string) {
  const parts = name.replace(/@.*/, "").split(/[\s._-]+/).filter(Boolean);
  return (parts.length > 1 ? parts[0][0] + parts[1][0] : name.slice(0, 2)).toUpperCase();
}

function Account() {
  const { user, signOut } = useAuth();
  const [open, setOpen] = useState(false);
  const [model, setModel] = useState<string | null>(null);
  const menu = useRef<HTMLDivElement>(null);
  useEffect(() => { api.config().then((config) => setModel(config.model)).catch(() => undefined); }, []);
  useEffect(() => {
    if (!open) return;
    const close = (event: MouseEvent | KeyboardEvent) => {
      if (event instanceof KeyboardEvent ? event.key === "Escape" : !menu.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", close);
    return () => { document.removeEventListener("mousedown", close); document.removeEventListener("keydown", close); };
  }, [open]);

  return (
    <div ref={menu} className="relative">
      <button type="button" aria-label="Account" aria-expanded={open} onClick={() => setOpen(!open)}
        className="flex size-9 items-center justify-center rounded-full bg-accent text-[11px] font-semibold text-primary focus-visible:outline-2 focus-visible:outline-primary">
        {initials(user.display_name || user.email)}
      </button>
      {open && (
        <div role="menu" className="absolute bottom-0 left-full z-50 ml-2 w-60 rounded-xl border border-border bg-card p-1.5 shadow-raised">
          <div className="px-2.5 py-2">
            <p className="truncate text-[13px] font-medium">{user.display_name || user.email}</p>
            <p className="truncate text-xs text-muted-foreground">{user.email}</p>
            <p className="mt-1 text-[11px] capitalize text-muted-foreground">{user.role}{model && <> · {model}</>}</p>
          </div>
          <div className="my-1 border-t border-border" />
          <button role="menuitem" type="button" onClick={signOut}
            className="flex w-full items-center gap-2 rounded-lg px-2.5 py-2 text-[13px] text-muted-foreground hover:bg-muted hover:text-foreground">
            <LogOut className="size-4" /> Sign out
          </button>
        </div>
      )}
    </div>
  );
}

export function Nav() {
  const path = usePathname();
  const { user } = useAuth();
  const { dark, toggle } = useTheme();
  const groups = GROUPS.map((group) => group.filter((item) => item.roles.includes(user.role))).filter((group) => group.length);

  return (
    <aside className="flex w-[60px] shrink-0 flex-col items-center border-r border-border bg-rail py-3" aria-label="Main">
      <Link href={user.role === "viewer" ? "/projects" : "/builder"} aria-label="Nova home"
        className="mb-3 flex size-9 items-center justify-center rounded-xl bg-primary text-[15px] font-bold text-primary-foreground">N</Link>
      <nav className="flex flex-1 flex-col items-center gap-1">
        {groups.map((group, index) => (
          <div key={index} className={cn("flex flex-col items-center gap-1", index > 0 && "mt-2 border-t border-border pt-3")}>
            {group.map(({ href, label, icon: Icon }) => (
              <RailButton key={href} href={href} label={label} active={path.startsWith(href)}><Icon className="size-[18px]" /></RailButton>
            ))}
          </div>
        ))}
      </nav>
      <div className="flex flex-col items-center gap-2">
        <RailButton label={dark ? "Light mode" : "Dark mode"} onClick={toggle}>
          {dark ? <Sun className="size-[18px]" /> : <Moon className="size-[18px]" />}
        </RailButton>
        <Account />
      </div>
    </aside>
  );
}
