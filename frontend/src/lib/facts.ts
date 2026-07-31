// Infotainment deck for the multi-minute committee waits.
//
// Three flavors, deliberately mixed: how THIS product works (so the wait
// teaches the app), plain-English investing concepts, and verifiable market
// history. Everything is educational context — no predictions, no advice.

export type MarketFact = {
  kind: "how it works" | "investing 101" | "market history";
  text: string;
};

export const MARKET_FACTS: MarketFact[] = [
  // ---------------------------------------------------------- how it works
  {
    kind: "how it works",
    text: "The Challenger on the committee isn't negative for its own sake — forcing a strong counter-argument is one of the oldest defenses against wishful thinking in investing.",
  },
  {
    kind: "how it works",
    text: "Every number the committee argues from is pulled straight from the company's official filings — the same documents auditors and regulators read.",
  },
  {
    kind: "how it works",
    text: "The Auditor ignores the story entirely and checks whether the accounting identities actually add up before any opinion is allowed to form.",
  },
  {
    kind: "how it works",
    text: "The sensitivity grid you'll see stress-tests the valuation against different growth and discount-rate assumptions — a fair value that survives pessimism deserves more trust.",
  },
  {
    kind: "how it works",
    text: "Management's word choices are data too: the committee scores the tone of the annual report's discussion section against peers, because language often shifts before results do.",
  },
  {
    kind: "how it works",
    text: "The reverse DCF flips the usual question: instead of guessing what the stock is worth, it computes how fast the company must grow to justify today's price.",
  },
  // ---------------------------------------------------------- investing 101
  {
    kind: "investing 101",
    text: "The rule of 72: divide 72 by an annual return to estimate doubling time. At 7% a year, money doubles roughly every decade.",
  },
  {
    kind: "investing 101",
    text: "A company's share price tells you nothing by itself — a $900 stock can be cheaper than a $9 one. What matters is price relative to the cash the business produces.",
  },
  {
    kind: "investing 101",
    text: "Fees compound just like returns. A 1% annual fee, left running for 30 years, can quietly consume roughly a quarter of a portfolio's final value.",
  },
  {
    kind: "investing 101",
    text: "Diversification is the only free lunch in finance: combining imperfectly-correlated assets can lower risk without lowering expected return.",
  },
  {
    kind: "investing 101",
    text: "Volatility and risk aren't the same thing. A stock that swings wildly but compounds for decades hurt nobody who held on; a stable price that quietly erodes did.",
  },
  {
    kind: "investing 101",
    text: "Free cash flow is harder to fake than earnings — profits are an opinion shaped by accounting choices, but cash leaving or entering the bank account is a fact.",
  },
  {
    kind: "investing 101",
    text: "When a company buys back shares below intrinsic value, every remaining shareholder's slice of the business quietly grows without them doing anything.",
  },
  {
    kind: "investing 101",
    text: "High growth creates value only when returns on new investment beat the cost of capital — growth that earns less than it costs actively destroys value.",
  },
  {
    kind: "investing 101",
    text: "The market being 'expensive' or 'cheap' matters less to your outcome than how long you stay invested — time in the market has historically beaten timing the market.",
  },
  // ---------------------------------------------------------- market history
  {
    kind: "market history",
    text: "On Black Monday — October 19, 1987 — the Dow fell 22.6% in a single session, still the largest one-day percentage drop in its history.",
  },
  {
    kind: "market history",
    text: "A landmark study found that since 1926, just 4% of US stocks accounted for ALL of the market's wealth creation above Treasury bills — most stocks individually lost to cash.",
  },
  {
    kind: "market history",
    text: "Missing only the 10 best trading days over multiple decades has historically cut a stock investor's total return roughly in half — and those days tend to cluster near the worst ones.",
  },
  {
    kind: "market history",
    text: "During the Dutch tulip mania of 1637, single bulbs briefly traded for more than an Amsterdam canal house — the canonical reminder that prices can detach from value.",
  },
  {
    kind: "market history",
    text: "The Nasdaq peaked in March 2000, then fell nearly 80%. It took about 15 years to reclaim its dot-com high — even great technology can be a poor investment at the wrong price.",
  },
  {
    kind: "market history",
    text: "Reinvested dividends, not price gains, have supplied a large share of the stock market's long-run total return — compounding does the heavy lifting.",
  },
  {
    kind: "market history",
    text: "US stocks have averaged roughly 10% a year over the last century — but almost never returned close to 10% in any single year. The average hides the ride.",
  },
  {
    kind: "market history",
    text: "In 1997 Apple was weeks from bankruptcy and took a $150M lifeline from Microsoft. Businesses change — which is why analysis starts from today's filings, not old reputations.",
  },
];

/** A stable "random" starting index so consecutive runs don't always open on fact #1. */
export function factStartIndex(seed: number): number {
  return Math.abs(Math.floor(seed)) % MARKET_FACTS.length;
}
