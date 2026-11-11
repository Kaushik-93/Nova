"use client";

import { use, useEffect, useState } from "react";

import { Dashboard } from "@/components/dashboard";
import { api, setRenderToken, type DashboardDetail } from "@/lib/api";

/** A dashboard for the renderer's browser (agent/tools/renderer.py): no shell, and let in by a short-lived
 *  token in the address's fragment, which browsers never send to a server. */
export default function RenderPage({ params }: { params: Promise<{ slug: string }> }) {
  const { slug } = use(params);
  const [detail, setDetail] = useState<DashboardDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const token = new URLSearchParams(window.location.hash.slice(1)).get("token");
    setRenderToken(token);
    history.replaceState(null, "", window.location.pathname);
    api.dashboard(slug).then(setDetail).catch((cause) => setError(cause.message));
  }, [slug]);

  if (error) return <p className="p-6 text-sm text-destructive" data-chart-state="error">{error}</p>;
  if (!detail?.spec) return null;
  return (
    <div className="min-h-full w-full bg-background">
      <h1 className="px-4 pt-4 text-lg font-semibold tracking-tight">{detail.title}</h1>
      <Dashboard slug={slug} spec={detail.spec} />
    </div>
  );
}
