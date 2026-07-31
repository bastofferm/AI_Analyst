// Stance / tone badges with the MZQA 10%-alpha fills.

const STANCE_STYLE: Record<string, string> = {
  attractive: "badge-pos",
  fair: "badge-neu",
  expensive: "badge-neg",
};

const STANCE_LABEL: Record<string, string> = {
  attractive: "▲ Attractive",
  fair: "— Fair",
  expensive: "▼ Expensive",
};

export function StanceBadge({ stance }: { stance: string | null | undefined }) {
  const key = (stance || "").toLowerCase();
  const cls = STANCE_STYLE[key] || "badge-neu";
  const label = STANCE_LABEL[key] || stance || "—";
  return (
    <span className={`inline-block rounded px-2 py-0.5 text-[10.5px] font-semibold ${cls}`}>{label}</span>
  );
}

/** Generic positive/negative/neutral pill, e.g. for tone or deltas. */
export function TonePill({
  value,
  label,
  title,
}: {
  value: number | null | undefined;
  label?: string;
  title?: string;
}) {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    return <span className="badge-neu inline-block rounded px-2 py-0.5 text-[10.5px] font-semibold">n/a</span>;
  }
  const cls = value > 0.1 ? "badge-pos" : value < -0.1 ? "badge-neg" : "badge-neu";
  const arrow = value > 0.1 ? "▲" : value < -0.1 ? "▼" : "—";
  return (
    <span className={`inline-block rounded px-2 py-0.5 text-[10.5px] font-semibold ${cls}`} title={title}>
      {arrow} {label ?? value.toFixed(2)}
    </span>
  );
}
