"use client";

// Animated MZQA "vector" glyphs — the decorative, always-moving SVGs that sit in
// the bottom-right of a card and brighten on hover. Ported from the terminal's
// home-feature-banners.tsx so the consumer app speaks the same visual language.
//
// All of them reuse keyframes that already live in globals.css
// (mzqa-macro-line / mzqa-macro-dot / mzqa-equity-bar / mzqa-portfolio-bar /
// mzqa-cube-wrap), so nothing new is animated from JS.

type VectorProps = {
  /** Stroke/fill colour. Defaults to the parchment tone used on navy panels. */
  tone?: string;
  className?: string;
  /** `corner` (default) is the dimmed decoration anchored bottom-right of a card.
   *  `badge` renders the same glyph inline and fully visible, for use as a card's
   *  identifying icon rather than background texture — the caller sizes the box. */
  variant?: VectorVariant;
};

type VectorVariant = "corner" | "badge";

const DEFAULT_TONE = "#F0EDE6";

/** Shared frame. In `corner` mode it is anchored bottom-right and dimmed until the
 *  parent `.group` is hovered; in `badge` mode it fills its container at full
 *  strength so the animation reads as part of the card's identity. */
function Frame({
  children,
  className = "",
  size = "h-[52px] w-[78px]",
  variant = "corner",
}: {
  children: React.ReactNode;
  className?: string;
  size?: string;
  variant?: VectorVariant;
}) {
  const placement =
    variant === "badge"
      ? "h-full w-full opacity-100"
      : `absolute bottom-3 right-3.5 ${size} opacity-45 transition duration-300 group-hover:scale-[1.04] group-hover:opacity-90`;
  return (
    <svg
      className={`pointer-events-none ${placement} ${className}`}
      viewBox="0 0 140 100"
      fill="none"
      aria-hidden="true"
    >
      {children}
    </svg>
  );
}

/** The Advocate — a drawn-in rally with a pulsing high. */
export function AdvocateVector({ tone = DEFAULT_TONE, className, variant }: VectorProps) {
  return (
    <Frame className={className} variant={variant}>
      <line x1="10" y1="82" x2="130" y2="82" stroke={tone} strokeWidth="0.5" opacity="0.4" />
      <path
        className="mzqa-macro-line"
        d="M10,78 C34,74 46,58 66,48 C88,37 100,40 130,16"
        stroke={tone}
        strokeWidth="1.6"
        strokeLinecap="round"
      />
      <circle className="mzqa-macro-dot" cx="130" cy="16" r="3" fill="#7FBF9A" />
    </Frame>
  );
}

/** The Challenger — a case put under load: pressure bears down on the middle, the
 *  line dips and recovers, and a lens sits over the point of maximum stress. The
 *  old glyph here was a plain falling chart, which read as "decline" rather than
 *  "scrutiny" and no longer matches the role. */
export function ChallengerVector({ tone = DEFAULT_TONE, className, variant }: VectorProps) {
  return (
    <Frame className={className} variant={variant}>
      <line x1="10" y1="84" x2="130" y2="84" stroke={tone} strokeWidth="0.5" opacity="0.4" />
      {/* Applied pressure: short ticks pushing down onto the dip. */}
      {[
        { x: 52, d: "0s" },
        { x: 70, d: "0.3s" },
        { x: 88, d: "0.6s" },
      ].map((p) => (
        <rect
          key={p.x}
          className="mzqa-equity-bar"
          x={p.x - 1}
          y="12"
          width="2"
          height="14"
          rx="1"
          fill={tone}
          opacity="0.5"
          style={{ animationDelay: p.d }}
        />
      ))}
      <path
        className="mzqa-macro-line"
        d="M10,44 C30,45 46,60 70,63 C94,66 108,52 130,38"
        stroke={tone}
        strokeWidth="1.6"
        strokeLinecap="round"
      />
      {/* The lens over the point of maximum stress. */}
      <circle className="mzqa-macro-dot" cx="70" cy="63" r="10" stroke="#E8B4B8" strokeWidth="1.4" fill="none" />
      <line x1="77" y1="70" x2="86" y2="79" stroke="#E8B4B8" strokeWidth="1.4" strokeLinecap="round" />
    </Frame>
  );
}

