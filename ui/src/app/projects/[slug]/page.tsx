import { Suspense } from "react";

import { ProjectView } from "@/app/projects/[slug]/project-view";
import { Loading } from "@/components/states";

export default function ProjectPage() {
  return <Suspense fallback={<Loading />}><ProjectView /></Suspense>;
}
