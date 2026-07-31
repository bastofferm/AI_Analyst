// The MZQA sine-wave signal glyph — same path as the terminal's nav mark.

export function Logomark({ size = 26, stroke = "#2F4D73" }: { size?: number; stroke?: string }) {
  return (
    <svg
      viewBox="0 0 280 140"
      width={size * 2}
      height={size}
      aria-hidden="true"
      fill="none"
    >
      <line x1="0" y1="70" x2="280" y2="70" stroke={stroke} strokeWidth="2" opacity="0.28" />
      <path
        d="M0,70 C35,70 35,10 70,10 C105,10 105,130 140,130 C175,130 175,10 210,10 C245,10 245,70 280,70"
        stroke={stroke}
        strokeWidth="10"
        strokeLinecap="round"
      />
    </svg>
  );
}
