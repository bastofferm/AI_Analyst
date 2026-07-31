import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        bg:           "#F5F4F0",
        panel:        "#FBFAF7",
        paper:        "#F8F7F4",
        navy:         "#2F4D73",
        "navy-2":     "#476D99",
        "navy-3":     "#6B86A8",
        muted:        "#6F7890",
        border:       "#DDD8CD",
        "border-soft": "#EEECE5",
        amber:        "#F59E0B",
        green:        "#16A34A",
        red:          "#DC2626",
      },
      fontFamily: {
        sans: ["Inter", "Segoe UI", "-apple-system", "BlinkMacSystemFont", "sans-serif"],
        mono: ["Consolas", "Courier New", "monospace"],
      },
      fontSize: {
        "2xs": ["10px", "14px"],
      },
      letterSpacing: {
        tightish: "-0.01em",
        label: "0.14em",
      },
    },
  },
  plugins: [],
};

export default config;
