import type { Metadata } from "next";
import type { ReactNode } from "react";

import { Nav } from "../components/Nav";
import "./globals.css";

export const metadata: Metadata = { title: "India equity research" };

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body>
        <Nav />
        <main>{children}</main>
        <footer className="note">Research tool for personal use. Not investment advice.</footer>
      </body>
    </html>
  );
}
