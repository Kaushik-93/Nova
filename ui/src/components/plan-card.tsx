"use client";

import { Trash2 } from "lucide-react";
import { useEffect, useState } from "react";

import { Button } from "@/components/ui/button";
import type { Plan } from "@/lib/api";

export function PlanCard({ plan, editable, onApprove, onReject }: {
  plan: Plan;
  editable: boolean;
  onApprove?: (plan: Plan) => void;
  onReject?: (reason: string) => void;
}) {
  const [draft, setDraft] = useState(plan);
  useEffect(() => setDraft(plan), [plan]);

  return (
    <div className="space-y-4">
      <div>
        {editable ? (
          <input aria-label="Dashboard title" value={draft.title} onChange={(e) => setDraft({ ...draft, title: e.target.value })}
            className="w-full rounded-lg border border-border bg-background px-2.5 py-1.5 text-[13px] font-semibold" />
        ) : <p className="text-[13px] font-semibold">{draft.title}</p>}
        <p className="mt-1.5 text-xs leading-relaxed text-muted-foreground">{draft.goal}</p>
      </div>

      <div>
        <h3 className="mb-1.5 text-[11px] font-medium text-muted-foreground">{draft.charts.length} charts</h3>
        <ul className="divide-y divide-border rounded-lg border border-border">
          {draft.charts.map((chart) => (
            <li key={chart.key} className="flex items-center gap-2 px-2.5 py-1.5 text-xs">
              {editable ? (
                <input aria-label={`Title of ${chart.key}`} value={chart.title}
                  onChange={(e) => setDraft({ ...draft, charts: draft.charts.map((c) => c.key === chart.key ? { ...c, title: e.target.value } : c) })}
                  className="min-w-0 flex-1 bg-transparent" />
              ) : <span className="min-w-0 flex-1 truncate" title={chart.title}>{chart.title}</span>}
              {editable && (
                <button aria-label={`Remove ${chart.title}`} className="text-muted-foreground hover:text-destructive"
                  onClick={() => setDraft({ ...draft, charts: draft.charts.filter((c) => c.key !== chart.key) })}>
                  <Trash2 className="size-3" />
                </button>
              )}
            </li>
          ))}
        </ul>
      </div>

      {draft.filters.length > 0 && (
        <div>
          <h3 className="mb-1 text-[11px] font-medium text-muted-foreground">Filters</h3>
          <p className="text-xs">{draft.filters.join(", ")}</p>
        </div>
      )}
      {(draft.new_definitions?.length ?? 0) > 0 && (
        <div>
          <h3 className="mb-1 text-[11px] font-medium text-muted-foreground">New definitions, to be added as AI-inferred</h3>
          <ul className="list-disc space-y-1 pl-4 text-xs leading-relaxed">
            {draft.new_definitions?.map((definition) => <li key={definition}>{definition}</li>)}
          </ul>
        </div>
      )}
      {draft.assumptions.length > 0 && (
        <div>
          <h3 className="mb-1 text-[11px] font-medium text-muted-foreground">Assumptions</h3>
          <ul className="list-disc space-y-1 pl-4 text-xs leading-relaxed text-muted-foreground">
            {draft.assumptions.map((assumption) => <li key={assumption}>{assumption}</li>)}
          </ul>
        </div>
      )}

      {editable && (
        <div className="flex gap-2">
          <Button size="sm" onClick={() => onApprove?.(draft)}>Approve and build</Button>
          <Button size="sm" variant="outline" onClick={() => onReject?.("Please revise this plan.")}>Ask for changes</Button>
        </div>
      )}
    </div>
  );
}
