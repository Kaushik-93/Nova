"use client";

import { KeyRound, Loader2 } from "lucide-react";
import { useSearchParams } from "next/navigation";
import { useEffect, useState, type FormEvent } from "react";

import { ErrorState } from "@/components/states";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Field, Input } from "@/components/ui/field";
import { API_URL, api, type AuthOptions } from "@/lib/api";

// Why a single sign-on attempt came back here. Each is a fixed code from the API, so nothing
// from the address bar is ever shown on the page.
const SSO_ERRORS: Record<string, string> = {
  sso_failed: "Single sign-on didn't complete. Please try again.",
  missing_email: "Your identity provider didn't share an email address with Nova.",
  domain_not_allowed: "Your email domain isn't allowed to sign in here.",
  account_conflict: "An account already uses that email. Ask an administrator to link it.",
  account_disabled: "Your account has been disabled. Ask an administrator for help.",
};

export function LoginForm() {
  const params = useSearchParams();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [options, setOptions] = useState<AuthOptions | null>(null);
  const code = params.get("error");
  const [error, setError] = useState<string | null>(code ? SSO_ERRORS[code] ?? SSO_ERRORS.sso_failed : null);

  useEffect(() => { api.authOptions().then(setOptions).catch(() => undefined); }, []);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.login(email, password);
      window.location.assign("/");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Couldn't sign in.");
      setBusy(false);
    }
  }

  return (
    <div className="flex min-h-full items-center justify-center p-6">
      <div className="w-full max-w-sm space-y-5">
        <div className="flex flex-col items-center gap-3 text-center">
          <span className="flex size-10 items-center justify-center rounded-xl bg-primary text-lg font-bold text-primary-foreground">N</span>
          <div>
            <h1 className="text-xl font-semibold tracking-tight">Sign in to Nova</h1>
            <p className="mt-1 text-[13px] text-muted-foreground">Dashboards from a sentence or two</p>
          </div>
        </div>
        {error && <ErrorState message={error} />}
        <Card className="space-y-4 p-5">
          {options?.sso.enabled && (
            <>
              <a href={`${API_URL}/api/auth/sso/login`}
                className="flex h-9 w-full items-center justify-center gap-2 rounded-lg border border-border bg-card text-[13px] font-medium shadow-card transition-colors hover:bg-muted">
                <KeyRound className="size-4" /> Continue with {options.sso.name}
              </a>
              <div className="flex items-center gap-3 text-[11px] text-muted-foreground">
                <span className="h-px flex-1 bg-border" /> or <span className="h-px flex-1 bg-border" />
              </div>
            </>
          )}
          <form onSubmit={submit} className="space-y-3">
            <Field label="Email">
              <Input type="email" required autoFocus autoComplete="username" value={email} onChange={(e) => setEmail(e.target.value)} />
            </Field>
            <Field label="Password">
              <Input type="password" required autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} />
            </Field>
            <Button type="submit" className="w-full" disabled={busy}>
              {busy && <Loader2 className="size-4 animate-spin" />} Sign in
            </Button>
          </form>
        </Card>
      </div>
    </div>
  );
}
