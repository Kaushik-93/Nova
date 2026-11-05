"use client";

import { AlertTriangle, CheckCircle2, Loader2 } from "lucide-react";
import { useState, type FormEvent } from "react";

import { ErrorState } from "@/components/states";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Field, Input, Select } from "@/components/ui/field";
import { api, type Connection, type ConnectionTest, type Started } from "@/lib/api";

const PORTS = { postgres: 5432, mysql: 3306 };
const EMPTY: Connection = { name: "", kind: "postgres", host: "", port: PORTS.postgres, database: "", username: "", password: "" };

export function ConnectForm({ onStarted }: { onStarted: (started: Started, name: string) => void }) {
  const [form, setForm] = useState<Connection>(EMPTY);
  const [busy, setBusy] = useState<"test" | "connect" | null>(null);
  const [tested, setTested] = useState<ConnectionTest | null>(null);
  const [error, setError] = useState<string | null>(null);

  const update = (changes: Partial<Connection>) => { setForm({ ...form, ...changes }); setTested(null); };

  async function run(step: "test" | "connect") {
    setBusy(step);
    setError(null);
    try {
      if (step === "test") {
        setTested(await api.testSource(form));
      } else {
        onStarted(await api.connectSource(form), form.name);
        setForm(EMPTY); // the password shouldn't stay in the page any longer than it has to
        setTested(null);
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "That didn't work.");
    } finally {
      setBusy(null);
    }
  }

  const submit = (event: FormEvent) => { event.preventDefault(); run("connect"); };

  return (
    <Card className="p-4">
      <form onSubmit={submit} autoComplete="off" className="grid gap-3 md:grid-cols-6">
        <div className="md:col-span-4"><Field label="Name" hint="What people will call this data."><Input required maxLength={80} value={form.name} onChange={(e) => update({ name: e.target.value })} /></Field></div>
        <div className="md:col-span-2">
          <Field label="Type">
            <Select value={form.kind} onChange={(e) => { const kind = e.target.value as Connection["kind"]; update({ kind, port: PORTS[kind] }); }}>
              <option value="postgres">PostgreSQL</option>
              <option value="mysql">MySQL</option>
            </Select>
          </Field>
        </div>
        <div className="md:col-span-3"><Field label="Host"><Input required value={form.host} onChange={(e) => update({ host: e.target.value })} /></Field></div>
        <div className="md:col-span-1"><Field label="Port"><Input required type="number" min={1} max={65535} value={form.port} onChange={(e) => update({ port: Number(e.target.value) })} /></Field></div>
        <div className="md:col-span-2"><Field label="Database"><Input required value={form.database} onChange={(e) => update({ database: e.target.value })} /></Field></div>
        <div className="md:col-span-3"><Field label="Username" hint="Use a read-only login."><Input required autoComplete="off" value={form.username} onChange={(e) => update({ username: e.target.value })} /></Field></div>
        <div className="md:col-span-3"><Field label="Password" hint="Stored encrypted, and never shown again."><Input required type="password" autoComplete="new-password" value={form.password} onChange={(e) => update({ password: e.target.value })} /></Field></div>

        {error && <div className="md:col-span-6"><ErrorState message={error} /></div>}
        {tested?.read_only && (
          <p className="flex items-center gap-1.5 text-xs text-success md:col-span-6"><CheckCircle2 className="size-3.5" /> Connected, and this login is read-only.</p>
        )}
        {tested && !tested.read_only && (
          <p className="flex items-start gap-1.5 text-xs text-warning md:col-span-6">
            <AlertTriangle className="mt-px size-3.5 shrink-0" />
            <span>Connected, but this login isn&apos;t read-only. {tested.warning} Nova only ever runs SELECT, but a read-only login is safer.</span>
          </p>
        )}
        <div className="flex gap-2 md:col-span-6">
          <Button type="button" variant="outline" size="sm" disabled={busy !== null} onClick={() => run("test")}>
            {busy === "test" && <Loader2 className="size-3.5 animate-spin" />} Test connection
          </Button>
          <Button type="submit" size="sm" disabled={busy !== null}>
            {busy === "connect" && <Loader2 className="size-3.5 animate-spin" />} Connect and learn this data
          </Button>
        </div>
      </form>
    </Card>
  );
}
