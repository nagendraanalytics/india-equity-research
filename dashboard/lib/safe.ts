/** Return the normalised URL only for http(s); anything else (javascript:, data:, relative,
 *  malformed, empty) yields null so callers render plain text instead of a link. */
export function safeHttpUrl(raw: string | null | undefined): string | null {
  if (typeof raw !== "string" || raw.trim() === "") return null;
  try {
    const u = new URL(raw);
    return u.protocol === "http:" || u.protocol === "https:" ? u.href : null;
  } catch {
    return null;
  }
}
