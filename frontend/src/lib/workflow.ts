// Plain-English maps of what actually runs when you press the button.
//
// The topology is transcribed from the real backend graphs — do not invent
// steps here:
//   single stock  backend/ai_analyst/committee/graph.py::build_committee_graph
//   sector        backend/api/routers/ai_committee_group.py -> committee/group.py
//
// The copy, though, is written for a reader who does not care what a DAG is.
// `kind` is the honest part: it says which steps are plain arithmetic and which
// ones actually think.

export type NodeKind =
  | "data"      // looks something up
  | "maths"     // pure arithmetic, same answer every time
  | "decision"  // a fork in the road
  | "ai"        // a model is doing the work
  | "result";   // what lands in front of you

export type WorkflowNode = {
  key: string;
  label: string;
  /** Second line inside the box — three or four words, no jargon. */
  caption: string;
  /** The full explanation, shown when this node is highlighted. */
  detail: string;
  /** A one-line analogy. This is the infotainment bit. */
  analogy?: string;
  /** Rough wall-clock, shown as a chip. */
  takes?: string;
  kind: NodeKind;
  col: number;
  row: number;      // 0 = the centre lane
  /** Which pipeline step (lib/pipeline.ts) lights this node up during a run. */
  step: string;
};

export type WorkflowEdge = {
  from: string;
  to: string;
  conditional?: boolean;
  label?: string;
};

export type WorkflowSpec = {
  id: string;
  title: string;
  subtitle: string;
  nodes: WorkflowNode[];
  edges: WorkflowEdge[];
};

// ------------------------------------------------------------- sector ranking
export const GROUP_WORKFLOW: WorkflowSpec = {
  id: "group",
  title: "What happens when you press the button",
  subtitle: "Seven steps — and only one of them is the AI.",
  nodes: [
    {
      key: "universe",
      label: "Find the companies",
      caption: "your sector → a list",
      detail:
        "Your sector choice becomes a search over every company we cover, biggest first. Nothing is judged yet — this is just deciding who is in the room.",
      analogy: "Like drawing up the guest list before the argument starts.",
      takes: "instant",
      kind: "data",
      col: 0,
      row: 0,
      step: "universe",
    },
    {
      key: "metrics",
      label: "Look up the numbers",
      caption: "nine figures each",
      detail:
        "For every company we pull the same nine figures from the database — how expensive it is, how fast it grows, how profitable it is. Same numbers for everyone, same moment in time.",
      analogy: "Everyone sits the same exam, so the marks can be compared.",
      takes: "a second",
      kind: "data",
      col: 1,
      row: 0,
      step: "scoring",
    },
    {
      key: "composite",
      label: "Score them all",
      caption: "cheap + growing wins",
      detail:
        "Each figure is compared against the rest of the group, weighted by how much it matters, and added up. Cheap and growing scores well; expensive and stalling scores badly. This is pure arithmetic — run it twice, get the same answer.",
      analogy: "Grading on a curve: you are scored against the class, not an absolute standard.",
      takes: "instant",
      kind: "maths",
      col: 2,
      row: 0,
      step: "scoring",
    },
    {
      key: "keygate",
      label: "Is the AI available?",
      caption: "no key → stop here",
      detail:
        "Everything above works without any AI at all. If no API key is set up, the run ends here and you get the numerical ranking on its own — still a complete answer, just without the commentary.",
      analogy: "The scoreboard works even if the pundits do not turn up.",
      kind: "decision",
      col: 3,
      row: 0,
      step: "deliberate",
    },
    {
      key: "debate",
      label: "The debate",
      caption: "one AI, one verdict",
      detail:
        "The scored table goes to the AI, which argues the group as a whole: which names look genuinely attractive, which look expensive, and one sentence of reasoning for each. It cannot change the numbers — only interpret them.",
      analogy: "The numbers set the table; the analyst argues over dinner.",
      takes: "most of the wait",
      kind: "ai",
      col: 4,
      row: 0,
      step: "deliberate",
    },
    {
      key: "merge",
      label: "Final ranking",
      caption: "opinions meet maths",
      detail:
        "The AI's stances and reasons are attached to the scored rows. If it mentions a company that was never in the group, that line is thrown away — it cannot smuggle names in.",
      analogy: "The referee checks the pundit only talked about players on the pitch.",
      takes: "instant",
      kind: "maths",
      col: 5,
      row: 0,
      step: "memo",
    },
    {
      key: "report",
      label: "Your verdict",
      caption: "ranked, with reasons",
      detail:
        "The ranked list, the reason behind every position, and the full score breakdown for whichever company you click. Every number traces back to the database.",
      analogy: "The league table — plus the working, so you can check it.",
      kind: "result",
      col: 6,
      row: 0,
      step: "memo",
    },
  ],
  edges: [
    { from: "universe", to: "metrics" },
    { from: "metrics", to: "composite" },
    { from: "composite", to: "keygate" },
    { from: "keygate", to: "debate" },
    { from: "debate", to: "merge" },
    { from: "merge", to: "report" },
    { from: "keygate", to: "merge", conditional: true, label: "no AI key" },
  ],
};

