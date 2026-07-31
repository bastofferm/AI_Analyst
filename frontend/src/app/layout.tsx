import "./globals.css";

export const metadata = {
  title: "MZQA — AI Investment Committee",
  description:
    "Nine AI analysts debate any stock over its official filings and hand you a plain-English verdict with the charts that matter. Independent research — not investment advice.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
