"use client";

import { ArrowLeft } from "lucide-react";
import Link from "next/link";
import { useParams } from "next/navigation";

import { DashboardEmbed } from "@/components/dashboard-embed";

/** A dashboard on its own, for reading. Everyone sees it through their own row rules, which
 *  the API puts in the token it gives this page. */
export default function DashboardPage() {
  const { slug } = useParams<{ slug: string }>();
  return (
    <div className="flex h-full flex-col">
      <div className="flex h-11 shrink-0 items-center border-b border-border px-4">
        <Link href="/dashboards" className="flex items-center gap-1.5 text-xs font-medium text-muted-foreground hover:text-foreground">
          <ArrowLeft className="size-3.5" /> Dashboards
        </Link>
      </div>
      <div className="min-h-0 flex-1"><DashboardEmbed slug={slug} onSelection={() => undefined} /></div>
    </div>
  );
}