/** The Auditor — a ledger grid being ticked off, row by row. */
export function AuditorVector({ tone = DEFAULT_TONE, className, variant }: VectorProps) {
  return (
    <Frame className={className} variant={variant}>
      <rect x="18" y="16" width="104" height="68" rx="2" stroke={tone} strokeWidth="1" opacity="0.75" />
      {[33, 50, 67].map((y) => (
        <line key={y} x1="18" y1={y} x2="122" y2={y} stroke={tone} strokeWidth="0.5" opacity="0.45" />
      ))}
      <line x1="52" y1="16" x2="52" y2="84" stroke={tone} strokeWidth="0.5" opacity="0.45" />
      {[
        { y: 24, w: 30, d: "0s" },
        { y: 41, w: 44, d: "0.5s" },
        { y: 58, w: 24, d: "1s" },
      ].map((r) => (
        <rect
          key={r.y}
          className="mzqa-equity-bar"
          x="58"
          y={r.y}
          width={r.w}
          height="4"
          rx="2"
          fill={tone}
          opacity="0.55"
          style={{ animationDelay: r.d }}
        />
      ))}
      <path d="M28,70 l6,7 l11,-15" stroke="#7FBF9A" strokeWidth="2" strokeLinecap="round" fill="none" />
    </Frame>
  );
}

/** Five specialists — five lenses breathing at different rates. */
export function SpecialistsVector({ tone = DEFAULT_TONE, className, variant }: VectorProps) {
  const bars = [
    { x: 20, h: 40 },
    { x: 44, h: 62 },
    { x: 68, h: 30 },
    { x: 92, h: 52 },
    { x: 116, h: 44 },
  ];
  return (
    <Frame className={className} variant={variant}>
      <line x1="12" y1="84" x2="132" y2="84" stroke={tone} strokeWidth="0.5" opacity="0.4" />
      {bars.map((b, i) => (
        <rect
          key={b.x}
          className="mzqa-equity-bar"
          x={b.x - 7}
          y={84 - b.h}
          width="14"
          height={b.h}
          rx="1.5"
          fill={tone}
          opacity={0.32 + i * 0.09}
          style={{ animationDelay: `${i * 0.22}s` }}
        />
      ))}
    </Frame>
  );
}

/** Ideas scanner — a radar sweep over a scatter of candidate names. */
export function ScannerVector({ tone = DEFAULT_TONE, className }: VectorProps) {
  return (
    <Frame className={className} size="h-[56px] w-[84px]">
      <circle cx="70" cy="52" r="38" stroke={tone} strokeWidth="0.6" opacity="0.35" fill="none" />
      <circle cx="70" cy="52" r="24" stroke={tone} strokeWidth="0.6" opacity="0.3" fill="none" />
      <circle cx="70" cy="52" r="11" stroke={tone} strokeWidth="0.6" opacity="0.25" fill="none" />
      <path
        className="mzqa-macro-line"
        d="M70,52 L108,52 A38,38 0 0,0 70,14 Z"
        fill={tone}
        opacity="0.12"
        stroke={tone}
        strokeWidth="0.8"
      />
      {[
        { cx: 52, cy: 36 },
        { cx: 92, cy: 66 },
        { cx: 60, cy: 72 },
      ].map((p) => (
        <circle key={`${p.cx}-${p.cy}`} cx={p.cx} cy={p.cy} r="2" fill={tone} opacity="0.6" />
      ))}
      <circle className="mzqa-macro-dot" cx="88" cy="34" r="3" fill="#F59E0B" />
    </Frame>
  );
}

/** Sector ranking — a ranked field, tallest to shortest. */
export function RankingVector({ tone = DEFAULT_TONE, className }: VectorProps) {
  const bars = [72, 58, 46, 34, 24];
  return (
    <Frame className={className} size="h-[56px] w-[84px]">
      <line x1="10" y1="86" x2="132" y2="86" stroke={tone} strokeWidth="0.5" opacity="0.4" />
      {bars.map((h, i) => (
        <g key={h}>
          <rect
            className="mzqa-portfolio-bar"
            x={14 + i * 24}
            y={86 - h}
            width="16"
            height={h}
            rx="1.5"
            fill={i === 0 ? "#7FBF9A" : tone}
            opacity={i === 0 ? 0.85 : 0.5 - i * 0.06}
            style={{ animationDelay: `${i * 0.14}s` }}
          />
        </g>
      ))}
      <path d="M14,14 L30,14" stroke="#7FBF9A" strokeWidth="1.5" strokeLinecap="round" />
    </Frame>
  );
}
