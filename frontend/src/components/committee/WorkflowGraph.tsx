"use client";

// The animated map of what the backend is doing, in plain English.
//
// Two modes:
//   live      — `activeStep` comes from the pipeline stepper, so the highlight
//               tracks (an estimate of) the run in progress.
//   explainer — nothing running; the graph walks itself so the page keeps
//               moving and the copy teaches the pipeline.
//
// The topology is transcribed from the real backend graphs (lib/workflow.ts).
// Edge packets animate with a 6-unit dash on a pathLength=100 path, so a single
// keyframe fits every edge length.

import { useEffect, useMemo, useState } from "react";
import { NODE_KIND_META, type WorkflowNode, type WorkflowSpec } from "@/lib/workflow";

const COL_W = 146;
const ROW_H = 76;
const NODE_W = 118;
const NODE_H = 52;
const PAD_X = 16;
const PAD_Y = 20;

type Placed = WorkflowNode & { x: number; y: number; index: number };

function place(spec: WorkflowSpec): { nodes: Placed[]; width: number; height: number } {
  const rows = spec.nodes.map((n) => n.row);
  const minRow = Math.min(...rows);
  const maxRow = Math.max(...rows);
  const cols = Math.max(...spec.nodes.map((n) => n.col)) + 1;
  const nodes = spec.nodes.map((n, index) => ({
    ...n,
    index,
    x: PAD_X + n.col * COL_W,
    y: PAD_Y + (n.row - minRow) * ROW_H,
  }));
  return {
    nodes,
    width: PAD_X * 2 + (cols - 1) * COL_W + NODE_W,
    height: PAD_Y * 2 + (maxRow - minRow) * ROW_H + NODE_H,
  };
}

function edgePath(a: Placed, b: Placed): string {
  const x1 = a.x + NODE_W;
  const y1 = a.y + NODE_H / 2;
  const x2 = b.x;
  const y2 = b.y + NODE_H / 2;
  if (Math.abs(y1 - y2) < 1) return `M${x1},${y1} L${x2},${y2}`;
  const mid = (x1 + x2) / 2;
  return `M${x1},${y1} C${mid},${y1} ${mid},${y2} ${x2},${y2}`;
}

/** Backward edge (the lead analyst's extra round): loop under the row. */
function loopPath(a: Placed, b: Placed): string {
  const x1 = a.x + NODE_W / 2;
  const y1 = a.y + NODE_H;
  const x2 = b.x + NODE_W / 2;
  const y2 = b.y + NODE_H;
  const dip = Math.max(y1, y2) + 24;
  return `M${x1},${y1} C${x1},${dip} ${x2},${dip} ${x2},${y2}`;
}

