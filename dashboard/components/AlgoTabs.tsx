"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";

import { FAMILIES } from "../lib/algos";

/** Second-level navigation for the Algorithms area: overview plus one dashboard per analysis family. */
export function AlgoTabs() {
  const path = usePathname() ?? "";
  const tabs = [{ href: "/algorithms", label: "Overview", on: path === "/algorithms" },
    ...FAMILIES.map((f) => ({ href: `/algorithms/family/${f.id}`, label: f.label, on: path === `/algorithms/family/${f.id}` }))];
  return (
    <nav className="algotabs" aria-label="Analyses">
      {tabs.map((t) => <Link key={t.href} href={t.href} aria-current={t.on ? "page" : undefined}>{t.label}</Link>)}
    </nav>
  );
}
