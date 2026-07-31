export function Skeleton({ className }: { className?: string }) {
  return <div className={`skeleton ${className || ""}`} aria-hidden="true" />;
}

export function SkeletonRows({ rows = 6, className }: { rows?: number; className?: string }) {
  return (
    <div className={`flex flex-col gap-2 ${className || ""}`} aria-hidden="true">
      {Array.from({ length: rows }).map((_, i) => (
        <div key={i} className="skeleton h-8 w-full" style={{ opacity: 1 - i * 0.09 }} />
      ))}
    </div>
  );
}
