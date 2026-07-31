// MZQA chart theme — ported from backend/ai_analyst/committee/charts.py so the
// interactive web charts match the institutional print report pixel-for-pixel
// in palette and geometry conventions.

export const NAVY = "#2F4D73";
export const NAVY2 = "#476D99";
export const NAVY3 = "#6B86A8";
export const MUTED = "#6F7890";
export const BORDER = "#DDD8CD";
export const BORDER_SOFT = "#EEECE5";
export const PANEL = "#FBFAF7";
export const BG = "#F5F4F0";
/** Print-chart semantic tones (deeper than the UI badge green/red). */
export const CHART_GREEN = "#1F7A52";
export const CHART_RED = "#8C2F39";
export const CHART_AMBER = "#B7791F";
/** UI semantic tones. */
export const UI_GREEN = "#16A34A";
export const UI_RED = "#DC2626";
export const UI_AMBER = "#F59E0B";

export const SERIES_COLORS = [NAVY, "#C2410C", "#0E7490", "#7C3AED", "#B45309", "#9333EA"];

export const CHART_FONT = "Inter, 'Segoe UI', sans-serif";
export const NUM_FONT = "Consolas, 'Courier New', monospace";

/** Heatmap red→green lerp, same interpolation as the print sensitivity grid. */
export function heatShade(t: number): string {
  const c = Math.max(0, Math.min(1, t));
  const r = Math.round(220 + (22 - 220) * c);
  const g = Math.round(38 + (163 - 38) * c);
  const b = Math.round(38 + (74 - 38) * c);
  return `rgb(${r},${g},${b})`;
}

/** Nice short number for axis/labels: 1.2T / 34B / 120M / 12.4. */
export function fmtShort(v: number): string {
  const a = Math.abs(v);
  if (a >= 1e12) return `${(v / 1e12).toFixed(1)}T`;
  if (a >= 1e9) return `${(v / 1e9).toFixed(0)}B`;
  if (a >= 1e6) return `${(v / 1e6).toFixed(0)}M`;
  if (a >= 100) return v.toFixed(0);
  if (a >= 10) return v.toFixed(1);
  return v.toFixed(2);
}

export function isNum(v: unknown): v is number {
  return typeof v === "number" && Number.isFinite(v);
}
