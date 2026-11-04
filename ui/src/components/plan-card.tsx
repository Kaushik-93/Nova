"use client";

import { Trash2 } from "lucide-react";
import { useEffect, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
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
    <Card>
      <CardHeader className="flex items-center justify-between gap-2">
        <CardTitle>Plan</CardTitle>
        {editable && <Badge variant="warning">Waiting for you</Badge>}
      </CardHeader>
      <CardContent className="space-y-3">
        {editable ? (
          <input aria-label="Dashboard title" value={draft.title} onChange={(e) => setDraft({ ...draft, title: e.target.value })}
            className="w-full rounded-md border border-border bg-background px-2 py-1 text-sm font-medium" />
        ) : <p className="text-sm font-medium">{draft.title}</p>}
        <p className="text-xs leading-relaxed text-muted-foreground">{draft.goal}</p>

        <ul className="space-y-1">
          {draft.charts.map((chart) => (
            <li key={chart.key} className="flex items-center gap-2 rounded bg-muted px-2 py-1 text-xs">
              {editable ? (
                <input aria-label={`Title of ${chart.key}`} value={chart.title}
                  onChange={(e) => setDraft({ ...draft, charts: draft.charts.map((c) => c.key === chart.key ? { ...c, title: e.target.value } : c) })}
                  className="min-w-0 flex-1 bg-transparent" />
              ) : <span className="min-w-0 flex-1 truncate">{chart.title}</span>}
              {editable && (
                <button aria-label={`Remove ${chart.title}`} className="text-muted-foreground hover:text-destructive"
                  onClick={() => setDraft({ ...draft, charts: draft.charts.filter((c) => c.key !== chart.key) })}>
                  <Trash2 className="size-3" />
                </button>
              )}
            </li>
          ))}
        </ul>

        {draft.filters.length > 0 && <p className="text-xs"><span className="text-muted-foreground">Filters: </span>{draft.filters.join(", ")}</p>}
        {draft.assumptions.length > 0 && (
          <details>
            <summary className="cursor-pointer text-xs text-primary">{draft.assumptions.length} assumption{draft.assumptions.length === 1 ? "" : "s"}</summary>
            <ul className="mt-1 list-disc space-y-1 pl-4 text-xs text-muted-foreground">
              {draft.assumptions.map((assumption) => <li key={assumption}>{assumption}</li>)}
            </ul>
          </details>
        )}

        {editable && (
          <div className="flex gap-2">
            <Button size="sm" onClick={() => onApprove?.(draft)}>Approve and build</Button>
            <Button size="sm" variant="outline" onClick={() => onReject?.("Please revise this plan.")}>Revise</Button>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
