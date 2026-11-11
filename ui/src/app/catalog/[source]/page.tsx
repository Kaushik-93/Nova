"use client";

import { ArrowLeft, Box, Check, ChevronDown, Link2, Search, Shield, ShieldOff, Users } from "lucide-react";
import Link from "next/link";
import { use, useCallback, useEffect, useMemo, useState } from "react";

import { Markdown } from "@/components/markdown";
import { Certified, MemberChip, MemberIcon, memberTint, type MemberKind } from "@/components/semantic";
import { EmptyState, ErrorState, Loading } from "@/components/states";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Segmented } from "@/components/ui/segmented";
import { api, type SemanticCube, type SemanticMember, type SemanticQuery, type SemanticSource, type Skill } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { cn } from "@/lib/utils";

type Tab = "model" | "guide" | "examples" | "proposals" | "changelog";

function matches(text: string | null | undefined, needle: string) {
  return (text ?? "").toLowerCase().includes(needle);
}

function cubeMatches(cube: SemanticCube, needle: string) {
  if (!needle) return true;
  return matches(cube.title, needle) || matches(cube.name, needle)
    || [...cube.measures, ...cube.dimensions].some((m) => matches(m.title, needle) || matches(m.short_name, needle));
}

function AccessNote({ rule, multiTenant }: { rule?: { tenant_dimension?: string; shared?: boolean }; multiTenant: boolean }) {
  if (!multiTenant || !rule) return null;
  const [Icon, text, tone] = rule.tenant_dimension ? [Shield, "Limited to each person's tenants", "text-success"]
    : rule.shared ? [Users, "Shared with every tenant", "text-muted-foreground"]
    : [ShieldOff, "Hidden from people limited to tenants: its rows can't be limited", "text-warning"];
  return <span className={cn("inline-flex items-center gap-1 text-xs", tone)}><Icon className="size-3.5" aria-hidden /> {text}</span>;
}

function Definition({ member }: { member: SemanticMember }) {
  if (!member.sql && !member.filters.length) return null;
  return (
    <pre className="mt-2 overflow-auto rounded-lg bg-muted px-3 py-2 font-mono text-[11px] leading-relaxed text-muted-foreground">
      {member.sql ?? "*"}{member.filters.map((filter) => `\n  where ${filter}`).join("")}
    </pre>
  );
}

function MemberRow({ member, kind, canReview, onApprove }: { member: SemanticMember; kind: MemberKind; canReview: boolean;
  onApprove: (member: SemanticMember) => Promise<void> }) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const facts = [member.type, member.format, member.unit].filter(Boolean).join(" · ");
  return (
    <li className="px-4 py-3">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5">
        <MemberChip member={member} kind={kind} />
        <code className="font-mono text-[11px] text-muted-foreground">{member.short_name}</code>
        <span className="text-[11px] text-muted-foreground">{facts}</span>
        <span className="ml-auto flex items-center gap-2">
          <Certified certified={member.status === "reviewed"} />
          {canReview && member.status !== "reviewed" && (
            <Button variant="outline" size="sm" disabled={busy}
              onClick={async () => { setBusy(true); await onApprove(member).finally(() => setBusy(false)); }}>
              <Check className="size-3.5" /> Approve
            </Button>
          )}
          {(member.sql || member.filters.length > 0) && (
            <button type="button" aria-expanded={open} aria-label={`${open ? "Hide" : "Show"} how ${member.title} is defined`}
              onClick={() => setOpen(!open)} className="rounded-md p-1 text-muted-foreground hover:bg-muted hover:text-foreground">
              <ChevronDown className={cn("size-4 transition-transform", open && "rotate-180")} />
            </button>
          )}
        </span>
      </div>
      {member.description && <p className="mt-1.5 text-[13px] leading-relaxed text-muted-foreground">{member.description}</p>}
      {member.status === "reviewed" && member.reviewed_by && <p className="mt-1 text-[11px] text-muted-foreground">Reviewed by {member.reviewed_by}</p>}
      {open && <Definition member={member} />}
    </li>
  );
}

