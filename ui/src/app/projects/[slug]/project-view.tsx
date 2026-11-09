"use client";

import { ArrowLeft, ChevronLeft, ChevronRight, ExternalLink, MessageSquare, Pencil, Plus, Trash2, Users } from "lucide-react";
import Link from "next/link";
import { useParams, useRouter, useSearchParams } from "next/navigation";
import { useCallback, useEffect, useState, type FormEvent } from "react";

import { DashboardEmbed } from "@/components/dashboard-embed";
import { EmptyState, ErrorState, Loading } from "@/components/states";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ConfirmButton } from "@/components/ui/confirm-button";
import { Input } from "@/components/ui/field";
import { api, type Config, type Project } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { cn } from "@/lib/utils";

const TAB_LINK = "inline-flex h-8 items-center gap-1.5 rounded-lg px-2.5 text-xs font-medium text-muted-foreground transition-colors hover:bg-muted hover:text-foreground";

function RenameForm({ value, label, onSave, onCancel }: { value: string; label: string; onSave: (name: string) => void; onCancel: () => void }) {
  const [name, setName] = useState(value);
  const submit = (event: FormEvent) => { event.preventDefault(); if (name.trim()) onSave(name.trim()); };
  return (
    <form onSubmit={submit} className="flex items-center gap-1.5">
      <Input aria-label={label} autoFocus maxLength={120} className="h-8 w-56 text-xs" value={name} onChange={(e) => setName(e.target.value)} />
      <Button type="submit" size="sm">Save</Button>
      <Button type="button" variant="ghost" size="sm" onClick={onCancel}>Cancel</Button>
    </form>
  );
}

