"use client";

// The model validation report for one research round, sectioned by quality attribute.
//
// Exported as a *body* rather than a card (the CompanyDataBody pattern) so the same
// component serves the inline drawer in the iteration timeline and any full-page view,
// without the two drifting apart.
//
// Section order follows the ML component quality model the backend reports against —
// robustness rating first, then correctness, ranking, economics, robustness detail,
// where-it-works, factor hygiene, explainability, consistency. Leading with the rating
// rather than with rank-IC is deliberate: a model can post a strong headline and still fall
// apart under routine data defects, and that is exactly the case the rating exists to make
// visible.

import { type ResearchBucket, type ResearchIteration, type ResearchRating } from "@/lib/api";
import { num, pct } from "@/lib/fmt";
import { Meta, icQuality, icirQuality } from "../AlphaSearch";

const RATING_TONE: Record<number, string> = { 1: "text-green-700", 2: "text-amber-600", 3: "text-red-700" };
const RATING_WORD: Record<number, string> = { 1: "robust", 2: "moderate", 3: "fragile" };

const n = (v: unknown, d = 4) =>
  typeof v === "number" && isFinite(v) ? v.toFixed(d) : "—";
const p = (v: unknown, d = 1) =>
  typeof v === "number" && isFinite(v) ? `${(v * 100).toFixed(d)}%` : "—";

function Section({ title, hint, children }: {
  title: string; hint?: string; children: React.ReactNode;
}) {
  return (
    <div className="space-y-1.5">
      <div className="flex items-baseline gap-2">
        <span className="label">{title}</span>
        {hint ? <span className="text-[10px] text-muted">{hint}</span> : null}
      </div>
      {children}
    </div>
  );
}

