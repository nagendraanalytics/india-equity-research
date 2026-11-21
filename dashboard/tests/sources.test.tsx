import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { SourcesView } from "../components/SourcesView";
import { installFetch } from "./helpers";

const item = (over: object) => ({
  id: "fx_usdinr", segment: "macro", name: "USD/INR", kind: "frankfurter", terms: "public API",
  refresh_minutes: 360, status: "not_checked", detail: null, last_checked_at: null, last_ok_at: null,
  count: null, latest: null, ...over,
});
const body = (status: string, detail: string | null) => ({
  segments: [{ id: "macro", name: "Macro", note: null, connected: status === "connected" ? 1 : 0, total: 1,
               sources: [item({ status, detail })] }],
  connected: status === "connected" ? 1 : 0, total: 1, affects_score: false,
  note: "Display only: no source changes any score until it has been calibrated.",
});

describe("sources view", () => {
  it("shows status per source, says display-only, and Test refreshes the row", async () => {
    let checked = false;
    const { log } = installFetch({
      "GET /sources": () => (checked ? body("connected", "USD/INR 88.4") : body("not_checked", null)),
      "POST /sources/fx_usdinr/check": () => { checked = true; return item({ status: "connected", detail: "USD/INR 88.4" }); },
    });
    render(<SourcesView />);
    const row = await screen.findByTestId("source-row");
    expect(screen.getByTestId("sources-note")).toHaveTextContent(/display only/i);
    expect(within(row).getByTestId("source-status")).toHaveTextContent("not checked yet");
    await userEvent.click(within(row).getByRole("button", { name: "Test" }));
    expect(await screen.findByText("connected")).toBeInTheDocument();
    expect(screen.getByText("USD/INR 88.4")).toBeInTheDocument();
    expect(log).toContain("POST /sources/fx_usdinr/check");
  });

  it("shows an error message when the API is unreachable", async () => {
    installFetch({});
    render(<SourcesView />);
    expect(await screen.findByText(/no such route|HTTP 404/i)).toBeInTheDocument();
  });
});
