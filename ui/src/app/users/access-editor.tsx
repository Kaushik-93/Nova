"use client";

import { useEffect, useState } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/field";
import { api, type AccessEntry, type SourceRow, type TenantOption } from "@/lib/api";

type Choice = { tenants: string[] | null };

function TenantPicker({ slug, selected, onChange }: { slug: string; selected: string[]; onChange: (ids: string[]) => void }) {
  const [tenants, setTenants] = useState<TenantOption[] | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  useEffect(() => { api.tenants(slug).then(setTenants).catch((cause) => setProblem(cause.message)); }, [slug]);

  if (problem) return <p className="text-xs text-destructive">{problem}</p>;
  if (!tenants) return <p className="text-xs text-muted-foreground">Loading tenants…</p>;
  const shown = tenants.filter((tenant) => tenant.name.toLowerCase().includes(search.toLowerCase()));
  return (
    <div className="space-y-1.5">
      <Input aria-label="Search tenants" className="h-8 text-xs" placeholder={`Search ${tenants.length} tenants…`} value={search} onChange={(e) => setSearch(e.target.value)} />
      <div className="max-h-40 space-y-0.5 overflow-auto rounded-lg border border-border p-1.5">
        {shown.map((tenant) => (
          <label key={tenant.id} className="flex items-center gap-2 rounded px-1 py-0.5 text-xs hover:bg-muted">
            <input type="checkbox" className="accent-[var(--primary)]" checked={selected.includes(tenant.id)}
              onChange={(e) => onChange(e.target.checked ? [...selected, tenant.id] : selected.filter((id) => id !== tenant.id))} />
            {tenant.name}
          </label>
        ))}
      </div>
      <p className="text-[11px] text-muted-foreground">{selected.length} selected. Choosing none shows them no rows.</p>
    </div>
  );
}

/** Replaces everything a person can read. A multi-tenant source starts limited to no tenants when
 *  it's ticked, so that the whole source is only ever an explicit choice. */
export function AccessEditor({ sources, access, onSave, onCancel }: {
  sources: SourceRow[];
  access: AccessEntry[];
  onSave: (entries: AccessEntry[]) => void;
  onCancel: () => void;
}) {
  const [choices, setChoices] = useState<Record<string, Choice>>(() =>
    Object.fromEntries(access.map((entry) => [entry.source, { tenants: entry.tenant_ids }])));

  const set = (slug: string, choice: Choice | null) => setChoices((current) => {
    const next = { ...current };
    if (choice) next[slug] = choice; else delete next[slug];
    return next;
  });

  return (
    <div className="space-y-3 border-t border-border bg-muted/40 px-4 py-3">
      {sources.length === 0 && <p className="text-xs text-muted-foreground">Connect a source first.</p>}
      {sources.map((source) => {
        const choice = choices[source.slug];
        const multiTenant = Boolean(source.tenant_column);
        return (
          <div key={source.slug} className="space-y-1.5">
            <label className="flex items-center gap-2 text-[13px] font-medium">
              <input type="checkbox" className="accent-[var(--primary)]" checked={Boolean(choice)}
                onChange={(e) => set(source.slug, e.target.checked ? { tenants: multiTenant ? [] : null } : null)} />
              {source.display_name}
            </label>
            {choice && multiTenant && (
              <div className="ml-6 space-y-1.5">
                <div className="flex gap-4 text-xs">
                  <label className="flex items-center gap-1.5">
                    <input type="radio" className="accent-[var(--primary)]" checked={choice.tenants !== null} onChange={() => set(source.slug, { tenants: [] })} />
                    Only these tenants
                  </label>
                  <label className="flex items-center gap-1.5">
                    <input type="radio" className="accent-[var(--primary)]" checked={choice.tenants === null} onChange={() => set(source.slug, { tenants: null })} />
                    Every tenant
                  </label>
                </div>
                {choice.tenants !== null && (
                  <TenantPicker slug={source.slug} selected={choice.tenants} onChange={(ids) => set(source.slug, { tenants: ids })} />
                )}
              </div>
            )}
          </div>
        );
      })}
      <div className="flex gap-2">
        <Button size="sm" onClick={() => onSave(Object.entries(choices).map(([source, choice]) => ({ source, tenant_ids: choice.tenants })))}>Save access</Button>
        <Button variant="ghost" size="sm" onClick={onCancel}>Cancel</Button>
      </div>
    </div>
  );
}
