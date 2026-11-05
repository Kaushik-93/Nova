"use client";

import { useEffect, useState } from "react";

import { ErrorState, Loading } from "@/components/states";
import { Button } from "@/components/ui/button";
import { Field, Input } from "@/components/ui/field";
import { api, type Tenancy } from "@/lib/api";

/** How a source separates its tenants. Nova suggests this but never assumes it: a wrong guess
 *  here would be a leak, so an administrator has to confirm it. */
export function TenancyPanel({ slug, onSaved }: { slug: string; onSaved: () => void }) {
  const [tenancy, setTenancy] = useState<Tenancy | null>(null);
  const [column, setColumn] = useState("");
  const [table, setTable] = useState("");
  const [label, setLabel] = useState("");
  const [shared, setShared] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.tenancy(slug).then((loaded) => {
      setTenancy(loaded);
      setColumn(loaded.column ?? loaded.suggestion?.column ?? "");
      setTable(loaded.table ?? loaded.suggestion?.table ?? "");
      setLabel(loaded.label_column ?? loaded.suggestion?.label_column ?? "");
      setShared(loaded.shared_tables);
    }).catch((cause) => setError(cause.message));
  }, [slug]);

  const save = (clear: boolean) => {
    setError(null);
    const body = clear ? { column: null, table: null, label_column: null, shared_tables: [] }
      : { column, table: table || null, label_column: label || null, shared_tables: shared };
    api.setTenancy(slug, body).then(onSaved).catch((cause) => setError(cause.message));
  };

  if (error && !tenancy) return <div className="p-4"><ErrorState message={error} /></div>;
  if (!tenancy) return <Loading />;
  const without = tenancy.suggestion?.tables_without_column ?? [];

  return (
    <div className="space-y-3 border-t border-border bg-muted/40 px-4 py-3">
      <p className="text-xs leading-relaxed text-muted-foreground">
        {tenancy.column ? "This source is set up as multi-tenant." : tenancy.suggestion
          ? "This looks like it holds several tenants' data. Check these, then confirm."
          : "Nova didn't find a tenant column. If this source holds several tenants, name it here."}
        {" "}Once it's on, every new dashboard has to carry this column, so build them after turning it on.
      </p>
      {error && <ErrorState message={error} />}
      <div className="grid gap-3 md:grid-cols-3">
        <Field label="Tenant column" hint="In each table that belongs to a tenant."><Input value={column} onChange={(e) => setColumn(e.target.value)} placeholder="tenant_id" /></Field>
        <Field label="Tenants table" hint="Lists the tenants."><Input value={table} onChange={(e) => setTable(e.target.value)} placeholder="public.tenants" /></Field>
        <Field label="Name column" hint="The tenant's name, in that table."><Input value={label} onChange={(e) => setLabel(e.target.value)} placeholder="tenant_name" /></Field>
      </div>
      {without.length > 0 && (
        <fieldset className="space-y-1">
          <legend className="text-xs font-medium">Tables without that column</legend>
          <p className="text-[11px] text-muted-foreground">Tick the ones everyone may read, such as exchange rates. The rest stay hidden from people limited to a tenant.</p>
          {without.map((name) => (
            <label key={name} className="flex items-center gap-2 text-xs">
              <input type="checkbox" className="accent-[var(--primary)]" checked={shared.includes(name)}
                onChange={(e) => setShared(e.target.checked ? [...shared, name] : shared.filter((item) => item !== name))} />
              <span className="font-mono text-[11px]">{name}</span>
            </label>
          ))}
        </fieldset>
      )}
      <div className="flex gap-2">
        <Button size="sm" disabled={!column} onClick={() => save(false)}>Confirm</Button>
        {tenancy.column && <Button variant="ghost" size="sm" onClick={() => save(true)}>Turn off</Button>}
      </div>
    </div>
  );
}
