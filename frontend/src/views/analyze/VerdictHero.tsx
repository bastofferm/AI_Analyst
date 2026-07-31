"use client";

// The verdict hero — the committee's bottom line in one glance: a plain-English
// verdict, fair value vs today's price on a gauge, the upside badge and the
// data-confidence ring. Degrades field-by-field for thin (INTL) names.

import type { CommitteeResponse } from "@/lib/api";
import { useMoney } from "@/lib/currency";
import { signedPctPoint } from "@/lib/fmt";
import { GaugeBar, ScoreRing, type GaugeMarker, type GaugeZone } from "@/components/charts/primitives";
import { CHART_RED, NAVY, NAVY2, isNum } from "@/components/charts/theme";
import { DqBanner } from "@/components/committee/DqBanner";
import { InfoBox } from "@/components/ui/InfoBox";
import { HelpTip } from "@/components/ui/HelpTip";

export function VerdictHero({ result }: { result: CommitteeResponse }) {
  const cv = useMoney();
  const tri = result.triangulation;
  const scen = result.scenarios;
  const jur = result.jurisdiction;

  const fairValue =
    tri?.primary_fair_value ??
    result.primary_fair_value ??
    result.probability_weighted_fair_value ??
    scen?.probability_weighted_fair_value ??
    null;
  const price = tri?.current_price ?? scen?.current_price ?? result.analytics?.current_price ?? null;
  const upside =
    tri?.implied_upside_pct ??
    (isNum(fairValue) && isNum(price) && price > 0 ? (fairValue / price - 1) * 100 : null);

  const verdictWord = !isNum(upside)
    ? null
    : upside >= 10
      ? "looks undervalued"
      : upside <= -10
        ? "looks overvalued"
        : "looks fairly valued";

  // Gauge span: scenario range ∪ {fair value, price}, padded.
  const scenarioVals = (scen?.scenarios || [])
    .map((s) => s.per_share_value)
    .filter(isNum) as number[];
  const spanVals = [...scenarioVals, fairValue, price].filter(isNum) as number[];
  let gauge: { min: number; max: number; markers: GaugeMarker[]; zones: GaugeZone[] } | null = null;
  if (spanVals.length >= 2 && isNum(price) && isNum(fairValue)) {
    const lo = Math.min(...spanVals);
    const hi = Math.max(...spanVals);
    const pad = (hi - lo || Math.abs(hi) * 0.2 || 1) * 0.12;
    const markers: GaugeMarker[] = [
      { value: price, label: "Today's price", color: CHART_RED, dashed: true },
      { value: fairValue, label: "Committee fair value", color: NAVY },
    ];
    const zones: GaugeZone[] =
      scenarioVals.length >= 2
        ? [{ from: Math.min(...scenarioVals), to: Math.max(...scenarioVals), color: NAVY2, opacity: 0.22 }]
        : [];
    gauge = { min: lo - pad, max: hi + pad, markers, zones };
  }

  const dqScore = result.data_quality_report?.overall_score;

  return (
    <section className="card overflow-hidden">
      {result.dq_warning ? (
        <div className="p-5 pb-0">
          <DqBanner warning={result.dq_warning} ticker={result.ticker} />
        </div>
      ) : null}
      {jur === "INTL" ? (
        <div className="border-b border-border bg-amber/10 px-5 py-2 text-[11px] leading-5">
          <span className="font-semibold uppercase tracking-[0.14em] text-amber">Lightweight coverage</span>
          <span className="ml-2 text-navy/80">
            — this international name is analyzed from summary fundamentals only, so several sections below may be
            unavailable and the valuation carries a wider uncertainty band.
          </span>
        </div>
      ) : null}

      <div className="p-5 sm:p-6">
        <div className="label">The verdict · {result.ticker.toUpperCase()}</div>
        <div className="mt-2 flex flex-wrap items-center justify-between gap-x-6 gap-y-4">
          <h2 className="text-[24px] font-semibold leading-tight text-navy">
            {verdictWord ? (
              <>
                {result.ticker.toUpperCase()} <span className="font-normal text-navy-2">{verdictWord}</span>
              </>
            ) : (
              <>
                {result.ticker.toUpperCase()}{" "}
                <span className="font-normal text-navy-2">— no headline number for this one</span>
              </>
            )}
          </h2>
          <div className="flex items-center gap-6">
            {isNum(upside) ? (
              <div className="text-right">
                <div
                  className={`num inline-block rounded-md px-3 py-1.5 text-[20px] font-bold ${
                    upside >= 0 ? "badge-pos" : "badge-neg"
                  }`}
                >
                  {upside >= 0 ? "▲" : "▼"} {signedPctPoint(upside)}
                </div>
                <div className="mt-1 text-[9.5px] uppercase tracking-[0.1em] text-muted">
                  <HelpTip term="upside">Est. upside vs price</HelpTip>
                </div>
              </div>
            ) : null}
            {isNum(dqScore) ? (
              <div title="How solid the underlying financial data is (0–100). Audited before any opinions were formed.">
                <ScoreRing score={dqScore} size={62} label="data confidence" />
              </div>
            ) : null}
          </div>
        </div>

        <div className="mt-2 flex flex-wrap gap-x-8 gap-y-2">
          <Headline label={<HelpTip term="fair value">Fair value / share</HelpTip>} value={cv.perShare(fairValue, jur)} strong />
          <Headline label="Today's price" value={cv.perShare(price, jur)} />
          {isNum(result.probability_weighted_fair_value) && isNum(fairValue) &&
          Math.abs(result.probability_weighted_fair_value - fairValue) > 0.005 * Math.abs(fairValue) ? (
            <Headline
              label={<HelpTip term="DCF">DCF cross-check</HelpTip>}
              value={cv.perShare(result.probability_weighted_fair_value, jur)}
            />
          ) : null}
        </div>

        {gauge ? (
          <div className="mt-4">
            <GaugeBar
              min={gauge.min}
              max={gauge.max}
              markers={gauge.markers}
              zones={gauge.zones}
              format={(v) => cv.perShare(v, jur)}
              height={100}
            />
            {gauge.zones.length > 0 ? (
              <div className="mt-0.5 text-center text-[10px] text-muted">
                The shaded band spans the committee&apos;s downside-to-upside scenario range.
              </div>
            ) : null}
          </div>
        ) : null}

        <div className="mt-4">
          <InfoBox copyKey="verdict" />
        </div>
      </div>
    </section>
  );
}

function Headline({
  label,
  value,
  strong,
}: {
  label: React.ReactNode;
  value: string;
  strong?: boolean;
}) {
  return (
    <div>
      <div className="text-[9.5px] uppercase tracking-[0.12em] text-muted">{label}</div>
      <div className={`num mt-0.5 leading-none text-navy ${strong ? "text-[26px] font-bold" : "text-[20px] font-semibold"}`}>
        {value}
      </div>
    </div>
  );
}
