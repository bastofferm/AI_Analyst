// Plain-English copy deck for the consumer app.
//
// Every major section of the product renders an InfoBox fed from SECTION_COPY, and
// jargon terms render a HelpTip fed from GLOSSARY. Keeping all explainer text in one
// file keeps the tone consistent: friendly, concrete, no unexplained jargon, and honest
// about what the numbers can and cannot tell you.

export type SectionCopy = { title: string; body: string };

export const SECTION_COPY = {
  verdict: {
    title: "What is this verdict?",
    body:
      "The committee's bottom line. \"Fair value\" is what our AI analyst team estimates one share is really worth after studying the company's official filings. Compare it to the current share price: if fair value is well above the price, the stock may be a bargain; well below, it may be expensive. It is an informed estimate — not a guarantee, and not personal advice.",
  },
  snapshot: {
    title: "What am I looking at while I wait?",
    body:
      "A quick statistical snapshot of the company pulled straight from our database — size, growth, valuation and the recent share price — so you can get familiar with the business while the committee does its deeper work.",
  },
  debate: {
    title: "Why a debate?",
    body:
      "Instead of one opinion, your analysis is argued out by a committee: The Advocate makes the strongest honest case for the stock, The Challenger stress-tests that case on the same evidence, and The Auditor checks the accounting. Sector specialists add their own lens. A lead analyst then weighs all sides. Disagreement is the point — it stops one rosy (or gloomy) story from going unchallenged.",
  },
  scenarios: {
    title: "What are scenarios?",
    body:
      "Nobody knows the future, so the committee models three of them: an Upside case (things go well), a Base case (business as usual) and a Downside case (things go wrong). Each gets a probability weight, and the weighted average of the three share values gives one balanced fair value. The spread between Upside and Downside also tells you how uncertain this stock's story is.",
  },
  footballField: {
    title: "How to read this chart",
    body:
      "Analysts call this a \"football field\". Each bar is one valuation method's answer to the question \"what is a share worth?\" — a low-to-high range with a tick at the midpoint. The dashed red line is today's actual share price. If the price sits to the left of most bars, the stock looks cheap by those methods; to the right, expensive. When several independent methods agree, the estimate deserves more trust.",
  },
  waterfall: {
    title: "From business value to share value",
    body:
      "This bridge shows the arithmetic behind the fair value. We start with the estimated value of the whole business (based on the cash we project it will generate), subtract what it owes (net debt), and what remains belongs to shareholders — divide by the number of shares and you get value per share.",
  },
  fcf: {
    title: "Why future cash matters",
    body:
      "A company is ultimately worth the cash it will hand back over its lifetime. The tall bars are the free cash flow the committee projects for each of the next years; the shorter bars are the same cash \"discounted\" — reduced to reflect that money arriving years from now is worth less than money today. Adding up the discounted bars (plus a value for everything beyond the forecast) is the heart of a DCF valuation.",
  },
  sensitivity: {
    title: "What if the assumptions are wrong?",
    body:
      "Every valuation rests on assumptions. This grid stress-tests the two biggest ones: how fast revenue grows (rows) and the discount rate applied to future cash (columns, the WACC). Each cell is the share value under that combination — green means above today's price, red below. If the grid is green almost everywhere, the thesis survives even pessimistic assumptions; if only one corner is green, the case is fragile.",
  },
  sotp: {
    title: "Valuing the parts separately",
    body:
      "Big companies are often several businesses in one — think cloud, devices and advertising under one roof. \"Sum of the parts\" values each division on its own growth and profitability, then adds them up. It often reveals value (or weakness) that a single blended number hides.",
  },
  reverseDcf: {
    title: "What the market already believes",
    body:
      "Instead of asking \"what is the stock worth?\", this flips the question: \"how fast must the company grow to justify today's price?\" If the market-implied growth is far above what the committee thinks is realistic, a lot of optimism is already baked in — and vice versa. It's one of the most honest checks in investing.",
  },
  health: {
    title: "What is company health?",
    body:
      "Charts of the company's recent operating reality: quarterly revenue momentum, how much cash it generates and returns to shareholders (dividends and buybacks), and whether the returns it earns on new investment beat its cost of capital. Healthy businesses fund their own growth and still have cash left over.",
  },
  quarterly: {
    title: "Quarterly momentum",
    body:
      "The last few quarters of revenue with the year-over-year growth line. Direction matters more than any single quarter — accelerating growth often precedes good news, decelerating growth precedes disappointment.",
  },
  capitalReturns: {
    title: "Cash back to shareholders",
    body:
      "The stacked bars show dividends and share buybacks — the two ways companies hand cash back to owners. The line is free cash flow, the cash actually available. Sustainable returns fit inside that line; payouts persistently above it are being borrowed or drawn from reserves.",
  },
  peerMultiples: {
    title: "Valuation vs. the neighbours",
    body:
      "How this stock's valuation multiple compares with companies in the same sector. The amber dashed line is the peer median. Cheaper than peers isn't automatically good (there may be a reason), but a big gap either way is worth understanding.",
  },
  tone: {
    title: "Reading between management's lines",
    body:
      "Our system reads the \"Management Discussion & Analysis\" section of the company's official annual report — the part where executives explain the year in their own words — and scores how confident or cautious the language is, compared to peers. Managers choose words carefully; a shift in tone often front-runs a shift in results.",
  },
  ownership: {
    title: "Who owns this stock?",
    body:
      "Large US investment managers must disclose their holdings every quarter in \"13F\" filings. This shows the biggest professional holders and whether they added (green) or trimmed (red) last quarter. Smart-money moves aren't gospel — the data arrives with a delay — but heavy accumulation or distribution is context worth having.",
  },
  memo: {
    title: "The full write-up",
    body:
      "The committee's complete investment memo, written by the lead analyst after the debate. It reads like the research note a professional fund would circulate internally: thesis, risks, valuation and what would change the committee's mind. Everything above on this page is a visual summary of what's argued here.",
  },
  toolkit: {
    title: "For advanced users",
    body:
      "The professional workbench behind the friendly summary: the raw evidence bundle with citations, the data-quality audit that reconciles our figures against independent sources, and the print-grade institutional report. Nothing here is required reading — it exists so every number above can be traced to a source.",
  },
  followUp: {
    title: "Ask the committee",
    body:
      "Not satisfied with part of the analysis? Push back. Ask the committee to stress an assumption, weigh a risk more heavily, or explain a disagreement — it will revise its memo and show you what changed. Each follow-up takes about a minute.",
  },
  dataConfidence: {
    title: "What does data confidence mean?",
    body:
      "Before any opinion is formed, we audit the underlying financial data: do the statements add up, and do our standardized figures match independent sources? A high score means the committee argued from solid numbers. A low score doesn't kill the analysis, but it means conclusions deserve extra scepticism — which is why we show it up front.",
  },
  companyData: {
    title: "Where do these numbers come from?",
    body:
      "This is the raw data basis for the company — the standardized line items we extract from its official filings (SEC EDGAR for US names, EDINET for Japan), the ratios computed from them, and the recent share price. It is exactly the material the committee argues from: if a number looks odd here, treat the AI's conclusions with extra care. No model output on this card — just the filings, year by year.",
  },
  explore: {
    title: "How to use Explore",
    body:
      "Browse the full coverage universe — the US, Japan and international markets — filtered by sector, industry and company size. Tap any company's Analyze button to send it to the committee for a full work-up. Sorting by market cap first is a good way to start with names you know.",
  },
  compare: {
    title: "What is a sector ranking?",
    body:
      "Pick a sector — say, banks or semiconductors — and the committee analyzes the whole group at once, ranking every name from most to least attractive relative to its peers, with a short reason for each. It's the fastest way to answer \"which one should I look at first?\"",
  },
  ideasScan: {
    title: "How the scan works",
    body:
      "A one-click screen for interesting stocks: it looks for companies that are cheap relative to the cash they generate, still growing, and whose management sounds confident in official filings. Each name gets an interest score from 0–100. Treat the list as a starting point for research — always run the full committee before drawing conclusions.",
  },
  ideasPrompt: {
    title: "Describe what you want",
    body:
      "Type what you're looking for in plain language — \"cheap large-cap software with fast growth\", \"Japanese manufacturers with high dividends\" — and the AI translates it into a proper screen, then has the committee rank the results. You don't need to know any financial jargon; that's our job.",
  },
  committee: {
    title: "Who's on the committee?",
    body:
      "Three core voices — The Advocate (builds the case), The Challenger (stress-tests it) and The Auditor (checks the books) — joined by five sector-aware specialists covering growth, earnings quality, relative value, macro and stress-testing. You can also deploy your own custom analyst with any mandate you like; it joins every future debate.",
  },
} satisfies Record<string, SectionCopy>;