export function ProjectView() {
  const { slug } = useParams<{ slug: string }>();
  const wanted = useSearchParams().get("sheet");
  const router = useRouter();
  const { user } = useAuth();
  const [project, setProject] = useState<Project | null>(null);
  const [config, setConfig] = useState<Config | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [renaming, setRenaming] = useState<"project" | "sheet" | null>(null);

  const load = useCallback(() => api.project(slug).then(setProject).catch((cause) => setError(cause.message)), [slug]);
  useEffect(() => { load(); api.config().then(setConfig).catch(() => undefined); }, [load]);

  if (!project) return error ? <div className="p-6"><ErrorState message={error} /></div> : <Loading />;

  const sheet = project.sheets.find((item) => item.slug === wanted) ?? project.sheets[0];
  const index = sheet ? project.sheets.indexOf(sheet) : -1;
  const open = (next: string) => { setRenaming(null); router.replace(`/projects/${slug}?sheet=${next}`); };

  const act = async (work: () => Promise<unknown>) => {
    setError(null);
    try {
      await work();
      setRenaming(null);
      await load();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "That didn't work.");
    }
  };

  const move = (step: number) => act(() => {
    const order = project.sheets.map((item) => item.slug);
    [order[index], order[index + step]] = [order[index + step], order[index]];
    return api.reorderSheets(slug, order);
  });

  const removeProject = async () => {
    setError(null);
    try {
      await api.deleteProject(slug);
      router.push("/projects");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "The project couldn't be deleted.");
    }
  };

  return (
    <div className="flex h-full flex-col">
      <header className="shrink-0 border-b border-border bg-card">
        <div className="flex min-h-14 items-center gap-2 px-4 py-2">
          <Link href="/projects" aria-label="All projects" className="text-muted-foreground hover:text-foreground"><ArrowLeft className="size-4" /></Link>
          {renaming === "project" ? (
            <RenameForm value={project.name} label="Project name" onCancel={() => setRenaming(null)} onSave={(name) => act(() => api.changeProject(slug, { name }))} />
          ) : (
            <div className="min-w-0">
              <h1 className="truncate text-sm font-semibold tracking-tight">{project.name}</h1>
              {project.description && <p className="truncate text-xs text-muted-foreground">{project.description}</p>}
            </div>
          )}
          {project.shared && <Badge><Users className="size-3" /> Shared</Badge>}
          <div className="ml-auto flex items-center gap-1">
            {user.role === "admin" && (
              <Button variant="ghost" size="sm" aria-pressed={project.shared} onClick={() => act(() => api.changeProject(slug, { shared: !project.shared }))}>
                <Users className="size-3.5" /> {project.shared ? "Stop sharing" : "Share"}
              </Button>
            )}
            {project.can_edit && renaming !== "project" && (
              <Button variant="ghost" size="sm" onClick={() => setRenaming("project")}><Pencil className="size-3.5" /> Rename</Button>
            )}
            {project.can_edit && <ConfirmButton confirm="Delete the project and all its sheets?" onConfirm={removeProject}><Trash2 className="size-3.5" /> Delete</ConfirmButton>}
          </div>
        </div>

        <div className="flex items-end gap-1 overflow-x-auto px-3" role="tablist" aria-label="Sheets">
          {project.sheets.map((item) => (
            <button key={item.slug} role="tab" aria-selected={item.slug === sheet?.slug} onClick={() => open(item.slug)}
              className={cn("-mb-px whitespace-nowrap border-b-2 border-transparent px-3 py-2 text-xs font-medium text-muted-foreground transition-colors hover:text-foreground",
                item.slug === sheet?.slug && "border-primary text-primary hover:text-primary")}>
              {item.name}
            </button>
          ))}
          {project.can_edit && (
            <Link href={`/builder?project=${slug}`} className="mb-1 ml-1 inline-flex h-7 shrink-0 items-center gap-1 rounded-md px-2 text-xs font-medium text-muted-foreground hover:bg-muted hover:text-foreground">
              <Plus className="size-3.5" /> Add sheet
            </Link>
          )}
        </div>
      </header>

      {error && <div className="p-3"><ErrorState message={error} /></div>}

      {sheet && (project.can_edit || config?.superset_url) && (
        <div className="flex h-10 shrink-0 items-center gap-1 border-b border-border px-3">
          {project.can_edit && renaming === "sheet" ? (
            <RenameForm value={sheet.name} label="Sheet name" onCancel={() => setRenaming(null)} onSave={(name) => act(() => api.changeSheet(slug, sheet.slug, name))} />
          ) : project.can_edit && (
            <>
              <Link href={`/builder?thread=${sheet.thread_id}`} className={TAB_LINK}><MessageSquare className="size-3.5" /> Change with the agent</Link>
              <Button variant="ghost" size="sm" onClick={() => setRenaming("sheet")}><Pencil className="size-3.5" /> Rename sheet</Button>
              <Button variant="ghost" size="icon" aria-label="Move sheet left" disabled={index === 0} onClick={() => move(-1)}><ChevronLeft className="size-4" /></Button>
              <Button variant="ghost" size="icon" aria-label="Move sheet right" disabled={index === project.sheets.length - 1} onClick={() => move(1)}><ChevronRight className="size-4" /></Button>
              <ConfirmButton confirm="Delete this sheet?" onConfirm={() => act(async () => { await api.deleteSheet(slug, sheet.slug); router.replace(`/projects/${slug}`); })}>
                <Trash2 className="size-3.5" /> Delete sheet
              </ConfirmButton>
            </>
          )}
          {config?.superset_url && (
            <a className={cn(TAB_LINK, "ml-auto")} href={`${config.superset_url}/superset/dashboard/${sheet.slug}/`} target="_blank" rel="noreferrer">
              Open in Superset <ExternalLink className="size-3" />
            </a>
          )}
        </div>
      )}

      <div className="min-h-0 flex-1">
        {sheet ? <DashboardEmbed key={sheet.slug} slug={sheet.slug} onSelection={() => undefined} /> : (
          <div className="mx-auto max-w-lg p-8">
            <EmptyState title="No sheets yet">
              {project.can_edit && <Link className="font-medium text-primary" href={`/builder?project=${slug}`}>Build the first sheet</Link>}
            </EmptyState>
          </div>
        )}
      </div>
    </div>
  );
}
