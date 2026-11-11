import { redirect } from "next/navigation";

/** Skills became the Catalog. */
export default async function Skill({ params }: { params: Promise<{ slug: string }> }) {
  redirect(`/catalog/${(await params).slug}`);
}
