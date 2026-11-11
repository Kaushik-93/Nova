"use client";

import { FolderOpen, Plus, Users } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState, type FormEvent } from "react";

import { Page } from "@/components/page";
import { EmptyState, ErrorState, Loading } from "@/components/states";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Field, Input } from "@/components/ui/field";
import { api, type Project } from "@/lib/api";
import { useAuth } from "@/lib/auth";

const DATE = new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });

function NewProject({ onError }: { onError: (message: string) => void }) {
  const router = useRouter();
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");

  async function submit(event: FormEvent) {
    event.preventDefault();
    try {
      const project = await api.createProject(name, description);
      router.push(`/projects/${project.slug}`);
    } catch (cause) {
      onError(cause instanceof Error ? cause.message : "The project couldn't be created.");
    }
  }

  return (
    <Card className="p-4">
      <form onSubmit={submit} className="grid gap-3 md:grid-cols-2">
        <Field label="Project name"><Input required autoFocus maxLength={120} value={name} onChange={(e) => setName(e.target.value)} /></Field>
        <Field label="Description"><Input maxLength={500} value={description} onChange={(e) => setDescription(e.target.value)} /></Field>
        <div className="md:col-span-2"><Button type="submit" size="sm"><Plus className="size-3.5" /> Create project</Button></div>
      </form>
    </Card>
  );
}

export default function ProjectsPage() {
  const { user } = useAuth();
  const [rows, setRows] = useState<Project[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const builder = user.role !== "viewer";

  useEffect(() => { api.projects().then(setRows).catch((cause) => setError(cause.message)); }, []);

  return (
    <Page title="Projects" icon={<FolderOpen className="size-4" />} width="default"
      description={builder
        ? "A project holds one or more sheets, and each sheet is a dashboard."
        : "The projects shared with you. Each sheet in a project is a dashboard."} actions={<>{builder && <Button variant="outline" size="sm" onClick={() => setAdding(!adding)}><Plus className="size-3.5" /> New project</Button>}</>}>
      {error && <ErrorState message={error} />}
      {adding && <NewProject onError={setError} />}
      {!rows && !error && <Loading />}
      {rows?.length === 0 && (
        <EmptyState title="No projects yet">
          {builder ? "Create one, or build a dashboard and it gets a project of its own." : "Projects appear here once they're shared with you."}
        </EmptyState>
      )}
      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
        {rows?.map((project) => (
          <Link key={project.slug} href={`/projects/${project.slug}`}>
            <Card className="flex h-full flex-col p-4 transition-colors hover:border-primary">
              <div className="flex items-center justify-between">
                <span className="flex size-8 items-center justify-center rounded-lg bg-accent text-primary"><FolderOpen className="size-4" /></span>
                {project.shared && <Badge><Users className="size-3" /> Shared</Badge>}
              </div>
              <h2 className="mt-3 text-[13px] font-semibold leading-snug">{project.name}</h2>
              {project.description && <p className="mt-1 line-clamp-2 text-xs leading-relaxed text-muted-foreground">{project.description}</p>}
              <p className="mt-2 line-clamp-2 flex-1 text-xs text-muted-foreground">{project.sheets.map((sheet) => sheet.name).join(" · ")}</p>
              <div className="mt-3 flex items-center justify-between border-t border-border pt-2.5 text-[11px] text-muted-foreground">
                <span>{project.sheets.length} {project.sheets.length === 1 ? "sheet" : "sheets"}</span>
                <span>{DATE.format(new Date(project.updated_at))}</span>
              </div>
            </Card>
          </Link>
        ))}
      </div>
    </Page>
  );
}