function Section({ title, kind, members, canReview, onApprove }: { title: string; kind: MemberKind; members: SemanticMember[];
  canReview: boolean; onApprove: (member: SemanticMember) => Promise<void> }) {
  if (!members.length) return null;
  return (
    <Card>
      <h3 className="flex items-center gap-2 border-b border-border px-4 py-2.5 text-[13px] font-semibold">
        <span className={cn("flex size-5 items-center justify-center rounded", memberTint(kind))}><MemberIcon type="" kind={kind} className="size-3" /></span>
        {title} <span className="font-normal text-muted-foreground">{members.length}</span>
      </h3>
      <ul className="divide-y divide-border">
        {members.map((member) => <MemberRow key={member.name} member={member} kind={kind} canReview={canReview} onApprove={onApprove} />)}
      </ul>
    </Card>
  );
}

function CubeDetail({ cube, rule, multiTenant, canReview, onApprove, onPick }: { cube: SemanticCube;
  rule?: { tenant_dimension?: string; shared?: boolean }; multiTenant: boolean; canReview: boolean;
  onApprove: (member: SemanticMember) => Promise<void>; onPick: (name: string) => void }) {
  return (
    <div className="space-y-4">
      <div>
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
          <h2 className="text-lg font-semibold tracking-tight">{cube.title}</h2>
          <code className="font-mono text-[11px] text-muted-foreground">{cube.name}</code>
        </div>
        {cube.description && <p className="mt-1 max-w-3xl text-[13px] leading-relaxed text-muted-foreground">{cube.description}</p>}
        <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1">
          <AccessNote rule={rule} multiTenant={multiTenant} />
          <span className="truncate font-mono text-[11px] text-muted-foreground" title={cube.source}>{cube.source.length > 90 ? `${cube.source.slice(0, 90)}…` : cube.source}</span>
        </div>
      </div>
      <Section title="Measures" kind="measure" members={cube.measures} canReview={canReview} onApprove={onApprove} />
      <Section title="Time" kind="time" members={cube.dimensions.filter((d) => d.type === "time")} canReview={canReview} onApprove={onApprove} />
      <Section title="Dimensions" kind="dimension" members={cube.dimensions.filter((d) => d.type !== "time")} canReview={canReview} onApprove={onApprove} />
      {cube.segments.length > 0 && (
        <Card className="p-4">
          <h3 className="text-[13px] font-semibold">Segments</h3>
          <div className="mt-2 flex flex-wrap gap-2">
            {cube.segments.map((segment) => <MemberChip key={segment.name} member={{ title: segment.title, type: "segment" }} kind="segment" title={segment.description ?? undefined} />)}
          </div>
        </Card>
      )}
      {cube.joins.length > 0 && (
        <Card className="p-4">
          <h3 className="flex items-center gap-1.5 text-[13px] font-semibold"><Link2 className="size-3.5" /> Joins</h3>
          <ul className="mt-2 space-y-1 text-[13px]">
            {cube.joins.map((join) => (
              <li key={join.cube}>
                <button type="button" className="font-medium text-primary hover:underline" onClick={() => onPick(join.cube)}>{join.cube}</button>
                <span className="text-muted-foreground"> · {join.relationship.replaceAll("_", " ")}</span>
              </li>
            ))}
          </ul>
        </Card>
      )}
    </div>
  );
}

function QueryChips({ query, titles }: { query: SemanticQuery; titles: Map<string, SemanticMember> }) {
  const chip = (name: string, kind: MemberKind, suffix = "") => {
    const member = titles.get(name);
    return <MemberChip key={`${kind}-${name}${suffix}`} kind={kind} member={{ title: `${member?.title ?? name}${suffix}`, type: member?.type ?? "" }} />;
  };
  return (
    <div className="flex flex-wrap gap-1.5">
      {query.measures?.map((name) => chip(name, "measure"))}
      {query.dimensions?.map((name) => chip(name, "dimension"))}
      {query.time_dimensions?.map((t) => chip(t.dimension, "time", t.granularity ? ` by ${t.granularity}` : ""))}
      {query.filters?.map((f) => (
        <span key={`${f.member}-${f.operator}`} className="inline-flex h-7 items-center rounded-md border border-border px-2 text-xs text-muted-foreground">
          {titles.get(f.member)?.title ?? f.member} {f.operator} {f.values.join(", ")}
        </span>
      ))}
    </div>
  );
}

