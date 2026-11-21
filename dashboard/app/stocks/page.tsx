"use client";
import { Suspense } from "react";
import { useSearchParams } from "next/navigation";

import { StocksView } from "../../components/StocksView";

function Inner() {
  const q = useSearchParams();
  return <StocksView sector={q.get("sector") ?? undefined} industry={q.get("industry") ?? undefined} />;
}
export default function Page() { return <Suspense fallback={<p>Loading…</p>}><Inner /></Suspense>; }
