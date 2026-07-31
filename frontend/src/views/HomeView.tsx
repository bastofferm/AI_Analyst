"use client";

// Home — the consumer landing: dark-navy hero with a big "analyze this ticker"
// search, a live sector-pulse strip (hidden when the warehouse has no data),
// how-it-works pillars, meet-the-committee cards and the disclaimer band.
//
// The card treatments follow the MZQA terminal's home banners: every panel
// carries an always-animating vector glyph that brightens on hover, the
// shortcut tiles are full navy gateways with a translating CTA, and hovering a
// sector card opens the terminal's constituents popout.

import { useEffect, useMemo, useRef, useState } from "react";
import {
  api,
  type CommitteeExtraAnalyst,
  type SectorConstituentsResponse,
  type SectorReturnRow,
} from "@/lib/api";
import { pct } from "@/lib/fmt";
import { Sparkline } from "@/components/charts/primitives";
import { InfoBox } from "@/components/ui/InfoBox";
import { SectorConstituentsPopout } from "@/components/home/SectorConstituentsPopout";
import {
  AdvocateVector,
  AuditorVector,
  ChallengerVector,
  ScannerVector,
  RankingVector,
  SpecialistsVector,
} from "@/components/home/vectors";

const QUICK_PICKS = ["MSFT", "AAPL", "NVDA", "GOOG", "7203.T"];

// Sector-pulse period toggle → which return field drives the badge & ordering.
type PulseRange = "1d" | "1w" | "1m" | "ytd";
const PULSE_RANGES: { key: PulseRange; label: string; sub: string; field: keyof SectorReturnRow }[] = [
  { key: "1d", label: "1D", sub: "1 day", field: "ret_1d" },
  { key: "1w", label: "1W", sub: "1 week", field: "ret_1w" },
  { key: "1m", label: "1M", sub: "1 month", field: "ret_1m" },
  { key: "ytd", label: "YTD", sub: "this year", field: "ret_ytd" },
];

const PILLARS = [
  {
    n: "01",
    title: "Pick a stock",
    body: "Type a ticker, browse the universe in Explore, or let the Ideas scanner surface candidates for you.",
  },
  {
    n: "02",
    title: "The committee debates it",
    body: "An Advocate, a Challenger, an Auditor and five sector specialists argue it out over the company's official filings — every claim tied to evidence.",
  },
  {
    n: "03",
    title: "You get a plain-English verdict",
    body: "A fair-value estimate, the scenarios behind it, every chart that matters, and a memo you can actually read.",
  },
];

const COMMITTEE_CARDS: {
  name: string;
  tone: string;
  stance: string;
  blurb: string;
  Vector: (p: { tone?: string; className?: string; variant?: "corner" | "badge" }) => JSX.Element;
}[] = [
  {
    name: "The Advocate",
    tone: "#1F7A52",
    stance: "Builds the case",
    blurb: "Makes the strongest honest case for the stock — growth, moats, and what the market may be underrating.",
    Vector: AdvocateVector,
  },
  {
    name: "The Challenger",
    tone: "#8C2F39",
    stance: "Stress-tests it",
    blurb: "Pressure-tests that case on the same evidence — what could break, what's already priced in.",
    Vector: ChallengerVector,
  },
  {
    name: "The Auditor",
    tone: "#2F4D73",
    stance: "Checks the books",
    blurb: "Ignores the story and checks the books — earnings quality and capital discipline.",
    Vector: AuditorVector,
  },
  {
    name: "The Specialists",
    tone: "#476D99",
    stance: "Five lenses",
    blurb: "Growth, earnings-quality, relative-value, macro and stress-testing lenses join every debate.",
    Vector: SpecialistsVector,
  },
];