export default function CatalogSourcePage({ params }: { params: Promise<{ source: string }> }) {
  const { source: slug } = use(params);
  const { user } = useAuth();
  const admin = user.role === "admin";
  const [meta, setMeta] = useState<SemanticSource | null>(null);
  const [skill, setSkill] = useState<Skill | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>("model");
  const [selected, setSelected] = useState<string | null>(null);
  const [query, setQuery] = useState("");

  const load = useCallback(async () => {
    try {
      const sources = await api.semanticMeta();
      const found = sources.find((item) => item.source === slug);
      if (!found) throw new Error("This source has no semantic model you can read.");
      setMeta(found);
      if (admin) setSkill(await api.skill(slug));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "The catalog couldn't be loaded.");
    }
  }, [slug, admin]);
  useEffect(() => { load(); }, [load]);

  const needle = query.trim().toLowerCase();
  const cubes = useMemo(() => (meta?.cubes ?? []).filter((cube) => cubeMatches(cube, needle)), [meta, needle]);
  const current = cubes.find((cube) => cube.name === selected) ?? cubes[0];
  const titles = useMemo(() => new Map((meta?.cubes ?? []).flatMap((c) => [...c.measures, ...c.dimensions]).map((m) => [m.name, m])), [meta]);
  const multiTenant = Object.values(skill?.access ?? {}).some((rule) => rule.tenant_dimension || rule.shared);

  async function approve(member: SemanticMember) {
    try { await api.approveMember(slug, member.name); await load(); } catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)); }
  }

  if (error && !meta) return <div className="p-6"><ErrorState message={error} /></div>;
  if (!meta) return <Loading />;

  const tabs: { value: Tab; label: string }[] = [{ value: "model", label: "Model" }];
  if (skill) {
    tabs.push({ value: "guide", label: "Guide" }, { value: "examples", label: `Examples ${skill.example_queries.length}` });
    tabs.push({ value: "proposals", label: `Proposals ${skill.proposals.length}` }, { value: "changelog", label: "Changelog" });
  }

  return (
    <div className="flex h-full flex-col">
      <header className="flex min-h-14 shrink-0 flex-wrap items-center gap-3 border-b border-border bg-card px-4 py-2 sm:px-6">
        <Link href="/catalog" aria-label="All sources" className="text-muted-foreground hover:text-foreground"><ArrowLeft className="size-4" /></Link>
        <div className="min-w-0 flex-1">
          <h1 className="truncate text-[15px] font-semibold tracking-tight">{meta.display_name}</h1>
          <p className="truncate font-mono text-[11px] text-muted-foreground">{meta.catalog}</p>
        </div>
        {skill && <Badge variant={skill.status === "reviewed" ? "success" : "warning"}>v{skill.version} {skill.status}</Badge>}
        {skill?.status === "draft" && (
          <Button size="sm" onClick={async () => { await api.approveSkill(slug); await load(); }}><Check className="size-3.5" /> Approve all</Button>
        )}
        {tabs.length > 1 && <Segmented label="Views" value={tab} onChange={setTab} options={tabs} />}
      </header>
      {error && <div className="p-4"><ErrorState message={error} /></div>}

      {tab === "model" && (
        <div className="flex min-h-0 flex-1 flex-col md:flex-row">
          <aside className="flex max-h-72 shrink-0 flex-col border-b border-border bg-card md:max-h-none md:w-72 md:border-r md:border-b-0" aria-label="Cubes">
            <div className="p-3">
              <label className="flex h-9 items-center gap-2 rounded-lg border border-border bg-background px-2.5 focus-within:border-primary">
                <Search className="size-4 text-muted-foreground" aria-hidden />
                <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Search cubes and members…" aria-label="Search cubes and members"
                  className="w-full bg-transparent text-[13px] outline-none placeholder:text-muted-foreground" />
              </label>
            </div>
            <ul className="min-h-0 flex-1 space-y-0.5 overflow-auto px-2 pb-3">
              {cubes.map((cube) => (
                <li key={cube.name}>
                  <button type="button" onClick={() => setSelected(cube.name)} aria-current={cube === current ? "true" : undefined}
                    className={cn("flex w-full items-center gap-2 rounded-lg px-2.5 py-2 text-left text-[13px] transition-colors hover:bg-muted",
                      cube === current && "bg-accent text-primary hover:bg-accent")}>
                    <Box className="size-4 shrink-0" aria-hidden />
                    <span className="min-w-0 flex-1 truncate font-medium">{cube.title}</span>
                    <span className="text-[11px] tabular-nums text-muted-foreground">{cube.measures.length + cube.dimensions.length}</span>
                  </button>
                </li>
              ))}
              {cubes.length === 0 && <li className="px-2.5 py-2 text-xs text-muted-foreground">Nothing matches “{query}”.</li>}
            </ul>
          </aside>
          <main className="min-h-0 min-w-0 flex-1 overflow-auto p-4 sm:p-6">
            {current ? (
              <CubeDetail cube={current} rule={skill?.access[current.name]} multiTenant={multiTenant} canReview={admin}
                onApprove={approve} onPick={(name) => { setQuery(""); setSelected(name); }} />
            ) : <EmptyState title="No cubes" />}
          </main>
        </div>
      )}

      {tab !== "model" && skill && (
        <div className="min-h-0 flex-1 overflow-auto">
          <div className="mx-auto max-w-4xl space-y-4 p-4 sm:p-6">
            {tab === "guide" && (
              <>
                <Card className="p-5"><Markdown>{skill.guide}</Markdown></Card>
                {skill.gotchas.length > 0 && (
                  <Card className="p-5">
                    <h3 className="text-[13px] font-semibold">Traps found while measuring</h3>
                    <ul className="mt-2 space-y-1.5 text-[13px]">
                      {skill.gotchas.map((g, index) => (
                        <li key={index}><Badge variant="warning">{g.kind.replaceAll("_", " ")}</Badge> <code className="text-xs">{g.table}.{g.column}</code> <span className="text-muted-foreground">{g.detail}</span></li>
                      ))}
                    </ul>
                  </Card>
                )}
              </>
            )}
            {tab === "examples" && (skill.example_queries.length ? skill.example_queries.map((example) => (
              <Card key={example.question} className="p-4">
                <p className="text-[13px] font-medium">{example.question}</p>
                <div className="mt-2"><QueryChips query={example.query} titles={titles} /></div>
              </Card>
            )) : <EmptyState title="No verified examples" />)}
            {tab === "proposals" && (skill.proposals.length ? skill.proposals.map((proposal) => (
              <Card key={proposal.index} className="p-4">
                <div className="flex flex-wrap items-center gap-2">
                  <code className="font-mono text-xs">{proposal.cube}.{proposal.member.name}</code>
                  <span className="ml-auto flex gap-2">
                    <Button variant="outline" size="sm" onClick={async () => { await api.decideProposal(slug, proposal.index, false); await load(); }}>Reject</Button>
                    <Button size="sm" onClick={async () => { await api.decideProposal(slug, proposal.index, true); await load(); }}><Check className="size-3.5" /> Accept</Button>
                  </span>
                </div>
                <p className="mt-2 text-[13px] text-muted-foreground">{proposal.reason}</p>
                <pre className="mt-2 overflow-auto rounded-lg bg-muted px-3 py-2 font-mono text-[11px]">{proposal.member.type}({proposal.member.sql ?? "*"})</pre>
              </Card>
            )) : <EmptyState title="No proposals waiting">Corrections people make while building dashboards appear here for review.</EmptyState>)}
            {tab === "changelog" && <Card className="p-5"><Markdown>{skill.changelog || "Nothing yet."}</Markdown></Card>}
          </div>
        </div>
      )}
    </div>
  );
}