export function WorkflowGraph({
  spec,
  activeStep,
  mode = "explainer",
  className = "",
}: {
  spec: WorkflowSpec;
  activeStep?: string;
  mode?: "live" | "explainer";
  className?: string;
}) {
  const { nodes, width, height } = useMemo(() => place(spec), [spec]);
  const byKey = useMemo(() => new Map(nodes.map((n) => [n.key, n])), [nodes]);

  // Explainer mode walks the graph on its own; hovering pins a node so you can
  // read at your own pace.
  const [cursor, setCursor] = useState(0);
  const [pinned, setPinned] = useState<string | null>(null);
  useEffect(() => {
    if (mode !== "explainer" || pinned) return;
    const t = setInterval(() => setCursor((c) => (c + 1) % nodes.length), 3200);
    return () => clearInterval(t);
  }, [mode, nodes.length, pinned]);

  const activeIndex = useMemo(() => {
    if (pinned) return nodes.findIndex((n) => n.key === pinned);
    if (mode === "explainer") return cursor;
    if (!activeStep) return -1;
    return nodes.findIndex((n) => n.step === activeStep);
  }, [pinned, mode, cursor, activeStep, nodes]);

  const activeKeys = useMemo(() => {
    if (pinned) return new Set([pinned]);
    if (mode === "explainer") return new Set(activeIndex >= 0 ? [nodes[activeIndex].key] : []);
    // Live: everything in the running step lights up together — the evidence
    // nodes and the tribunal really do run in parallel.
    return new Set(nodes.filter((n) => n.step === activeStep).map((n) => n.key));
  }, [pinned, mode, activeIndex, activeStep, nodes]);

  const doneKeys = useMemo(() => {
    const stop = activeIndex < 0 ? nodes.length : activeIndex;
    return new Set(nodes.slice(0, stop).filter((n) => !activeKeys.has(n.key)).map((n) => n.key));
  }, [activeIndex, nodes, activeKeys]);

  const focus = activeIndex >= 0 ? nodes[activeIndex] : null;

  return (
    <div className={className}>
      <div className="no-scrollbar overflow-x-auto">
        <svg
          viewBox={`0 0 ${width} ${height + 24}`}
          width={width}
          height={height + 24}
          className="max-w-full"
          role="img"
          aria-label={`${spec.title} — step by step`}
        >
          <defs>
            <marker id={`wf-arrow-${spec.id}`} markerWidth="6" markerHeight="6" refX="5.4" refY="3" orient="auto">
              <path d="M0,0 L6,3 L0,6 Z" fill="#6B86A8" />
            </marker>
          </defs>

          {spec.edges.map((e) => {
            const a = byKey.get(e.from);
            const b = byKey.get(e.to);
            if (!a || !b) return null;
            const backward = b.col <= a.col;
            const d = backward ? loopPath(a, b) : edgePath(a, b);
            const live = activeKeys.has(b.key) && (doneKeys.has(a.key) || activeKeys.has(a.key));
            return (
              <g key={`${e.from}->${e.to}`}>
                <path
                  d={d}
                  fill="none"
                  stroke={live ? "#2F4D73" : "#DDD8CD"}
                  strokeWidth={live ? 1.5 : 1}
                  strokeDasharray={e.conditional ? "3 3" : undefined}
                  markerEnd={backward ? undefined : `url(#wf-arrow-${spec.id})`}
                  opacity={e.conditional ? 0.7 : 1}
                />
                <path
                  className={live ? "mzqa-flow-live" : "mzqa-flow-idle"}
                  d={d}
                  pathLength={100}
                  fill="none"
                  stroke={live ? "#F59E0B" : "#6B86A8"}
                  strokeWidth={live ? 2.4 : 1.6}
                  strokeLinecap="round"
                  opacity={live ? 0.95 : 0.3}
                />
                {e.label ? (
                  <text
                    x={(a.x + NODE_W + b.x) / 2}
                    y={Math.max(a.y, b.y) + NODE_H + 15}
                    textAnchor="middle"
                    fontSize="8"
                    fill="#6F7890"
                  >
                    {e.label}
                  </text>
                ) : null}
              </g>
            );
          })}

          {nodes.map((n) => {
            const meta = NODE_KIND_META[n.kind];
            const isActive = activeKeys.has(n.key);
            const isDone = doneKeys.has(n.key);
            return (
              <g
                key={n.key}
                onMouseEnter={() => mode === "explainer" && setPinned(n.key)}
                onMouseLeave={() => mode === "explainer" && setPinned(null)}
                style={{ cursor: mode === "explainer" ? "help" : "default" }}
              >
                {isActive ? (
                  <rect
                    className="mzqa-node-halo"
                    x={n.x - 4}
                    y={n.y - 4}
                    width={NODE_W + 8}
                    height={NODE_H + 8}
                    rx="8"
                    fill={meta.color}
                  />
                ) : null}
                <rect
                  x={n.x}
                  y={n.y}
                  width={NODE_W}
                  height={NODE_H}
                  rx="6"
                  fill={isActive ? meta.color : isDone ? "#FBFAF7" : "#F5F4F0"}
                  stroke={isActive ? meta.color : isDone ? meta.color : "#DDD8CD"}
                  strokeWidth={isActive ? 1.4 : 1}
                  opacity={isActive || isDone ? 1 : 0.8}
                />
                <rect x={n.x} y={n.y} width="3" height={NODE_H} rx="1.5" fill={meta.color} opacity={isActive ? 1 : 0.5} />
                {/* Step number — turns the graph into a journey rather than a diagram. */}
                <text
                  x={n.x + 10}
                  y={n.y + 15}
                  fontSize="8"
                  fontWeight="700"
                  fill={isActive ? "#FBFAF7" : "#6B86A8"}
                  opacity={isActive ? 0.8 : 1}
                >
                  {String(n.index + 1).padStart(2, "0")}
                </text>
                <text
                  x={n.x + NODE_W / 2}
                  y={n.y + 27}
                  textAnchor="middle"
                  fontSize="9.5"
                  fontWeight={isActive ? 600 : 500}
                  fill={isActive ? "#FBFAF7" : "#2F4D73"}
                >
                  {n.label}
                </text>
                <text
                  x={n.x + NODE_W / 2}
                  y={n.y + 39}
                  textAnchor="middle"
                  fontSize="7.5"
                  fill={isActive ? "#FBFAF7" : "#6F7890"}
                  opacity={isActive ? 0.85 : 1}
                >
                  {n.caption}
                </text>
                {n.kind === "ai" ? (
                  <circle
                    cx={n.x + NODE_W - 9}
                    cy={n.y + 10}
                    r="2.8"
                    fill={isActive ? "#FBFAF7" : "#1F7A52"}
                    opacity={isActive ? 0.9 : 0.75}
                  />
                ) : null}
              </g>
            );
          })}
        </svg>
      </div>

      {/* What the highlighted step is doing. */}
      {focus ? (
        <div key={focus.key} className="fact-swap mt-2 rounded-md border border-border bg-panel px-3.5 py-2.5">
          <div className="flex flex-wrap items-center gap-2">
            <span
              className="rounded px-1.5 py-px text-[8.5px] font-semibold uppercase tracking-[0.1em] text-white"
              style={{ background: NODE_KIND_META[focus.kind].color }}
            >
              {NODE_KIND_META[focus.kind].label}
            </span>
            <span className="text-[12.5px] font-semibold text-navy">
              Step {focus.index + 1} · {focus.label}
            </span>
            {focus.takes ? (
              <span className="rounded-full border border-border px-1.5 text-[9px] text-muted">
                takes {focus.takes}
              </span>
            ) : null}
          </div>
          <p className="mt-1 text-[11.5px] leading-relaxed text-muted">{focus.detail}</p>
          {focus.analogy ? (
            <p className="mt-1 text-[11px] italic leading-relaxed text-navy-3">{focus.analogy}</p>
          ) : null}
        </div>
      ) : null}

      <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1">
        {Object.entries(NODE_KIND_META).map(([kind, meta]) => (
          <span key={kind} className="flex items-center gap-1 text-[9.5px] text-muted">
            <span className="inline-block h-2 w-2 rounded-sm" style={{ background: meta.color }} />
            {meta.label}
          </span>
        ))}
      </div>
    </div>
  );
}
