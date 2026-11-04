"use client";

import { embedDashboard, type EmbeddedDashboard } from "@superset-ui/embedded-sdk";
import { useEffect, useRef, useState } from "react";

import { ErrorState } from "@/components/states";
import { api, type EmbedInfo } from "@/lib/api";

const SETTLE_MS = 2500;

export type FilterSelection = { name: string; values: string[] }[];

type DataMask = Record<string, { filterState?: { value?: unknown } } | unknown>;

/** Reads which values are picked in the dashboard's filters. Superset reports them by filter
 *  id, and the API tells us which name each id has. */
function readSelection(mask: DataMask, filters: EmbedInfo["filters"]): FilterSelection {
  const selected: FilterSelection = [];
  for (const filter of filters) {
    const value = (mask[filter.id] as { filterState?: { value?: unknown } } | undefined)?.filterState?.value;
    const values = Array.isArray(value) ? value.map(String) : value ? [String(value)] : [];
    if (values.length) selected.push({ name: filter.name, values });
  }
  return selected;
}

export function DashboardEmbed({ slug, onSelection, onLoaded }: {
  slug: string;
  onSelection: (selection: FilterSelection) => void;
  onLoaded?: () => void;
}) {
  const mount = useRef<HTMLDivElement>(null);
  const [error, setError] = useState<string | null>(null);
  const report = useRef(onSelection);
  report.current = onSelection;
  const loaded = useRef(onLoaded);
  loaded.current = onLoaded;

  useEffect(() => {
    let cancelled = false;
    let embedded: EmbeddedDashboard | undefined;
    let settle: ReturnType<typeof setTimeout> | undefined;
    const applyTheme = (event: Event) => embedded?.setThemeMode((event as CustomEvent<boolean>).detail ? "dark" : "default");

    async function start() {
      setError(null);
      try {
        const info = await api.embed(slug);
        if (cancelled || !mount.current) return;
        embedded = await embedDashboard({
          id: info.embed_uuid,
          supersetDomain: info.superset_url,
          mountPoint: mount.current,
          fetchGuestToken: async () => (await api.embed(slug)).guest_token,
          dashboardUiConfig: {
            hideTitle: true,
            hideChartControls: true,
            emitDataMasks: true, // what lets this page see the filters picked inside the iframe
            filters: { visible: true, expanded: true },
            urlParams: { themeMode: localStorage.getItem("theme") === "dark" ? "dark" : "default" },
          },
        });
        if (cancelled) { embedded.unmount(); return; }
        embedded.observeDataMask((mask) => report.current(readSelection(mask as DataMask, info.filters)));
        window.addEventListener("nova-theme", applyTheme);
        // The iframe is up, but its charts need a moment to draw.
        settle = setTimeout(() => loaded.current?.(), SETTLE_MS);
      } catch (cause) {
        if (!cancelled) setError(cause instanceof Error ? cause.message : "The dashboard couldn't be loaded.");
      }
    }
    start();

    return () => {
      cancelled = true;
      clearTimeout(settle);
      window.removeEventListener("nova-theme", applyTheme);
      embedded?.unmount();
    };
  }, [slug]);

  if (error) return <ErrorState message={error} />;
  return <div ref={mount} className="h-full" data-testid="dashboard-embed" />;
}