export type SectionCopyKey = keyof typeof SECTION_COPY;

// ---------------------------------------------------------------------------
// Glossary — inline plain-English definitions for HelpTip.
// ---------------------------------------------------------------------------

export const GLOSSARY: Record<string, string> = {
  "fair value":
    "The committee's estimate of what one share is actually worth, based on the cash the business should generate — as opposed to whatever the market happens to be paying today.",
  "market cap":
    "The total price tag of the whole company: share price × number of shares.",
  "P/E":
    "Price-to-earnings ratio: the share price divided by one year's profit per share. Roughly, how many years of current profit you pay for the stock. Lower can mean cheaper — or in trouble.",
  "P/B":
    "Price-to-book ratio: the share price relative to the company's accounting net worth per share.",
  "EV/EBITDA":
    "Enterprise value ÷ EBITDA. A price tag for the whole business (debt included) relative to raw operating profit. Handy for comparing firms with different debt levels; lower is cheaper, all else equal.",
  "FCF yield":
    "Free cash flow ÷ market value: the spare cash the business generates each year as a % of its price. Like an interest rate the company earns for its owners.",
  "free cash flow":
    "Cash left after running the business and maintaining/expanding its assets — the money that can fund dividends, buybacks or acquisitions.",
  DCF:
    "Discounted cash flow: valuing a company by projecting the cash it will produce and translating it into today's money. The workhorse of professional valuation.",
  WACC:
    "Weighted average cost of capital — the discount rate for future cash. Think of it as the annual return investors demand for holding this company's risk. Higher WACC ⇒ future cash is worth less today ⇒ lower valuation.",
  "terminal growth":
    "The steady growth rate assumed forever after the explicit forecast years. Small changes move valuations a lot, which is why we stress-test it.",
  "net debt":
    "Total borrowings minus cash on hand. What's left for shareholders is business value minus net debt.",
  "enterprise value":
    "The value of the entire business operation — what it would cost to buy the whole company, debt and all.",
  SOTP:
    "Sum of the parts: valuing each business division separately and adding them up.",
  "reverse DCF":
    "Running the valuation backwards: taking today's share price and solving for the growth the market must be assuming. A reality check on expectations.",
  "13F":
    "A quarterly SEC filing where large US investment managers must reveal what they hold. It's how we see what professional money is doing (with a delay).",
  "MD&A":
    "\"Management's Discussion & Analysis\" — the section of the annual report where executives explain results in their own words. We score the tone of this language.",
  "dividend yield":
    "Annual dividends per share ÷ share price: the cash income the stock pays you, as a percentage.",
  buybacks:
    "The company buying its own shares off the market — remaining shareholders own a bigger slice of the same pie.",
  "revenue CAGR":
    "Compound annual growth rate of sales over a period — the smoothed 'per-year' growth rate.",
  "rev YoY":
    "Revenue growth versus the same period a year earlier.",
  GICS:
    "The standard industry classification system (sectors → industries) used to group comparable companies.",
  stance:
    "The committee's relative-value call within a group: attractive (better value than peers), fair, or expensive.",
  "composite score":
    "How a company scores against the others in its group. Each metric (P/E, EV/EBITDA, FCF yield, growth, margins) is measured relative to the peers, weighted, and added up. Zero is average for the group; positive is better value, negative worse. It has no fixed range — it depends on how spread out the group is.",
  "z-score":
    "How far a number sits from the group average, measured in standard deviations. 0 = exactly average, +1 = one deviation above, −1 = one below. It lets a P/E and a growth rate be compared on the same scale.",
  "interest score":
    "The scanner's 0–100 rating of how much a stock deserves a closer look — combining cheapness, growth and management tone. Not a buy signal.",
  ROIC:
    "Return on invested capital: profit generated per dollar invested in the business. Above WACC = creating value; below = destroying it.",
  "implied growth":
    "The growth rate baked into today's share price — what buyers at this price are implicitly betting on.",
  upside:
    "The gap between estimated fair value and the current price, as a %. Positive = potentially undervalued. An estimate, not a promise.",
  WACC_axis: "Each column assumes a different discount rate (cost of capital).",
  "data confidence":
    "Our 0–100 audit score of the underlying financial data quality before any opinions were formed.",
};
