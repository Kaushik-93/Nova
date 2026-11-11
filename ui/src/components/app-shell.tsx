"use client";

import { ShieldAlert } from "lucide-react";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useMemo, useState, type ReactNode } from "react";

import { Nav } from "@/components/nav";
import { EmptyState, ErrorState, Loading } from "@/components/states";
import { api, type User } from "@/lib/api";
import { AuthContext } from "@/lib/auth";

const ADMIN_PAGES = ["/sources", "/users"];
const BUILDER_PAGES = ["/builder", "/catalog", "/skills"];

/** Works out who's signed in before showing a page, and which pages they can see. This is only
 *  for a tidy interface: the API refuses everything these checks hide. */
function SignedIn({ children }: { children: ReactNode }) {
  const path = usePathname();
  const router = useRouter();
  const [user, setUser] = useState<User | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => { api.me().then(setUser).catch((cause) => setError(cause.message)); }, []);
  const viewerOnBuilder = user?.role === "viewer" && path.startsWith("/builder");
  useEffect(() => { if (viewerOnBuilder) router.replace("/projects"); }, [viewerOnBuilder, router]);

  const auth = useMemo(() => user && {
    user,
    signOut: () => { api.logout().finally(() => window.location.assign("/login")); },
  }, [user]);

  if (error) return <div className="m-auto w-96"><ErrorState message={error} /></div>;
  if (!auth || viewerOnBuilder) return <div className="m-auto"><Loading /></div>;
  const role = auth.user.role;
  const allowed = role === "admin" || (!ADMIN_PAGES.some((page) => path.startsWith(page))
    && !(role === "viewer" && BUILDER_PAGES.some((page) => path.startsWith(page))));

  return (
    <AuthContext.Provider value={auth}>
      <Nav />
      <main className="min-h-0 min-w-0 flex-1 overflow-auto">
        {allowed ? children : (
          <div className="mx-auto max-w-lg p-8">
            <EmptyState title={role === "viewer" ? "This page is for analysts" : "This page is for administrators"}>
              <ShieldAlert className="mx-auto mt-2 size-5" />
            </EmptyState>
          </div>
        )}
      </main>
    </AuthContext.Provider>
  );
}

export function AppShell({ children }: { children: ReactNode }) {
  const path = usePathname();
  // Signing in, and the renderer's view of a dashboard, which brings its own token, come without the shell.
  const bare = path === "/login" || path.startsWith("/render/");
  return bare ? <main className="min-h-0 min-w-0 flex-1 overflow-auto">{children}</main> : <SignedIn>{children}</SignedIn>;
}
