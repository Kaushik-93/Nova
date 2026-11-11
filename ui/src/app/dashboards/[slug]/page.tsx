"use client";

import { ArrowLeft } from "lucide-react";
import Link from "next/link";
import { use, useState } from "react";

import { Sheet } from "@/components/sheet";
import type { DashboardDetail } from "@/lib/api";

/** One dashboard on its own, for a link someone was sent. */
export default function DashboardPage({ params }: { params: Promise<{ slug: string }> }) {
  const { slug } = use(params);
  const [detail, setDetail] = useState<DashboardDetail | null>(null);
  return (
    <div className="flex h-full flex-col">
      <header className="flex min-h-14 shrink-0 items-center gap-3 border-b border-border bg-card px-4 sm:px-6">
        <Link href={detail?.project ? `/projects/${detail.project.slug}?sheet=${slug}` : "/projects"} aria-label="Back to the project"
          className="text-muted-foreground hover:text-foreground"><ArrowLeft className="size-4" /></Link>
        <div className="min-w-0">
          <h1 className="truncate text-[15px] font-semibold tracking-tight">{detail?.title ?? "Dashboard"}</h1>
          {detail?.project && <p className="truncate text-xs text-muted-foreground">{detail.project.name}</p>}
        </div>
      </header>
      <div className="min-h-0 flex-1 overflow-auto"><Sheet slug={slug} onLoaded={setDetail} /></div>
    </div>
  );
}
