import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

const WIDTHS = { narrow: "mx-auto max-w-4xl", default: "mx-auto max-w-6xl", full: "" };

/** A page: a header bar with its title and actions, over its content. */
export function Page({ title, icon, description, actions, width = "default", children }: {
  title: ReactNode;
  icon?: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
  width?: keyof typeof WIDTHS;
  children: ReactNode;
}) {
  return (
    <div className="flex min-h-full flex-col">
      <header className="sticky top-0 z-20 flex min-h-14 shrink-0 items-center gap-3 border-b border-border bg-card px-4 py-2 sm:px-6">
        {icon && <span className="flex size-8 shrink-0 items-center justify-center rounded-lg bg-accent text-primary">{icon}</span>}
        <div className="min-w-0 flex-1">
          <h1 className="truncate text-[15px] font-semibold tracking-tight">{title}</h1>
          {description && <p className="line-clamp-2 text-xs leading-relaxed text-muted-foreground">{description}</p>}
        </div>
        {actions && <div className="flex shrink-0 flex-wrap items-center justify-end gap-2">{actions}</div>}
      </header>
      <div className={cn("w-full flex-1 space-y-5 p-4 sm:p-6", WIDTHS[width])}>{children}</div>
    </div>
  );
}