export function HomeView({
  analysts,
  onAnalyze,
  onNavigate,
}: {
  analysts: CommitteeExtraAnalyst[];
  onAnalyze: (ticker: string) => void;
  onNavigate: (view: string) => void;
}) {
  const [ticker, setTicker] = useState("");
  const [sectorRows, setSectorRows] = useState<SectorReturnRow[]>([]);
  const [pulseRange, setPulseRange] = useState<PulseRange>("1m");

  useEffect(() => {
    let cancelled = false;
    api
      .sectorReturns("US")
      .then((rows) => {
        if (!cancelled && Array.isArray(rows)) setSectorRows(rows.filter((r) => (r.level_series || []).length > 2));
      })
      .catch(() => {
        /* strip stays hidden */
      });
    return () => {
      cancelled = true;
    };
  }, []);

  function go() {
    const tk = ticker.trim().toUpperCase();
    if (tk) onAnalyze(tk);
  }

  return (
    <div className="flex flex-col gap-6">
      {/* ------------------------------------------------ Hero */}
      <section
        className="relative overflow-hidden rounded-lg border border-navy/30 px-6 py-9 sm:px-10"
        style={{ background: "linear-gradient(135deg, #1A2744 0%, #2F4D73 78%, #3A5B85 100%)" }}
      >
        <div className="relative z-10 flex flex-col gap-8 lg:flex-row lg:items-center lg:justify-between">
          <div className="max-w-xl">
            <div className="eyebrow-hero">MZQA · AI Investment Committee</div>
            <h1 className="hero-title" style={{ fontSize: 30, lineHeight: 1.15 }}>
              Nine AI analysts. <em>One honest answer.</em>
            </h1>
            <p className="hero-subtext mt-3" style={{ fontSize: 12.5 }}>
              Wondering what a stock is really worth? Our committee of AI analysts — advocates, challengers, auditors
              and specialists — debates it over the company&apos;s official filings and hands you a plain-English verdict,
              with every chart that matters.
            </p>
            <div className="mt-6 flex w-full max-w-md items-stretch gap-2">
              <input
                value={ticker}
                onChange={(e) => setTicker(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && go()}
                placeholder="Try a ticker… e.g. MSFT"
                aria-label="Ticker to analyze"
                className="home-search-input--dark h-11 min-w-0 flex-1 rounded-md border border-white/20 bg-white/10 px-3.5 text-[15px] font-semibold uppercase tracking-wide text-[#FBFAF7] outline-none backdrop-blur-sm transition-colors focus:border-amber/70"
              />
              <button
                onClick={go}
                className="h-11 shrink-0 rounded-md bg-amber px-5 text-[13px] font-bold text-[#1A2744] transition-opacity hover:opacity-90"
              >
                Analyze →
              </button>
            </div>
            <div className="mt-3 flex flex-wrap items-center gap-1.5">
              <span className="text-[9.5px] uppercase tracking-[0.12em] text-white/35">Popular</span>
              {QUICK_PICKS.map((t) => (
                <button
                  key={t}
                  onClick={() => onAnalyze(t)}
                  className="num rounded-full border border-white/15 px-2.5 py-0.5 text-[11px] font-semibold text-white/75 transition-colors hover:border-amber/70 hover:text-amber"
                >
                  {t}
                </button>
              ))}
            </div>
          </div>

          {/* Animated MZQA signal graphic (reuses the terminal keyframes) */}
          <div className="hidden shrink-0 lg:block" aria-hidden="true">
            <svg width="300" height="170" viewBox="0 0 300 170" fill="none">
              <line x1="10" y1="150" x2="290" y2="150" stroke="rgba(240,237,230,0.25)" strokeWidth="1" />
              <line x1="10" y1="150" x2="10" y2="10" stroke="rgba(240,237,230,0.25)" strokeWidth="1" />
              {[40, 75, 110].map((y) => (
                <line key={y} x1="10" y1={y} x2="290" y2={y} stroke="rgba(240,237,230,0.07)" strokeWidth="1" />
              ))}
              <path
                className="mzqa-macro-line"
                d="M10,135 C40,132 55,120 80,112 C110,102 125,110 150,92 C180,70 195,84 225,55 C250,32 270,30 290,22"
                stroke="#F59E0B"
                strokeWidth="2.2"
                strokeLinecap="round"
              />
              <circle className="mzqa-macro-dot" cx="290" cy="22" r="3.5" fill="#F59E0B" />
              <path
                d="M10,140 C45,138 70,128 100,124 C140,118 170,108 210,96 C245,86 270,80 290,74"
                stroke="rgba(168,184,216,0.6)"
                strokeWidth="1.4"
                strokeDasharray="3 4"
              />
              <text x="290" y="66" fontSize="8" fill="rgba(168,184,216,0.9)" textAnchor="end" fontFamily="Inter, sans-serif">
                fair value
              </text>
              <text x="290" y="14" fontSize="8" fill="#F59E0B" textAnchor="end" fontFamily="Inter, sans-serif">
                price
              </text>
            </svg>
            <div className="hero-right-eyebrow mt-2 text-right">Independent research · Not investment advice</div>
          </div>
        </div>
      </section>

      {/* ------------------------------------------------ Sector pulse */}
      {sectorRows.length > 0 && <SectorPulse rows={sectorRows} range={pulseRange} onRange={setPulseRange} />}

      {/* ------------------------------------------------ How it works */}
      <section className="card p-6">
        <div className="label">How it works</div>
        <div className="mt-4 grid grid-cols-1 gap-6 md:grid-cols-3">
          {PILLARS.map((p, i) => (
            <div key={p.n} className="rise-in group relative flex gap-3.5" style={{ animationDelay: `${i * 90}ms` }}>
              <div className="num text-[26px] font-bold leading-none text-navy-3/60 transition-colors duration-200 group-hover:text-navy">
                {p.n}
              </div>
              <div className="min-w-0">
                <div className="text-[13.5px] font-semibold text-navy">{p.title}</div>
                {/* Underline wipes in on hover — ties the three steps together. */}
                <span className="mt-1 block h-[2px] w-0 rounded-full bg-amber transition-all duration-300 group-hover:w-10" />
                <p className="mt-1 text-[12px] leading-relaxed text-muted">{p.body}</p>
              </div>
              {i < PILLARS.length - 1 ? (
                <span
                  aria-hidden="true"
                  className="absolute -right-3 top-2 hidden text-[16px] leading-none text-navy-3/30 md:block"
                >
                  →
                </span>
              ) : null}
            </div>
          ))}
        </div>
      </section>

      {/* ------------------------------------------------ Meet the committee */}
      <section>
        <div className="mb-3">
          <div className="label">Meet the committee</div>
          <h2 className="mt-0.5 text-[18px] font-semibold text-navy">Disagreement, by design</h2>
        </div>
        <InfoBox copyKey="committee" className="mb-3" />
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
          {COMMITTEE_CARDS.map((c, i) => (
            <div
              key={c.name}
              className="rise-in hover-lift card group relative min-h-[142px] overflow-hidden p-4 transition-colors hover:border-navy/40"
              style={{ animationDelay: `${i * 70}ms` }}
            >
              {/* Tone rail — grows to full height on hover. */}
              <span
                className="absolute left-0 top-0 h-8 w-[3px] transition-all duration-300 group-hover:h-full"
                style={{ background: c.tone }}
              />
              <div className="relative z-[2]">
                <div className="flex items-start justify-between gap-2">
                  <div className="min-w-0">
                    <div className="flex items-center gap-2">
                      <span className="inline-block h-2.5 w-2.5 rounded-full" style={{ background: c.tone }} />
                      <span className="text-[13px] font-semibold text-navy">{c.name}</span>
                    </div>
                    <div
                      className="mt-0.5 text-[8.5px] font-semibold uppercase tracking-[0.14em]"
                      style={{ color: c.tone, opacity: 0.7 }}
                    >
                      {c.stance}
                    </div>
                  </div>
                  {/* The glyph identifies the analyst, so it reads at rest rather than
                      hiding at 18% until hover the way the old corner decoration did. */}
                  <span
                    className="grid h-9 w-[52px] shrink-0 place-items-center rounded-md px-1.5 py-1 transition-transform duration-300 group-hover:scale-105"
                    style={{ background: `${c.tone}14`, border: `1px solid ${c.tone}33` }}
                  >
                    <c.Vector tone={c.tone} variant="badge" />
                  </span>
                </div>
                <p className="mt-2.5 text-[11.5px] leading-relaxed text-muted">{c.blurb}</p>
              </div>
            </div>
          ))}
        </div>
        <div className="mt-2.5 text-[11px] text-muted">
          {analysts.length > 0 ? (
            <>
              Plus <b className="text-navy">{analysts.length}</b> custom analyst{analysts.length > 1 ? "s" : ""} you deployed —
              manage the roster under <b>Setup</b> in the top bar.
            </>
          ) : (
            <>Want another voice at the table? Deploy your own analyst under <b>Setup</b> in the top bar.</>
          )}
        </div>
      </section>

      {/* ------------------------------------------------ Gateway banners
          MZQA terminal treatment: navy fill, parchment type, animated vector
          bottom-right, CTA slides right on hover. */}
      <section className="grid grid-cols-1 gap-3 md:grid-cols-2">
        <GatewayBanner
          eyebrow="No ticker in mind?"
          title="Let the AI hunt for ideas"
          cta="Run the scanner →"
          onClick={() => onNavigate("ideas")}
        >
          One click scans the market for <strong className="font-semibold">cheap, growing companies</strong> whose
          management sounds confident — each scored 0–100.
          <ScannerVector />
        </GatewayBanner>
        <GatewayBanner
          eyebrow="Torn between similar stocks?"
          title="Rank a whole sector at once"
          cta="Rank a sector →"
          onClick={() => onNavigate("compare")}
        >
          The committee ranks a whole sector <strong className="font-semibold">best → worst value</strong> in one
          debate, with a reason for every name.
          <RankingVector />
        </GatewayBanner>
      </section>

      {/* ------------------------------------------------ Disclaimer */}
      <section className="rounded-md border border-border bg-white/50 px-4 py-3 text-center text-[11px] leading-relaxed text-muted">
        MZQA runs independent, systematic research for educational purposes. Estimates are model outputs, not
        guarantees, and nothing here is investment advice or a recommendation to buy or sell any security. Markets
        carry risk — always do your own research.
      </section>
    </div>
  );
}

/** MZQA gateway banner — navy panel, parchment type, CTA that slides on hover.
 *  The animated vector is passed inside `children` so it positions against the
 *  banner (the banner is the `.group` and the positioning context). */
function GatewayBanner({
  eyebrow,
  title,
  cta,
  onClick,
  children,
}: {
  eyebrow: string;
  title: string;
  cta: string;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      onClick={onClick}
      className="group relative flex min-h-[148px] flex-col justify-between overflow-hidden rounded-md border border-navy bg-navy p-5 text-left text-[#F0EDE6] transition-colors duration-200 hover:bg-navy-2 hover:shadow-[0_6px_18px_rgba(47,77,115,0.22)]"
    >
      <div className="relative z-[2]">
        <div className="text-[9px] font-medium uppercase tracking-[0.15em] text-[#F0EDE6]/55">{eyebrow}</div>
        <div className="mt-1.5 text-[16px] font-semibold leading-tight">{title}</div>
        <div className="mt-1.5 max-w-[300px] text-[12px] font-light leading-snug text-[#F0EDE6]/80">{children}</div>
      </div>
      <div className="relative z-[2] mt-3 text-[10px] font-semibold uppercase tracking-[0.08em] transition-transform duration-200 group-hover:translate-x-1">
        {cta}
      </div>
    </button>
  );
}

function SectorPulse({
  rows,
  range,
  onRange,
}: {
  rows: SectorReturnRow[];
  range: PulseRange;
  onRange: (r: PulseRange) => void;
}) {
  // Only offer periods the warehouse actually has data for (YTD can be empty).
  const availableRanges = useMemo(
    () =>
      PULSE_RANGES.filter((rd) =>
        rows.some((r) => {
          const v = r[rd.field];
          return typeof v === "number" && isFinite(v);
        })
      ),
    [rows]
  );
  const def =
    availableRanges.find((r) => r.key === range) ||
    availableRanges.find((r) => r.key === "1m") ||
    availableRanges[0] ||
    PULSE_RANGES[2];

  const { sorted, upCount, withRet, maxAbs } = useMemo(() => {
    const ret = (r: SectorReturnRow) => {
      const v = r[def.field];
      return typeof v === "number" && isFinite(v) ? v : null;
    };
    const sorted = [...rows].sort((a, b) => {
      const av = ret(a);
      const bv = ret(b);
      if (av === null && bv === null) return 0;
      if (av === null) return 1;
      if (bv === null) return -1;
      return bv - av;
    });
    const vals = rows.map(ret).filter((v): v is number => v !== null);
    return {
      sorted,
      upCount: vals.filter((v) => v >= 0).length,
      withRet: vals.length,
      // Scales the strength bar so the biggest mover fills the card width.
      maxAbs: vals.reduce((m, v) => Math.max(m, Math.abs(v)), 0) || 1,
    };
  }, [rows, def.field]);

  // Hover popout — which sector is hovered, where the panel anchors, and a
  // per-sector cache so re-hovering never refetches (mirrors HomeSectorPanel
  // in the MZQA terminal).
  const [hover, setHover] = useState<{ gicsCode: string; x: number; y: number } | null>(null);
  const [popoutCache, setPopoutCache] = useState<Record<string, SectorConstituentsResponse>>({});
  const [popoutLoading, setPopoutLoading] = useState(false);
  const inFlight = useRef<Set<string>>(new Set());

  useEffect(() => {
    if (!hover) return;
    const key = hover.gicsCode;
    if (popoutCache[key] || inFlight.current.has(key)) return;
    inFlight.current.add(key);
    setPopoutLoading(true);
    api
      .sectorConstituents(key, "US", 10)
      .then((resp) => setPopoutCache((prev) => ({ ...prev, [key]: resp })))
      .catch(() => {
        /* popout renders its empty state */
      })
      .finally(() => {
        inFlight.current.delete(key);
        setPopoutLoading(false);
      });
  }, [hover, popoutCache]);

  const popoutData = hover ? popoutCache[hover.gicsCode] ?? null : null;

  // Newest date in the sector index. Shown next to the range label because the
  // constituents popout runs off a different (usually fresher) price feed.
  const asOf = useMemo(() => {
    const dates = rows.map((r) => r.as_of).filter((d): d is string => Boolean(d)).sort();
    const latest = dates[dates.length - 1];
    return latest
      ? new Date(latest).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" })
      : null;
  }, [rows]);

  /** Anchor the popout below the hovered card; flip above it when the viewport
   *  has no room, and clamp on both axes so it never hangs off-screen. */
  function openPopout(gicsCode: string, el: HTMLElement) {
    const rect = el.getBoundingClientRect();
    const W = 660;
    const H = 380; // 10 rows + both rollups + header/footer
    const x = Math.min(Math.max(rect.left, 12), Math.max(12, window.innerWidth - W - 12));
    const below = rect.bottom + 8;
    const y =
      below + H <= window.innerHeight - 12
        ? below
        : Math.max(12, Math.min(rect.top - H - 8, window.innerHeight - H - 12));
    setHover({ gicsCode, x, y });
  }

  return (
    <section>
      <div className="mb-2 flex flex-wrap items-end justify-between gap-2">
        <div>
          <div className="label">Market pulse · US sectors</div>
          <div className="text-[11px] text-muted">
            Sector performance over {def.sub}, best to worst
            {withRet > 0 ? (
              <>
                {" · "}
                <b className={`num ${upCount >= withRet - upCount ? "text-green" : "text-red"}`}>
                  {upCount} of {withRet}
                </b>{" "}
                sectors up
              </>
            ) : null}
            {/* The sector index and the per-company price feed are refreshed by
                different pipelines and can sit on different dates — label the
                window rather than let two "1 month" figures silently disagree. */}
            {asOf ? (
              <>
                {" · "}
                <span title="Latest date in the sector index">to {asOf}</span>
              </>
            ) : null}
          </div>
        </div>
        {availableRanges.length > 1 ? (
          <div className="flex items-center gap-1">
            {availableRanges.map((r) => (
              <button
                key={r.key}
                onClick={() => onRange(r.key)}
                className={`num rounded-full border px-2.5 py-0.5 text-[10.5px] font-semibold transition-colors ${
                  def.key === r.key
                    ? "border-navy bg-navy text-white"
                    : "border-border bg-white text-muted hover:border-navy hover:text-navy"
                }`}
              >
                {r.label}
              </button>
            ))}
          </div>
        ) : null}
      </div>
      <div className="no-scrollbar flex gap-2.5 overflow-x-auto pb-1 pt-1">
        {sorted.map((r, i) => {
          const v = r[def.field];
          const ret = typeof v === "number" && isFinite(v) ? v : null;
          const strength = ret === null ? 0 : Math.min(100, (Math.abs(ret) / maxAbs) * 100);
          const active = hover?.gicsCode === r.gics_code;
          return (
            <div
              key={r.gics_code}
              className={`rise-in hover-lift card group relative w-[168px] shrink-0 cursor-help overflow-hidden p-3 transition-colors ${
                active ? "border-navy/50" : ""
              }`}
              style={{ animationDelay: `${Math.min(i, 8) * 50}ms` }}
              onMouseEnter={(e) => openPopout(r.gics_code, e.currentTarget)}
              onMouseLeave={() => setHover(null)}
              onFocus={(e) => openPopout(r.gics_code, e.currentTarget)}
              onBlur={() => setHover(null)}
              tabIndex={0}
              aria-label={`${r.gics_name} — hover for the biggest companies in this sector`}
            >
              <div className="flex items-baseline gap-1.5">
                <span className="num text-[9px] font-bold text-navy-3/70">{String(i + 1).padStart(2, "0")}</span>
                <span className="truncate text-[11px] font-semibold text-navy" title={r.gics_name}>
                  {r.gics_name}
                </span>
              </div>
              <div className="mt-1.5 flex items-center justify-between gap-2">
                <Sparkline points={r.level_series || []} width={72} height={24} />
                <div className="flex flex-col items-end gap-0.5">
                  {ret === null ? (
                    <span className="num rounded px-1.5 py-px text-[10px] font-bold text-muted">—</span>
                  ) : (
                    <span
                      className={`num rounded px-1.5 py-px text-[10px] font-bold ${ret >= 0 ? "badge-pos" : "badge-neg"}`}
                    >
                      {ret >= 0 ? "▲" : "▼"} {pct(ret)}
                    </span>
                  )}
                  <span className="text-[8.5px] uppercase tracking-[0.08em] text-muted">{def.sub}</span>
                </div>
              </div>
              {/* Relative-strength rail: widest bar = biggest mover in the strip. */}
              <div className="mt-2 h-[3px] w-full overflow-hidden rounded-full bg-border-soft">
                <div
                  className="h-full rounded-full transition-[width] duration-700 ease-out"
                  style={{
                    width: `${strength}%`,
                    background: ret === null ? "#DDD8CD" : ret >= 0 ? "#16A34A" : "#DC2626",
                    opacity: 0.65,
                  }}
                />
              </div>
              <div className="mt-1 text-[8px] uppercase tracking-[0.1em] text-muted opacity-0 transition-opacity duration-200 group-hover:opacity-70">
                Hover · biggest names
              </div>
            </div>
          );
        })}
      </div>

      {hover ? (
        <SectorConstituentsPopout
          data={popoutData}
          loading={popoutLoading && !popoutData}
          x={hover.x}
          y={hover.y}
          jurisdiction="US"
        />
      ) : null}
    </section>
  );
}
