"use client";
import { useEffect, useState } from "react";
import { apiGet } from "./api";

export type Loaded<T> = { state: "loading" } | { state: "error"; message: string } | { state: "ok"; data: T };

/** GET `path` (re-fetched when path/params change). Never leaves the caller without a state. */
export function useApi<T>(path: string, params?: Record<string, string | undefined | null>): Loaded<T> {
  const [res, setRes] = useState<Loaded<T>>({ state: "loading" });
  const key = path + JSON.stringify(params ?? {});
  useEffect(() => {
    let live = true;
    setRes({ state: "loading" });
    apiGet<T>(path, params).then(
      (data) => live && setRes({ state: "ok", data }),
      (e) => live && setRes({ state: "error", message: e instanceof Error ? e.message : String(e) }),
    );
    return () => { live = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
  return res;
}