function Rows({ rows }: { rows: [string, React.ReactNode][] }) {
  return (
    <table className="w-full text-[12px]">
      <tbody>
        {rows.map(([k, v]) => (
          <tr key={k} className="border-b border-border-soft last:border-0">
            <td className="py-1 pr-3 text-navy/70">{k}</td>
            <td className="num py-1 text-right font-medium text-navy">{v}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function BucketTable({ rows }: { rows: ResearchBucket[] }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-[11.5px]">
        <thead>
          <tr className="border-b border-border text-[10px] uppercase tracking-[0.07em] text-muted">
            <th className="py-1.5 pr-2 text-left">Bucket</th>
            <th className="px-2 py-1.5 text-right">Names</th>
            <th className="px-2 py-1.5 text-right">Months</th>
            <th className="px-2 py-1.5 text-right">Rank-IC</th>
            <th className="px-2 py-1.5 text-right">t-stat</th>
            <th className="px-2 py-1.5 text-right">Decile spread</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.bucket} className="border-b border-border-soft last:border-0">
              <td className="py-1 pr-2">
                <span className="text-navy">{r.bucket}</span>
                {r.thin ? (
                  <span className="ml-1.5 rounded border border-red-700/60 px-1 text-[9px] uppercase tracking-[0.06em] text-red-700">
                    thin
                  </span>
                ) : null}
              </td>
              <td className="num px-2 py-1 text-right text-navy/70">{r.n_names}</td>
              <td className="num px-2 py-1 text-right text-navy/70">{r.n_months}</td>
              <td className={`num px-2 py-1 text-right font-medium ${icQuality(r.rank_ic ?? undefined).tone > 0 ? "text-green-700" : "text-navy"}`}>
                {n(r.rank_ic)}
              </td>
              <td className="num px-2 py-1 text-right text-navy/70">{n(r.rank_ic_t_stat, 2)}</td>
              <td className="num px-2 py-1 text-right text-navy/70">{p(r.top_decile_spread, 2)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function PerturbationTable({ rating }: { rating: ResearchRating }) {
  if (!rating?.available) {
    return <div className="text-[11.5px] text-muted">{rating?.reason || "Not available."}</div>;
  }
  return (
    <>
      <div className="overflow-x-auto">
        <table className="w-full text-[11.5px]">
          <thead>
            <tr className="border-b border-border text-[10px] uppercase tracking-[0.07em] text-muted">
              <th className="py-1.5 pr-2 text-left">Perturbation</th>
              <th className="px-2 py-1.5 text-left">Stands for</th>
              <th className="px-2 py-1.5 text-right">Rank-IC lost</th>
            </tr>
          </thead>
          <tbody>
            {(rating.perturbations ?? []).filter((x) => x.available !== false).map((x) => {
              const deg = x.rank_ic_degradation ?? 0;
              return (
                <tr key={x.id} className="border-b border-border-soft last:border-0">
                  <td className="py-1 pr-2 text-navy">{x.label}</td>
                  <td className="px-2 py-1 text-[11px] text-muted">{x.stands_for}</td>
                  <td className={`num px-2 py-1 text-right font-medium ${deg > 0.5 ? "text-red-700" : deg > 0.25 ? "text-amber-600" : "text-navy"}`}>
                    {p(x.rank_ic_degradation, 1)}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <p className="text-[10.5px] text-muted">
        The model is held fixed and the data it is served is degraded, so this needs no access
        to the training pipeline. The rating aggregates the <strong>worst</strong> case, not the
        average{rating.deconfounding ? `, and is deconfounded by ${rating.deconfounding}` : ""}.
      </p>
    </>
  );
}

export function ValidationReportBody({ it }: { it: ResearchIteration }) {
  const rep = it.report_json;
  if (!rep) return <div className="text-[12px] text-muted">No report stored for this round.</div>;

  const s = (rep.sections ?? {}) as Record<string, any>;
  const fc = s.functional_correctness ?? {};
  const rq = s.ranking_quality ?? {};
  const econ = s.economic_value ?? {};
  const rob = s.robustness ?? {};
  const expl = s.explainability ?? {};
  const fh = s.factor_hygiene ?? {};
  const cons = s.consistency ?? {};
  const mon = s.monitorability ?? {};
  const rating = (it.rating_json ?? rob.perturbation_rating ?? {}) as ResearchRating;
  // Prefer the dedicated column; fall back to the copy inside the report blob so an older
  // run (persisted before those columns were returned) still renders its tables.
  const cuts = (it.breakdown_json ?? s.domain_adaptability) as any;
  const ci = (fc.rank_ic_ci95 ?? []) as (number | null)[];
  const icq = icQuality(fc.rank_ic_mean);
  const fr = fh.factor_regression ?? {};
  const stab = expl.stability ?? {};
  const hyper = cons.hyperparameter_stability ?? {};

  return (
    <div className="space-y-4">
      {/* headline: the rating leads, because a strong rank-IC can still be fragile */}
      <div className="grid grid-cols-2 gap-x-5 gap-y-3 rounded-lg border border-border-soft bg-paper/40 p-3 sm:grid-cols-3 lg:grid-cols-6">
        <div>
          <div className="label">Robustness rating</div>
          <div className={`mt-0.5 text-[13px] font-semibold tabular-nums ${RATING_TONE[rating.rating ?? 0] ?? "text-navy"}`}>
            {rating.rating ? `${rating.rating} — ${RATING_WORD[rating.rating]}` : "—"}
          </div>
          <div className="text-[10px] text-muted">1 robust · 3 fragile</div>
        </div>
        <Meta label="Model skill" value={`rank-IC ${n(fc.rank_ic_mean, 3)}`} sub={icq.label} tone={icq.tone} />
        <Meta label="95% interval"
          value={ci.length === 2 ? `${n(ci[0], 3)} … ${n(ci[1], 3)}` : "—"}
          sub={ci.length === 2 && (ci[0] ?? 0) <= 0 && (ci[1] ?? 0) >= 0 ? "spans zero" : "excludes zero"}
          tone={ci.length === 2 && (ci[0] ?? 0) > 0 ? 1 : -1} />
        <Meta label="Consistency" value={`ICIR ${n(fc.rank_icir_annualized, 2)}`}
          sub={`${icirQuality(fc.rank_icir_annualized)} · annualized`} />
        <Meta label="R² out-of-sample" value={n(fc.r2_oos?.zero_benchmarked, 5)} sub="zero-benchmarked" />
        <Meta label="Validated on" value={`${fc.n_dates ?? "—"} months`}
          sub={`${mon.names ?? "—"} names · ${mon.features_out ?? "—"} features`} />
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <Section title="Functional correctness">
          <Rows rows={[
            ["Rank-IC t-statistic", n(fc.rank_ic_t_stat, 2)],
            ["Rank-IC p-value", n(fc.rank_ic_p_value, 4)],
            ["Months with positive IC", p(fc.ic_hit_rate, 0)],
            ["Signal autocorrelation", n(fc.signal_autocorr, 3)],
            ["R² (mean-benchmarked)", n(fc.r2_oos?.mean_benchmarked, 5)],
          ]} />
        </Section>

        <Section title="Ranking quality">
          <Rows rows={[
            ["Top-minus-bottom decile spread", p(rq.top_minus_bottom, 2)],
            ["Spread t-statistic", n(rq.top_minus_bottom_tstat, 2)],
            ["Monotonicity across deciles", n(rq.monotonicity, 2)],
            ["Top-decile hit rate", p(rq.top_decile_hit_rate, 0)],
          ]} />
        </Section>

        <Section title="Economic value" hint="what the desk would actually run">
          <Rows rows={[
            ["Long/short annualized return", p(econ.long_short_annualized_return)],
            ["Sharpe", n(econ.long_short_sharpe, 2)],
            ["Maximum drawdown", p(econ.max_drawdown)],
            ["Monthly turnover of the book", p(econ.turnover, 0)],
          ]} />
        </Section>

        <Section title="Stability over time">
          <Rows rows={[
            ["Train-minus-OOS rank-IC gap", n(rob.train_oos_gap)],
            ["Positive years", `${rob.positive_years ?? "—"} of ${rob.total_years ?? "—"}`],
            ["Worst year", `${rob.worst_year ?? "—"} (${n(rob.worst_year_rank_ic)})`],
            ["Up-market rank-IC", n(rob.regime_split?.up_market_rank_ic)],
            ["Down-market rank-IC", n(rob.regime_split?.down_market_rank_ic)],
          ]} />
        </Section>
      </div>

      <Section title="Robustness — perturbation battery"
        hint="the model held fixed, the data degraded">
        <PerturbationTable rating={rating} />
      </Section>

      {cuts?.available ? (
        <Section title="Where the model works"
          hint="GICS classification and Fama-French exposure ask different questions">
          <div className="space-y-3">
            {Object.entries(cuts.cuts ?? {}).map(([cut, rows]) => (
              <div key={cut}>
                <div className="mb-1 text-[11px] font-semibold text-navy">{cut}</div>
                <BucketTable rows={rows as ResearchBucket[]} />
              </div>
            ))}
          </div>
        </Section>
      ) : null}

      <div className="grid gap-4 lg:grid-cols-2">
        <Section title="Factor hygiene" hint="is it alpha, or factor beta?">
          {fr.available ? (
            <>
              <Rows rows={[
                ["Annualized alpha", p(fr.alpha_annualized)],
                ["Alpha t-statistic", n(fr.alpha_tstat, 2)],
                ["Regression R²", n(fr.r2, 3)],
                ["Rank-IC after neutralizing exposure",
                  n(fh.factor_neutral_ic?.factor_neutral_rank_ic)],
              ]} />
              <p className="text-[10.5px] text-muted">
                A small alpha t-statistic beside a large R² means the spread is Fama-French
                factor exposure the desk could buy far more cheaply.
              </p>
            </>
          ) : (
            <div className="text-[11.5px] text-muted">{fr.reason || "Not available."}</div>
          )}
        </Section>

        <Section title="Explainability" hint="and whether the explanation itself is stable">
          {expl.available ? (
            <>
              <Rows rows={(Object.keys(expl.gain_importance ?? {}).slice(0, 6)).map((f) => [
                f,
                <span key={f} className="num">
                  {n(expl.permutation_importance_rank_ic_drop?.[f], 4)}
                </span>,
              ] as [string, React.ReactNode])} />
              <p className="text-[10.5px] text-muted">
                Permutation importance: the rank-IC lost when that feature is shuffled.
                Ranking stability across refits — Jaccard {n(stab.mean_top_k_jaccard, 2)}
                {stab.stable === false
                  ? " — UNSTABLE, the ranking should not be reasoned from."
                  : ", stable."}
              </p>
            </>
          ) : (
            <div className="text-[11.5px] text-muted">Not available.</div>
          )}
        </Section>
      </div>

      <Section title="Consistency & sample" hint="reproducibility and what the filters left">
        <div className="grid gap-4 lg:grid-cols-2">
          <Rows rows={[
            ["Spec hash", <span key="h" className="font-mono text-[11px]">{cons.spec_hash ?? "—"}</span>],
            ["Seed", String(cons.seed ?? "—")],
            ["Hyperparameter stability (CV)", n(hyper.coefficient_of_variation, 3)],
            ["Complexity stable across windows",
              hyper.available ? String(hyper.stable) : "—"],
          ]} />
          <Rows rows={[
            ["Panel rows used", String(mon.rows_out ?? "—")],
            ["Row retention after filters", p(mon.row_retention, 0)],
            ["Window", `${mon.first_month ?? "—"} → ${mon.last_month ?? "—"}`],
            ["Fit seconds", n(mon.fit_seconds, 1)],
          ]} />
        </div>
      </Section>
    </div>
  );
}
