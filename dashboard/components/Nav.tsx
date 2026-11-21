"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";

const LINKS = [
  { href: "/overview", label: "Overview" },
  { href: "/fleet", label: "Fleet" },
  { href: "/pipeline", label: "Pipeline" },
  { href: "/", label: "Agent console" },
  { href: "/signals", label: "Signals" },
  { href: "/sectors", label: "Sectors" },
  { href: "/stocks", label: "Stocks" },
  { href: "/sources", label: "Sources" },
];

export function Nav() {
  const path = usePathname() ?? "";
  return (
    <header className="topbar">
      <strong className="brand">India equity research</strong>
      <nav aria-label="Main">
        {LINKS.map((l) => (
          <Link key={l.href} href={l.href}
                aria-current={(l.href === "/" ? path === "/" : path.startsWith(l.href)) ? "page" : undefined}>
            {l.label}
          </Link>
        ))}
      </nav>
    </header>
  );
}
