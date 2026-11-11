import { BadgeCheck, CaseSensitive, Clock, Filter, Hash, Sigma, Sparkles, ToggleLeft } from "lucide-react";
import type { ComponentProps } from "react";

import type { SemanticMember } from "@/lib/api";
import { cn } from "@/lib/utils";

export type MemberKind = "measure" | "time" | "dimension" | "segment";

const MEASURE_TYPES = new Set(["count", "count_distinct", "sum", "avg", "min", "max", "number"]);

export function kindOf(member: Pick<SemanticMember, "type"> & { kind?: MemberKind }): MemberKind {
  if (member.kind) return member.kind;
  if (MEASURE_TYPES.has(member.type)) return "measure";
  return member.type === "time" ? "time" : "dimension";
}

const TINTS: Record<MemberKind, string> = {
  measure: "bg-measure text-measure-foreground",
  time: "bg-time text-time-foreground",
  dimension: "bg-dim text-dim-foreground",
  segment: "bg-segment text-segment-foreground",
};

/** Measures are aggregates (Σ); dimensions show what they hold: text, a number, a time, yes or no. */
export function MemberIcon({ type, kind, className }: { type: string; kind?: MemberKind; className?: string }) {
  const resolved = kind ?? kindOf({ type });
  const Icon = resolved === "measure" ? Sigma : resolved === "segment" ? Filter : resolved === "time" ? Clock
    : type === "number" ? Hash : type === "boolean" ? ToggleLeft : CaseSensitive;
  return <Icon aria-hidden className={cn("size-3.5 shrink-0", className)} />;
}

export function MemberChip({ member, kind, className, ...props }: { member: Pick<SemanticMember, "type" | "title">; kind?: MemberKind }
  & ComponentProps<"span">) {
  const resolved = kind ?? kindOf(member);
  return (
    <span className={cn("inline-flex h-7 max-w-full items-center gap-1.5 rounded-md px-2 text-xs font-medium", TINTS[resolved], className)} {...props}>
      <MemberIcon type={member.type} kind={resolved} />
      <span className="truncate">{member.title}</span>
    </span>
  );
}

export function memberTint(kind: MemberKind) {
  return TINTS[kind];
}

/** Certified: a person has reviewed every definition behind it. */
export function Certified({ certified, className }: { certified: boolean; className?: string }) {
  return certified ? (
    <span className={cn("inline-flex items-center gap-1 text-xs font-medium text-success", className)}>
      <BadgeCheck className="size-3.5" aria-hidden /> Certified
    </span>
  ) : (
    <span className={cn("inline-flex items-center gap-1 text-xs font-medium text-muted-foreground", className)}
      title="Defined by AI from the data, and not yet reviewed by a person">
      <Sparkles className="size-3.5" aria-hidden /> AI-inferred
    </span>
  );
}
