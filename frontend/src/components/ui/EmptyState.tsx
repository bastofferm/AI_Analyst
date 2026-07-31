export function EmptyState({ title, hint }: { title: string; hint?: string }) {
  return (
    <div className="rounded-md border border-dashed border-border bg-white/40 px-4 py-6 text-center">
      <div className="text-[12px] font-medium text-muted">{title}</div>
      {hint ? <div className="mt-1 text-[11px] text-muted/80">{hint}</div> : null}
    </div>
  );
}
