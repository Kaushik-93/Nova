"use client";

import { useCallback, useEffect, useState } from "react";

/** Light or dark, remembered in this browser. Charts listen for `nova-theme` to redraw. */
export function useTheme() {
  const [dark, setDark] = useState(false);

  useEffect(() => {
    try { setDark(localStorage.getItem("theme") === "dark"); } catch { /* storage blocked: stay light */ }
  }, []);

  const toggle = useCallback(() => {
    setDark((current) => {
      const next = !current;
      document.documentElement.classList.toggle("dark", next);
      try { localStorage.setItem("theme", next ? "dark" : "light"); } catch { /* not remembered, still applied */ }
      window.dispatchEvent(new CustomEvent("nova-theme", { detail: next }));
      return next;
    });
  }, []);

  return { dark, toggle };
}
