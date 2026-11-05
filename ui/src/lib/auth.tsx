"use client";

import { createContext, useContext } from "react";

import type { User } from "@/lib/api";

export type Auth = { user: User; signOut: () => void };

export const AuthContext = createContext<Auth | null>(null);

/** Who's signed in. Only used inside AppShell, which doesn't render pages until it knows. */
export function useAuth(): Auth {
  const auth = useContext(AuthContext);
  if (!auth) throw new Error("useAuth is only available inside AppShell");
  return auth;
}
