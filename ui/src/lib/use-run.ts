"use client";

import { useEffect, useState } from "react";

import { API_URL, api, type Run, type RunEvent } from "@/lib/api";

const REFRESH_MS = 2500;

/** Follows a run: its steps as they happen, and its status and timing. */
export function useRun(runId: string | null) {
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [run, setRun] = useState<Run | null>(null);
  const [lost, setLost] = useState(false);

  useEffect(() => {
    setEvents([]);
    setRun(null);
    setLost(false);
    if (!runId) return;

    let finished = false;
    const refresh = () => api.getRun(runId).then(setRun).catch(() => undefined);
    refresh();
    const timer = setInterval(() => { if (!finished) refresh(); }, REFRESH_MS);

    const source = new EventSource(`${API_URL}/api/runs/${runId}/events`);
    source.addEventListener("step", (message) => {
      const event = JSON.parse((message as MessageEvent).data) as RunEvent;
      setEvents((current) => (current.some((e) => e.id === event.id) ? current : [...current, event]));
      setLost(false);
    });
    source.addEventListener("end", () => {
      finished = true;
      source.close();
      refresh();
    });
    // EventSource reconnects by itself, resuming after the last event id.
    source.onerror = () => { if (!finished) setLost(true); };

    return () => {
      finished = true;
      source.close();
      clearInterval(timer);
    };
  }, [runId]);

  return { events, run, lost };
}

/** The current time, ticking, so that a running step shows how long it's been going. */
export function useNow(active: boolean) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [active]);
  return now;
}
