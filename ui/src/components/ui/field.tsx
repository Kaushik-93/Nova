import type { ComponentProps, ReactNode } from "react";

import { cn } from "@/lib/utils";

const CONTROL = "h-9 w-full rounded-lg border border-border bg-background px-2.5 text-[13px] outline-none transition-colors placeholder:text-muted-foreground focus-visible:border-primary";

export function Input({ className, ...props }: ComponentProps<"input">) {
  return <input className={cn(CONTROL, className)} {...props} />;
}

export function Select({ className, ...props }: ComponentProps<"select">) {
  return <select className={cn(CONTROL, className)} {...props} />;
}

export function Field({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return (
    <label className="block space-y-1">
      <span className="text-xs font-medium">{label}</span>
      {children}
      {hint && <span className="block text-[11px] leading-relaxed text-muted-foreground">{hint}</span>}
    </label>
  );
}
