"use client";

import { useEffect, useState, type ReactNode } from "react";

import { Button } from "@/components/ui/button";

const RESET_MS = 4000;

/** For something that can't be undone: the first press asks, and the second does it. */
export function ConfirmButton({ children, confirm, onConfirm, disabled }: {
  children: ReactNode;
  confirm: string;
  onConfirm: () => void;
  disabled?: boolean;
}) {
  const [asking, setAsking] = useState(false);
  useEffect(() => {
    if (!asking) return;
    const timer = setTimeout(() => setAsking(false), RESET_MS);
    return () => clearTimeout(timer);
  }, [asking]);

  return (
    <Button variant={asking ? "outline" : "ghost"} size="sm" disabled={disabled} className={asking ? "text-destructive" : undefined}
      onClick={() => { if (asking) { setAsking(false); onConfirm(); } else setAsking(true); }}>
      {asking ? confirm : children}
    </Button>
  );
}
