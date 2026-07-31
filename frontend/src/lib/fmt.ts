export function num(v: number | null | undefined, digits = 2): string {
  return typeof v === "number" && isFinite(v) ? v.toFixed(digits) : "—";
}

export function pct(v: number | null | undefined, digits = 1): string {
  return typeof v === "number" && isFinite(v) ? `${(v * 100).toFixed(digits)}%` : "—";
}

export function signedPct(v: number | null | undefined, digits = 1): string {
  return typeof v === "number" && isFinite(v) ? `${v >= 0 ? "+" : ""}${v.toFixed(digits)}%` : "—";
}

export function usd(v: number | null | undefined): string {
  if (typeof v !== "number" || !isFinite(v)) return "—";
  if (Math.abs(v) >= 1e12) return `$${(v / 1e12).toFixed(1)}T`;
  if (Math.abs(v) >= 1e9) return `$${(v / 1e9).toFixed(1)}B`;
  if (Math.abs(v) >= 1e6) return `$${(v / 1e6).toFixed(1)}M`;
  return `$${v.toFixed(0)}`;
}

// The currencies the app can display. "Home" currency of a data row is derived
// from its jurisdiction (JP warehouse figures are yen, everything else USD).
export type CurrencyCode = "USD" | "JPY" | "EUR";
export const CURRENCY_SYMBOL: Record<CurrencyCode, string> = { USD: "$", JPY: "¥", EUR: "€" };

/** Map a jurisdiction ("US"/"JP"/"INTL") or a currency code to a CurrencyCode. */
export function homeCurrency(jurisdictionOrCurrency?: string | null): CurrencyCode {
  const s = (jurisdictionOrCurrency || "").toUpperCase();
  if (s === "JP" || s === "JPY") return "JPY";
  if (s === "EUR") return "EUR";
  return "USD";
}

/** Compact money in an explicit currency, e.g. "€1.4B" / "¥36.79T". */
export function moneyC(v: number | null | undefined, currency: CurrencyCode): string {
  if (typeof v !== "number" || !isFinite(v)) return "—";
  const sym = CURRENCY_SYMBOL[currency];
  const a = Math.abs(v);
  if (a >= 1e12) return `${sym}${(v / 1e12).toFixed(2)}T`;
  if (a >= 1e9) return `${sym}${(v / 1e9).toFixed(1)}B`;
  if (a >= 1e6) return `${sym}${(v / 1e6).toFixed(1)}M`;
  return `${sym}${v.toLocaleString("en-US", { maximumFractionDigits: 0 })}`;
}

/** Per-share value in an explicit currency, e.g. "$412.30" / "¥2,180". */
export function perShareC(v: number | null | undefined, currency: CurrencyCode): string {
  if (typeof v !== "number" || !isFinite(v)) return "—";
  const digits = Math.abs(v) >= 1000 ? 0 : 2;
  return `${CURRENCY_SYMBOL[currency]}${v.toLocaleString("en-US", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })}`;
}

/** Compact money with a jurisdiction-aware currency symbol (¥ for JP, $ otherwise). */
export function money(v: number | null | undefined, jurisdiction?: string | null): string {
  return moneyC(v, homeCurrency(jurisdiction));
}

/** Per-share value, e.g. "$412.30" / "¥2,180". */
export function perShare(v: number | null | undefined, jurisdiction?: string | null): string {
  return perShareC(v, homeCurrency(jurisdiction));
}

/** A value already expressed in percent points, e.g. 7.5 -> "7.5%". */
export function pctPoint(v: number | null | undefined, digits = 1): string {
  return typeof v === "number" && isFinite(v) ? `${v.toFixed(digits)}%` : "—";
}

/** Signed percent-point value, e.g. +7.5% / −3.2%. */
export function signedPctPoint(v: number | null | undefined, digits = 1): string {
  if (typeof v !== "number" || !isFinite(v)) return "—";
  return `${v > 0 ? "+" : ""}${v.toFixed(digits)}%`;
}

/** Tailwind text tone for a signed metric cell: green above zero, red below. */
export function signedTone(v: number | null | undefined): string {
  if (typeof v !== "number" || !isFinite(v) || v === 0) return "";
  return v > 0 ? "text-green" : "text-red";
}
