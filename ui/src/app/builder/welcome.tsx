"use client";

import { ArrowRight, ArrowUp, LayoutDashboard } from "lucide-react";
import Link from "next/link";
import { useEffect, useState } from "react";

import { SUGGESTIONS } from "@/app/builder/suggestions";
import { Button } from "@/components/ui/button";
import { Select } from "@/components/ui/field";
import { api, type DashboardRow, type Project, type SourceRow } from "@/lib/api";
import { useAuth } from "@/lib/auth";

export function Welcome({ onSend, requireApproval, setRequireApproval, project, setProject }: {
  onSend: (prompt: string) => void;
  requireApproval: boolean;
  setRequireApproval: (value: boolean) => void;
  project: string | null;
  setProject: (slug: string | null) => void;
}) {
  const { user } = useAuth();
  const [prompt, setPrompt] = useState("");
  const [sources, setSources] = useState<SourceRow[] | null>(null);
  const [recent, setRecent] = useState<DashboardRow[]>([]);
  const [projects, setProjects] = useState<Project[]>([]);

  useEffect(() => {
    api.sources().then(setSources).catch(() => setSources([]));
    api.projects().then((rows) => setProjects(rows.filter((row) => row.can_edit))).catch(() => undefined);
    // Only what this person can carry on with: a dashboard shared with them opens elsewhere.
    api.listDashboards()
      .then((rows) => setRecent(rows.filter((row) => user.role === "admin" || row.created_by === user.email).slice(0, 3)))
      .catch(() => undefined);
  }, [user]);

  const suggestions = (sources ?? []).flatMap((source) => (source.skill ? SUGGESTIONS[source.slug] ?? [] : []));

  return (
    <div className="mx-auto flex min-h-full max-w-3xl flex-col justify-center px-6 py-12">
      <h1 className="text-center text-[28px] font-semibold tracking-tight">What do you want to see?</h1>
      <p className="mt-2 text-center text-sm text-muted-foreground">Describe a dashboard, and Nova builds it from your data while you watch.</p>

      <div className="mt-8 rounded-2xl border border-border bg-card p-3 shadow-raised focus-within:border-primary">
        <textarea value={prompt} onChange={(e) => setPrompt(e.target.value)} rows={3} autoFocus aria-label="Describe the dashboard you want"
          placeholder="Revenue by region for the last 12 months, with a filter for tenant…"
          className="w-full resize-none bg-transparent px-1 text-sm leading-relaxed outline-none placeholder:text-muted-foreground"
          onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey && prompt.trim()) { e.preventDefault(); onSend(prompt); } }} />
        <div className="flex items-center gap-3">
          <label className="flex items-center gap-1.5 px-1 text-xs text-muted-foreground">
            <input type="checkbox" checked={requireApproval} onChange={(e) => setRequireApproval(e.target.checked)} className="accent-[var(--primary)]" />
            Let me approve the plan first
          </label>
          <Select aria-label="Project" className="ml-auto h-8 w-52 text-xs" value={project ?? ""} onChange={(e) => setProject(e.target.value || null)}>
            <option value="">In a new project</option>
            {projects.map((row) => <option key={row.slug} value={row.slug}>As a sheet in {row.name}</option>)}
          </Select>
          <Button size="icon" aria-label="Send" disabled={!prompt.trim()} onClick={() => onSend(prompt)}><ArrowUp className="size-4" /></Button>
        </div>
      </div>

      {suggestions.length > 0 && (
        <div className="mt-6">
          <h2 className="mb-2 text-xs font-medium text-muted-foreground">Try one of these</h2>
          <div className="grid gap-2 sm:grid-cols-2">
            {suggestions.map((suggestion) => (
              <button key={suggestion.title} onClick={() => onSend(suggestion.prompt)}
                className="group rounded-xl border border-border bg-card p-3.5 text-left shadow-card transition-colors hover:border-primary">
                <span className="flex items-center justify-between text-[13px] font-medium">
                  {suggestion.title}
                  <ArrowRight className="size-3.5 text-muted-foreground transition-transform group-hover:translate-x-0.5" />
                </span>
                <span className="mt-1 line-clamp-2 text-xs leading-relaxed text-muted-foreground">{suggestion.prompt}</span>
              </button>
            ))}
          </div>
        </div>
      )}

      {recent.length > 0 && (
        <div className="mt-6">
          <h2 className="mb-2 text-xs font-medium text-muted-foreground">Recent dashboards</h2>
          <ul className="divide-y divide-border rounded-xl border border-border bg-card shadow-card">
            {recent.map((row) => (
              <li key={row.slug}>
                <Link href={`/builder?thread=${row.thread_id}`} className="flex items-center gap-2.5 px-3.5 py-2.5 text-[13px] hover:bg-muted">
                  <LayoutDashboard className="size-4 text-muted-foreground" /> <span className="truncate">{row.title}</span>
                </Link>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
