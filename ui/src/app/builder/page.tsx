import { Suspense } from "react";

import { Builder } from "@/app/builder/builder";
import { Loading } from "@/components/states";

export default function BuilderPage() {
  return <Suspense fallback={<Loading />}><Builder /></Suspense>;
}
