import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

/** A small set of mutually exclusive views, such as Results and Chart. */
export function Segmented<T extends string>({ value, onChange, options, label, className }: {
  value: T;
  onChange: (value: T) => void;
  options: { value: T; label: ReactNode; icon?: ReactNode }[];
  label: string;
  className?: string;
}) {
  return (
    <div role="tablist" aria-label={label} className={cn("inline-flex rounded-lg bg-muted p-0.5", className)}>
      {options.map((option) => (
        <button key={option.value} type="button" role="tab" aria-selected={value === option.value} onClick={() => onChange(option.value)}
          className={cn("inline-flex h-7 items-center gap-1.5 rounded-md px-2.5 text-xs font-medium text-muted-foreground transition-colors",
            "hover:text-foreground focus-visible:outline-2 focus-visible:outline-primary",
            value === option.value && "bg-card text-foreground shadow-card")}>
          {option.icon}{option.label}
        </button>
      ))}
    </div>
  );
}