// ------------------------------------------------------------ single company
export const COMMITTEE_WORKFLOW: WorkflowSpec = {
  id: "committee",
  title: "What the committee actually does",
  subtitle: "Nine analysts, three rounds — but the maths is settled before anyone speaks.",
  nodes: [
    {
      key: "completeness",
      label: "Check the filings",
      caption: "is anything missing?",
      detail:
        "Before anyone forms an opinion we check the company's official filings are actually complete. If the statements have holes, the run stops here rather than guessing.",
      analogy: "No point debating a case file with pages torn out.",
      takes: "seconds",
      kind: "decision",
      col: 0,
      row: 0,
      step: "gate",
    },
    {
      key: "dq",
      label: "Check the maths",
      caption: "do the books balance?",
      detail:
        "The accounts are re-checked against the rules they must obey — assets equal liabilities plus equity, and so on. Numbers that do not reconcile get flagged before they can mislead an analyst.",
      analogy: "The bank statement has to match the chequebook.",
      takes: "seconds",
      kind: "decision",
      col: 1,
      row: 0,
      step: "gate",
    },
    {
      key: "engine",
      label: "Do the valuation",
      caption: "what is it worth?",
      detail:
        "The heavy arithmetic: projecting the cash the business should generate, discounting it back to today, valuing the divisions separately, and stress-testing all of it. No AI involved — this part is the same every time.",
      analogy: "The spreadsheet work a junior analyst would spend a week on.",
      takes: "about a third of the run",
      kind: "maths",
      col: 2,
      row: 0,
      step: "engine",
    },
    {
      key: "news",
      label: "Read the news",
      caption: "and the wider economy",
      detail:
        "Recent news about the company is scored for sentiment, and the economic backdrop is pulled in — rates, growth, inflation — because a valuation has to survive the world it lives in.",
      analogy: "Checking the weather before deciding what the picnic is worth.",
      kind: "data",
      col: 3,
      row: -1,
      step: "context",
    },
    {
      key: "ownership",
      label: "Who owns it",
      caption: "the big funds' moves",
      detail:
        "Large investment managers must publish what they hold every quarter. We look at who has been buying and who has been selling.",
      analogy: "Seeing which way the professionals voted with their money.",
      kind: "data",
      col: 3,
      row: 0,
      step: "context",
    },
    {
      key: "dqagent",
      label: "Sanity-check",
      caption: "an AI second opinion",
      detail:
        "A model reads the data-quality report and suggests where an odd-looking figure came from — a mislabelled line item, a restatement — so the analysts are not arguing about a typo.",
      analogy: "A proofreader who flags the suspicious footnote.",
      kind: "ai",
      col: 3,
      row: 1,
      step: "context",
    },
    {
      key: "advocate",
      label: "The Advocate",
      caption: "builds the case",
      detail:
        "Builds the strongest honest case for the stock — growth, competitive advantages, what the market may be underrating. Every claim has to point at the evidence gathered above.",
      analogy: "The one who makes the case.",
      kind: "ai",
      col: 4,
      row: -1,
      step: "tribunal",
    },
    {
      key: "challenger",
      label: "The Challenger",
      caption: "stress-tests it",
      detail:
        "Pressure-tests the same evidence: what could break, what is already priced in, what the optimistic case is quietly assuming. A strong challenge makes the final answer more reliable.",
      analogy: "The one who asks the hard questions.",
      kind: "ai",
      col: 4,
      row: 0,
      step: "tribunal",
    },
    {
      key: "auditor",
      label: "Auditor + specialists",
      caption: "six more lenses",
      detail:
        "The Auditor ignores the story and checks the accounting quality, joined by five specialists covering growth, earnings quality, relative value, the economy and stress-testing.",
      analogy: "The expert witnesses.",
      takes: "the longest stretch",
      kind: "ai",
      col: 4,
      row: 1,
      step: "tribunal",
    },
    {
      key: "lead",
      label: "Lead analyst",
      caption: "weighs it up",
      detail:
        "Reads the whole debate, decides how likely each scenario is, and can send the committee back for another round if the argument has not converged. Up to three rounds.",
      analogy: "The judge — who can order a retrial.",
      kind: "ai",
      col: 5,
      row: 0,
      step: "lead",
    },
    {
      key: "memo",
      label: "Your memo",
      caption: "in plain English",
      detail:
        "A fair-value estimate with the scenarios behind it, every chart that matters, and a written memo you can actually read — plus the evidence trail behind each claim.",
      analogy: "The verdict, written out with the reasoning attached.",
      kind: "result",
      col: 6,
      row: 0,
      step: "memo",
    },
  ],
  edges: [
    { from: "completeness", to: "dq" },
    { from: "dq", to: "engine" },
    { from: "engine", to: "news" },
    { from: "engine", to: "ownership" },
    { from: "engine", to: "dqagent" },
    { from: "news", to: "advocate" },
    { from: "news", to: "challenger" },
    { from: "ownership", to: "challenger" },
    { from: "ownership", to: "auditor" },
    { from: "dqagent", to: "auditor" },
    { from: "advocate", to: "lead" },
    { from: "challenger", to: "lead" },
    { from: "auditor", to: "lead" },
    { from: "lead", to: "memo" },
    { from: "lead", to: "challenger", conditional: true, label: "another round" },
  ],
};

export const NODE_KIND_META: Record<NodeKind, { label: string; color: string }> = {
  data: { label: "Looks it up", color: "#6B86A8" },
  maths: { label: "Pure maths", color: "#2F4D73" },
  decision: { label: "Decision point", color: "#B45309" },
  ai: { label: "The AI thinks", color: "#1F7A52" },
  result: { label: "What you get", color: "#476D99" },
};
