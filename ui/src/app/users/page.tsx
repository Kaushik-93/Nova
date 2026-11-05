"use client";

import { Plus, Shield } from "lucide-react";
import { useEffect, useState, type FormEvent } from "react";

import { AccessEditor } from "@/app/users/access-editor";
import { EmptyState, ErrorState, Loading, PageHeader } from "@/components/states";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Field, Input, Select } from "@/components/ui/field";
import { api, type AccessEntry, type ManagedUser, type Role, type SourceRow } from "@/lib/api";
import { useAuth } from "@/lib/auth";

const ROLES: Record<Role, string> = {
  admin: "Administrator: everything, on all data",
  analyst: "Analyst: builds dashboards from what they can read",
  viewer: "Viewer: opens dashboards shared with them",
};

function describeAccess(user: ManagedUser, sources: SourceRow[]): string {
  if (user.role === "admin") return "All data";
  if (!user.access.length) return "No access yet";
  return user.access.map((entry) => {
    const name = sources.find((source) => source.slug === entry.source)?.display_name ?? entry.source;
    if (entry.tenant_ids === null) return name;
    return `${name} (${entry.tenant_ids.length} ${entry.tenant_ids.length === 1 ? "tenant" : "tenants"})`;
  }).join(", ");
}

function NewUserForm({ onCreated, onError }: { onCreated: () => void; onError: (message: string) => void }) {
  const [email, setEmail] = useState("");
  const [name, setName] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState<Role>("viewer");

  async function submit(event: FormEvent) {
    event.preventDefault();
    try {
      await api.createUser({ email, display_name: name, password: password || null, role });
      setEmail(""); setName(""); setPassword("");
      onCreated();
    } catch (cause) {
      onError(cause instanceof Error ? cause.message : "Couldn't add them.");
    }
  }

  return (
    <Card className="p-4">
      <form onSubmit={submit} className="grid gap-3 md:grid-cols-2">
        <Field label="Email"><Input type="email" required value={email} onChange={(e) => setEmail(e.target.value)} /></Field>
        <Field label="Name"><Input value={name} onChange={(e) => setName(e.target.value)} /></Field>
        <Field label="Password" hint="At least 10 characters. Leave empty for someone who signs in with SSO.">
          <Input type="password" minLength={10} autoComplete="new-password" value={password} onChange={(e) => setPassword(e.target.value)} />
        </Field>
        <Field label="Role" hint={ROLES[role]}>
          <Select value={role} onChange={(e) => setRole(e.target.value as Role)}>
            <option value="viewer">Viewer</option>
            <option value="analyst">Analyst</option>
            <option value="admin">Administrator</option>
          </Select>
        </Field>
        <div className="md:col-span-2"><Button type="submit" size="sm"><Plus className="size-3.5" /> Add person</Button></div>
      </form>
    </Card>
  );
}

export default function UsersPage() {
  const { user: me } = useAuth();
  const [users, setUsers] = useState<ManagedUser[] | null>(null);
  const [sources, setSources] = useState<SourceRow[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [open, setOpen] = useState<string | null>(null);

  const load = () => api.users().then(setUsers).catch((cause) => setError(cause.message));
  useEffect(() => {
    load();
    api.sources().then((rows) => setSources(rows.filter((source) => source.status === "ready"))).catch((cause) => setError(cause.message));
  }, []);

  const change = (user: ManagedUser, body: { role?: Role; is_active?: boolean }) => {
    setError(null);
    api.changeUser(user.id, body).then(load).catch((cause) => setError(cause.message));
  };
  const saveAccess = async (user: ManagedUser, entries: AccessEntry[]) => {
    setError(null);
    try {
      await api.setAccess(user.id, entries);
      setOpen(null);
      load();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Couldn't save that.");
    }
  };

  return (
    <div className="mx-auto max-w-4xl space-y-5 p-8">
      <PageHeader title="People" description="Who can sign in and what they can read. Nobody sees any data until you give them access.">
        <Button variant="outline" size="sm" onClick={() => setAdding(!adding)}><Plus className="size-3.5" /> Add person</Button>
      </PageHeader>
      {error && <ErrorState message={error} />}
      {adding && <NewUserForm onError={setError} onCreated={() => { setAdding(false); setError(null); load(); }} />}
      {!users && !error && <Loading />}
      {users?.length === 0 && <EmptyState title="Nobody here yet" />}
      {users && users.length > 0 && (
        <Card className="divide-y divide-border">
          {users.map((user) => (
            <div key={user.id}>
              <div className="flex items-center gap-3 px-4 py-3">
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <h2 className="truncate text-[13px] font-semibold">{user.display_name || user.email}</h2>
                    {user.uses_sso && <Badge>SSO</Badge>}
                    {!user.is_active && <Badge variant="warning">Disabled</Badge>}
                  </div>
                  <p className="truncate text-xs text-muted-foreground">{user.email} · {describeAccess(user, sources)}</p>
                </div>
                <Select aria-label={`Role for ${user.email}`} className="h-8 w-32 text-xs" value={user.role} disabled={user.id === me.id}
                  onChange={(e) => change(user, { role: e.target.value as Role })}>
                  <option value="viewer">Viewer</option>
                  <option value="analyst">Analyst</option>
                  <option value="admin">Administrator</option>
                </Select>
                {user.role !== "admin" && (
                  <Button variant="outline" size="sm" onClick={() => setOpen(open === user.id ? null : user.id)}>
                    <Shield className="size-3.5" /> Access
                  </Button>
                )}
                {user.id !== me.id && (
                  <Button variant="ghost" size="sm" onClick={() => change(user, { is_active: !user.is_active })}>
                    {user.is_active ? "Disable" : "Enable"}
                  </Button>
                )}
              </div>
              {open === user.id && (
                <AccessEditor sources={sources} access={user.access} onCancel={() => setOpen(null)} onSave={(entries) => saveAccess(user, entries)} />
              )}
            </div>
          ))}
        </Card>
      )}
    </div>
  );
}
