import React, { useMemo, useEffect, useRef, useCallback, useState } from "react";
import ReactFlow, {
  Background, Controls, MiniMap,
  useNodesState, useEdgesState,
  Handle, Position,
  useReactFlow,
  useStore as useRFStore,
  BackgroundVariant,
  MarkerType,
} from "reactflow";
import { useFlowStore } from "../store.js";
import { BRANCH_HUES, LANE_OVERVIEW_MIN, CARD_W, CARD_H, layoutOverview, useLanes } from "./laneLayout.js";

const fmt = new Intl.NumberFormat();
const prefersReducedMotion = () =>
  typeof window !== "undefined" && window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;

// ─────────────────────────────────────────────────────────────────────────────
// Semantic kind → visual config
// ─────────────────────────────────────────────────────────────────────────────
const SEMANTIC_KIND_CONFIG = {
  EXPOSES_API:    { color: "#10b981", label: "API",   icon: "🔌", edgeColor: "#10b981" },
  USES_DB:        { color: "#8b5cf6", label: "DB",    icon: "🗄️",  edgeColor: "#8b5cf6" },
  EMITS_EVENT:    { color: "#f59e0b", label: "Event", icon: "📤", edgeColor: "#f59e0b" },
  CONSUMES_EVENT: { color: "#ef4444", label: "Sub",   icon: "📥", edgeColor: "#ef4444" },
  CALLS:          { color: "#3b82f6", label: null,    icon: null,  edgeColor: "#2a3f5a" },
};

function getSemanticConfig(kind) {
  return SEMANTIC_KIND_CONFIG[kind] || SEMANTIC_KIND_CONFIG.CALLS;
}

// Above this many rendered nodes, skip fitView's animation (see the effect
// that calls it) — the stretch of continuous work reads as lag, not polish.
const FIT_VIEW_ANIMATE_THRESHOLD = 150;
const FLOW_TRACE_HOPS = 3;

// ─────────────────────────────────────────────────────────────────────────────
// Role badge — semantic_kind already distinguishes API/DB/Event/Sub for
// functions the backend has analyzed; this fills in the categories it doesn't
// cover (Service, Model, ML, Utility, Test, External) from the file path, so
// "what kind of code is this" reads before you read the label.
// ─────────────────────────────────────────────────────────────────────────────
const ROLE_FALLBACK_RULES = [
  { test: (p) => p.startsWith("external::") || p.includes("external::module::"), color: "#f87171", label: "External", icon: "📦" },
  { test: (p) => /(^|\/)(tests?|__tests__|spec)(\/|$)|(^|\/)test_|_test\.py$/i.test(p), color: "#facc15", label: "Test", icon: "🧪" },
  { test: (p) => /\b(models?|schemas?)\b/i.test(p), color: "#fb923c", label: "Model", icon: "🗄️" },
  { test: (p) => /\b(api|routes?|controllers?|views?|endpoints?)\b/i.test(p), color: "#38bdf8", label: "API", icon: "🔌" },
  { test: (p) => /\b(ml|llm|embeddings?|inference|train(er|ing)?)\b/i.test(p), color: "#c084fc", label: "ML", icon: "🧠" },
  { test: (p) => /\b(services?|handlers?)\b/i.test(p), color: "#34d399", label: "Service", icon: "⚙️" },
  { test: (p) => /\b(utils?|helpers?|lib|common|shared)\b/i.test(p), color: "#9ca3af", label: "Util", icon: "🔧" },
];

function resolveRole(semanticKind, filePath) {
  const semCfg = getSemanticConfig(semanticKind);
  if (semCfg.label) return semCfg; // EXPOSES_API / USES_DB / EMITS_EVENT / CONSUMES_EVENT already meaningful
  const p = (filePath || "").toLowerCase();
  for (const rule of ROLE_FALLBACK_RULES) {
    if (rule.test(p)) return rule;
  }
  return semCfg; // CALLS / no strong signal — no badge
}

// ─────────────────────────────────────────────────────────────────────────────
// Heatmap encoding — wires /graph_analytics per-node metrics onto the
// interactive canvas: size = fan-in + fan-out, glow = PageRank/centrality,
// border thickness = cyclomatic complexity. Ratios are normalized against the
// max seen across the WHOLE repo's metrics (not just currently-visible nodes)
// so "important" means globally important, matching what "god nodes" means
// elsewhere in the app (Graph Insights modal).
// ─────────────────────────────────────────────────────────────────────────────
const COMPLEXITY_BORDER_WIDTH = { low: 1, medium: 1.5, high: 2.5, very_high: 4 };

function complexityBorderWidth(complexity) {
  return COMPLEXITY_BORDER_WIDTH[complexity] || 1;
}

// Extra px added to a node's min/max width — sqrt-scaled so one outlier
// hub doesn't make every other node microscopic by comparison.
function fanSizeBoost(fanRatio) {
  if (!fanRatio) return 0;
  return Math.round(Math.sqrt(fanRatio) * 34);
}

// White "spotlight" glow (kept distinct from the amber search-highlight and
// the branch-hue palette) that only shows up for genuinely notable nodes.
function pagerankGlow(prRatio) {
  if (!prRatio || prRatio < 0.18) return undefined;
  const blur = 5 + prRatio * 16;
  const alpha = 0.2 + prRatio * 0.4;
  return `drop-shadow(0 0 ${blur.toFixed(0)}px rgba(255,255,255,${alpha.toFixed(2)}))`;
}

function metricsTooltip(metrics) {
  if (!metrics) return undefined;
  const parts = [];
  if (metrics.criticality) parts.push(`criticality: ${metrics.criticality}`);
  if (metrics.complexity) parts.push(`complexity: ${metrics.complexity}`);
  parts.push(`callers: ${metrics.in_degree}`, `calls out: ${metrics.out_degree}`);
  parts.push(`pagerank: ${metrics.pagerank}`);
  return parts.join(" · ");
}

// ─────────────────────────────────────────────────────────────────────────────
// Branch coloring — every root's expansion lineage ("branch") gets one hue,
// prominent and never blue/green (those blend into the dark backdrop). Depth
// within the branch fades the color toward a muted floor so you can read
// "how far from the root is this" at a glance, without it disappearing.
// Distinct from the pre-existing edge `kind === "FLOW"` (aggregated
// file/module call edges) — unrelated concept, unfortunate name proximity.
// ─────────────────────────────────────────────────────────────────────────────
function branchNodeColor(hue, depth) {
  const d = Math.min(depth, 6);
  const light = Math.max(66 - d * 6, 38);
  const sat = Math.max(88 - d * 6, 42);
  return `hsl(${hue} ${sat}% ${light}%)`;
}

function branchEdgeColor(hue, depth) {
  const d = Math.min(depth, 6);
  const light = Math.max(60 - d * 5, 34);
  const sat = Math.max(80 - d * 5, 38);
  return `hsl(${hue} ${sat}% ${light}%)`;
}

// ─────────────────────────────────────────────────────────────────────────────
// Icons (inline SVG, no external dep)
// ─────────────────────────────────────────────────────────────────────────────
const FileIcon = () => (
  <svg aria-hidden="true" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
    <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
    <polyline points="14,2 14,8 20,8"/>
  </svg>
);
const FnIcon = () => (
  <svg aria-hidden="true" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
    <polyline points="16,18 22,12 16,6"/><polyline points="8,6 2,12 8,18"/>
  </svg>
);
const ClassIcon = () => (
  <svg aria-hidden="true" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
    <rect x="2" y="3" width="20" height="14" rx="2"/><line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/>
  </svg>
);
const ChevronRight = () => (
  <svg aria-hidden="true" width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
    <polyline points="9,18 15,12 9,6"/>
  </svg>
);
const ChevronDown = () => (
  <svg aria-hidden="true" width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
    <polyline points="6,9 12,15 18,9"/>
  </svg>
);

// Left accent stripe — the always-visible "which branch, how deep" signal,
// independent of selection/highlight state.
function BranchStripe({ color, rounded }) {
  if (!color) return null;
  return (
    <div
      className={`absolute left-0 top-0 bottom-0 w-[5px] ${rounded}`}
      style={{ background: color }}
    />
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Custom node types
// ─────────────────────────────────────────────────────────────────────────────
const FileNodeComponent = React.memo(function FileNodeComponent({ id, data }) {
  const { label, filePath, fnCount, description, isExpanded, isHighlighted, isSelected, isUpstream, isDownstream, isRoot, isDimmed, toggleExpand, deepExpand, branchColor, metrics, fanRatio, prRatio } = data;
  // Below this zoom the label is barely legible anyway — drop everything
  // that costs paint (glow layer, description text) and show just the box
  // and its label, so zooming out on a big graph stays cheap.
  const zoom = useRFStore((s) => s.transform[2]);
  const lod = zoom < 0.5;
  // Don't show description if it's just the file path repeated
  const showDesc = !lod && description && description !== filePath && !description.startsWith("backend/") && !description.startsWith("frontend/") && !description.startsWith("tests/");
  const branchBorder = (!isHighlighted && !isSelected && !isUpstream && !isDownstream && branchColor) ? { borderColor: branchColor } : {};
  // Entry-point/root files are the "big picture" — give them visual weight
  // over files reached several hops away; heavily-connected files (summed
  // fan-in/out across their functions) get an extra boost on top.
  const boost = fanSizeBoost(fanRatio);
  const sizeStyle = isRoot
    ? { minWidth: 210 + boost, maxWidth: 320 + boost }
    : { minWidth: 170 + boost, maxWidth: 260 + boost };
  // Worst-offender complexity in the file → border thickness; summed
  // PageRank → the same white "notable" glow used on function nodes.
  const borderWidthStyle = { borderWidth: complexityBorderWidth(metrics?.complexity) };
  const glow = lod ? undefined : pagerankGlow(prRatio);

  return (
    <div
      title={metricsTooltip(metrics)}
      className={[
        "group relative rounded-xl border transition-[opacity,box-shadow,border-color,filter] duration-150 select-none",
        "bg-gradient-to-br from-blue-950/80 to-blue-900/40",
        isHighlighted
          ? "border-amber-400 shadow-[0_0_0_2px_rgba(251,191,36,0.25)] node-highlighted"
          : isSelected
          ? "border-blue-400 shadow-[0_0_16px_rgba(59,130,246,0.3)]"
          : isUpstream
          ? "border-sky-400 shadow-[0_0_14px_rgba(56,189,248,0.4)]"
          : isDownstream
          ? "border-emerald-400 shadow-[0_0_14px_rgba(16,185,129,0.4)]"
          : "hover:brightness-125 shadow-[0_2px_12px_rgba(0,0,0,0.4)]",
      ].join(" ")}
      style={{ ...sizeStyle, ...branchBorder, ...borderWidthStyle, opacity: isDimmed ? 0.32 : 1, filter: glow }}
    >
      <BranchStripe color={isHighlighted ? null : branchColor} rounded="rounded-l-xl" />
      <Handle type="target" position={Position.Left} className="!bg-blue-500 !border-blue-700 !w-2 !h-2" />

      <div className={isRoot ? "px-4 py-3.5" : "px-4 py-3"}>
        <div className="flex items-center gap-2 mb-1">
          <span className="text-blue-400 shrink-0"><FileIcon /></span>
          <span className="text-[10px] uppercase tracking-widest text-blue-400/70 font-semibold">File</span>
          {isUpstream && <span className="text-[9px] font-semibold text-sky-400 bg-sky-950/80 px-1.5 py-0.5 rounded border border-sky-700/50">Caller</span>}
          {isDownstream && <span className="text-[9px] font-semibold text-emerald-400 bg-emerald-950/80 px-1.5 py-0.5 rounded border border-emerald-700/50">Callee</span>}
          <button
            onClick={(e) => { e.stopPropagation(); (e.shiftKey ? deepExpand : toggleExpand)(id); }}
            title={isExpanded ? "Collapse" : "Expand to see functions (Shift-click or Shift+Enter: expand several levels)"}
            aria-label={`${isExpanded ? "Collapse" : "Expand"} ${label}`}
            aria-expanded={!!isExpanded}
            className="ml-auto shrink-0 -m-1 p-1 rounded text-blue-400/60 hover:text-blue-200 hover:bg-blue-800/40 transition-colors"
          >
            {isExpanded ? <ChevronDown /> : <ChevronRight />}
          </button>
        </div>
        <div className={`${isRoot ? "text-base" : "text-sm"} font-semibold text-white leading-snug truncate`}>{label}</div>
        {filePath && (
          <div className="text-[10px] text-blue-300/50 mt-0.5 truncate">{filePath}</div>
        )}
        {showDesc && (
          <div className="text-[10px] text-blue-200/60 mt-1.5 italic leading-snug line-clamp-2">{description}</div>
        )}
        {fnCount > 0 && (
          <div className="mt-2 inline-flex items-center gap-1 text-[10px] text-blue-300/60 bg-blue-900/30 rounded-full px-2 py-0.5">
            <FnIcon />{fnCount} symbols
          </div>
        )}
      </div>

      <Handle type="source" position={Position.Right} className="!bg-blue-500 !border-blue-700 !w-2 !h-2" />
    </div>
  );
});

const FunctionNodeComponent = React.memo(function FunctionNodeComponent({ id, data }) {
  const { label, kind, filePath, description, isHighlighted, isSelected, isUpstream, isDownstream, isExpanded, hasCallees, semanticKind, isDimmed, toggleExpand, deepExpand, importance, importanceLevel, hiddenCallees, showAllCallees, branchColor, metrics, fanRatio, prRatio } = data;
  const zoom = useRFStore((s) => s.transform[2]);
  const lod = zoom < 0.5;

  const isClass = kind === "class";
  const role = resolveRole(semanticKind || "CALLS", filePath);
  const accent = isClass
    ? { border: "border-emerald-700/60", hoverBorder: "hover:border-emerald-500/80", text: "text-emerald-400", bg: "from-emerald-950/80 to-emerald-900/30" }
    : { border: "border-indigo-800/60", hoverBorder: "hover:border-indigo-500/80", text: "text-indigo-400", bg: "from-indigo-950/80 to-indigo-900/30" };

  // Branch identity takes over the border color in the default state — the
  // role (API/DB/Service/...) stays visible via its own badge below.
  const branchBorder = (!isHighlighted && !isSelected && !isUpstream && !isDownstream && branchColor) ? { borderColor: branchColor } : {};
  // Show description only if it's a real one (not a file path, not a stub)
  const showDesc = !lod && description && !description.startsWith("(stub)") && description.length > 4;
  // Classes read as a bigger architectural unit than a plain helper function;
  // heavily-called/calling nodes (fan-in + fan-out) get an extra size boost.
  const boost = fanSizeBoost(fanRatio);
  const sizeStyle = isClass
    ? { minWidth: 180 + boost, maxWidth: 280 + boost }
    : { minWidth: 145 + boost, maxWidth: 225 + boost };
  // Cyclomatic complexity → border thickness; PageRank/centrality → a white
  // spotlight glow, distinct from the amber search-highlight and branch hues.
  const borderWidthStyle = { borderWidth: complexityBorderWidth(metrics?.complexity) };
  const glow = lod ? undefined : pagerankGlow(prRatio);

  return (
    <div
      title={metricsTooltip(metrics)}
      className={[
        "relative rounded-lg border transition-[opacity,box-shadow,border-color,filter] duration-150 select-none",
        `bg-gradient-to-br ${accent.bg}`,
        isHighlighted
          ? "border-amber-400 shadow-[0_0_0_2px_rgba(251,191,36,0.2)] node-highlighted"
          : isSelected
          ? `border-opacity-100 ${accent.border.replace('/60','')}`
          : isUpstream
          ? "border-sky-400 shadow-[0_0_12px_rgba(56,189,248,0.4)]"
          : isDownstream
          ? "border-emerald-400 shadow-[0_0_12px_rgba(16,185,129,0.4)]"
          : `${accent.border} hover:brightness-125 shadow-[0_1px_8px_rgba(0,0,0,0.3)]`,
      ].join(" ")}
      style={{ ...sizeStyle, ...branchBorder, ...borderWidthStyle, opacity: isDimmed ? 0.32 : 1, filter: glow }}
    >
      <BranchStripe color={isHighlighted ? null : branchColor} rounded="rounded-l-lg" />
      <Handle type="target" position={Position.Left} className="!bg-indigo-500 !border-indigo-700 !w-1.5 !h-1.5" />

      <div className="px-3 py-2.5">
        <div className="flex items-center gap-1.5 mb-0.5">
          <span className={`${accent.text} shrink-0`}>{isClass ? <ClassIcon /> : <FnIcon />}</span>
          <span className={`text-[9px] uppercase tracking-wider font-semibold ${accent.text} opacity-70`}>
            {kind || "fn"}
          </span>
          {role.label && (
            <span
              className="ml-1 text-[8px] font-bold px-1 rounded"
              style={{ background: `${role.color}22`, color: role.color }}
            >
              <span aria-hidden="true">{role.icon} </span>{role.label}
            </span>
          )}
          {importanceLevel && (
            <span
              className="ml-1 flex items-center gap-[2px] shrink-0"
              title={`Importance ${fmt.format(Number(importance.toFixed(2)))} (${importanceLevel}) — PageRank, fan-in and criticality`}
            >
              <span className="sr-only">{`Importance: ${importanceLevel}`}</span>
              {[0, 1, 2].map((i) => (
                <span
                  key={i}
                  aria-hidden="true"
                  className="w-1 h-1 rounded-full"
                  style={{ background: i <= ({ low: 0, medium: 1, high: 2 }[importanceLevel] ?? 0) ? "#fbbf24" : "#334155" }}
                />
              ))}
            </span>
          )}
          {isUpstream && <span className="text-[8px] font-bold text-sky-400 bg-sky-950 px-1 rounded border border-sky-800">Caller</span>}
          {isDownstream && <span className="text-[8px] font-bold text-emerald-400 bg-emerald-950 px-1 rounded border border-emerald-800">Callee</span>}
          {hasCallees && (
            <button
              onClick={(e) => { e.stopPropagation(); (e.shiftKey ? deepExpand : toggleExpand)(id); }}
              title={isExpanded ? "Collapse" : "Expand to see what this calls (Shift-click or Shift+Enter: expand several levels)"}
              aria-label={`${isExpanded ? "Collapse" : "Expand"} ${label}`}
              aria-expanded={!!isExpanded}
              className="ml-auto shrink-0 -m-1 p-1 rounded text-slate-500 hover:text-slate-200 hover:bg-slate-700/40 transition-colors"
            >
              {isExpanded ? <ChevronDown /> : <ChevronRight />}
            </button>
          )}
        </div>
        <div className="font-mono text-xs text-white/90 truncate leading-snug">{label}</div>
        {showDesc && (
          <div className="text-[10px] text-slate-400/80 mt-1 italic leading-snug line-clamp-2">{description}</div>
        )}
        {isExpanded && hiddenCallees > 0 && (
          <button
            onClick={(e) => { e.stopPropagation(); showAllCallees(id); }}
            title="Showing the most important callees — click to add the rest"
            className="mt-1.5 text-[9px] font-semibold text-amber-300/90 hover:text-amber-200 bg-amber-950/50 border border-amber-800/50 rounded px-1.5 py-0.5"
          >
            +{hiddenCallees} less important
          </button>
        )}
      </div>

      <Handle type="source" position={Position.Right} className="!bg-indigo-500 !border-indigo-700 !w-1.5 !h-1.5" />
    </div>
  );
});

// Translucent tinted band behind a branch's lane — a cheap stand-in for full
// cluster containers: the eye groups a region before it reads individual
// nodes. Non-interactive (no drag/select/pointer events) and rendered behind
// real nodes.
const LaneBackgroundComponent = React.memo(function LaneBackgroundComponent({ data }) {
  const { hue } = data;
  return (
    <div
      style={{
        width: data.width,
        height: data.height,
        background: `linear-gradient(135deg, hsl(${hue} 70% 55% / 0.09), hsl(${hue} 70% 55% / 0.02))`,
        border: `1px solid hsl(${hue} 70% 55% / 0.16)`,
        borderRadius: 20,
        pointerEvents: "none",
      }}
    />
  );
});

// One whole lane collapsed to a card: what it is, how big, and how it connects
// to the other lanes. The overview is made of these; in focus mode they stand
// in for the neighbouring lanes. The whole card is one real button.
const LaneCardComponent = React.memo(function LaneCardComponent({ id, data }) {
  const { label, path, kind, unit, count, calls, calledBy, hue, isHighlighted, relation, onFocus } = data;
  const stripe = branchNodeColor(hue, 0);
  return (
    <div className="relative">
      <Handle type="target" position={Position.Left} className="!bg-blue-500 !border-blue-700 !w-2 !h-2" />
      <button
        type="button"
        onClick={(e) => { e.stopPropagation(); onFocus(id); }}
        aria-label={`Focus ${label}: ${count} ${unit}, calls into ${calls} places, called from ${calledBy}`}
        style={{ width: CARD_W, height: CARD_H }}
        className={[
          "relative block overflow-hidden text-left rounded-xl border pl-4 pr-3 py-2.5",
          "bg-gradient-to-br from-blue-950/80 to-blue-900/40 shadow-[0_2px_12px_rgba(0,0,0,0.4)]",
          "transition-[box-shadow,border-color,filter] duration-150 hover:brightness-125",
          isHighlighted ? "border-amber-400 node-highlighted" : "border-[#1e2d4a] hover:border-blue-500/70",
        ].join(" ")}
      >
        <BranchStripe color={stripe} rounded="rounded-l-xl" />
        <div className="flex items-center gap-2">
          <span className="text-[10px] uppercase tracking-widest text-blue-400/70 font-semibold">{kind}</span>
          {relation && (
            <span className={`text-[10px] font-semibold px-1.5 rounded border ${
              relation === "Caller"
                ? "text-sky-400 bg-sky-950/80 border-sky-700/50"
                : "text-emerald-400 bg-emerald-950/80 border-emerald-700/50"
            }`}>{relation}</span>
          )}
        </div>
        <div translate="no" className="mt-0.5 text-sm font-semibold text-white truncate">{label}</div>
        <div translate="no" className="text-[11px] text-blue-300/50 truncate">{path || "\u00a0"}</div>
        <div className="mt-1.5 flex items-center gap-3 text-[11px] text-slate-300 tabular-nums">
          <span>{fmt.format(count)} {unit}</span>
          <span title="Calls into other lanes">→ {fmt.format(calls)}</span>
          <span title="Called from other lanes">← {fmt.format(calledBy)}</span>
        </div>
      </button>
      <Handle type="source" position={Position.Right} className="!bg-blue-500 !border-blue-700 !w-2 !h-2" />
    </div>
  );
});

const nodeTypes = {
  fileNode:     FileNodeComponent,
  functionNode: FunctionNodeComponent,
  laneBg:       LaneBackgroundComponent,
  laneCard:     LaneCardComponent,
};

function laneCardData(lane, onFocus) {
  const { label, path, kind, unit, count, calls, calledBy, hue } = lane;
  return { label, path, kind, unit, count, calls, calledBy, hue, onFocus };
}

// A weighted edge between two lane cards (or a function and a neighbour card).
function laneEdge(source, target, count, hue) {
  const width = Math.min(1 + Math.log2(count) * 0.5, 3);
  const stroke = branchEdgeColor(hue ?? BRANCH_HUES[0], 0);
  return {
    id: `lane:${source}>${target}`,
    source,
    target,
    type: "smoothstep",
    label: count > 1 ? fmt.format(count) : undefined,
    labelStyle: { fill: "#cbd5e1", fontSize: 11, fontWeight: 600 },
    labelBgStyle: { fill: "#07090f", fillOpacity: 0.85 },
    markerEnd: { type: MarkerType.ArrowClosed, width: 14, height: 14, color: stroke },
    style: { stroke, strokeWidth: width, opacity: 0.4 },
    data: { restWidth: width, restOpacity: 0.4, baseWidth: width },
  };
}

// Most neighbouring lanes shown per side in focus mode; the rest are counted.
const NEIGHBOR_CAP = 8;
const NEIGHBOR_GAP = 130;

// ─────────────────────────────────────────────────────────────────────────────
// Compact layered grid layout — one independent pass per branch.
//
// The previous approach (dagre, one column per rank) grew a cluster's
// bounding box LINEARLY with its widest level — a branch with 30 siblings at
// one depth became a single 30-tall column, ballooning the whole cluster.
// Levels come for free from `depth` (hops from the branch root via the
// parent chain used for expansion — already computed for the branch
// color/fade), so instead of stacking each level in one column, its nodes
// are packed into a roughly SQUARE grid (cols ≈ √count). That makes a
// cluster's footprint grow with the square root of its node count instead
// of linearly, which is what actually keeps large clusters compact.
// Trade-off: this optimizes for compactness, not edge-crossing minimization
// (which dagre did) — acceptable since the primary complaint is size, and
// cross-level/cross-sibling edges are a minority of what's on screen.
// ─────────────────────────────────────────────────────────────────────────────
const GRID_GAP_X = 26;
const GRID_GAP_Y = 20;
const LEVEL_GAP = 70;

function compactLevelLayout(rawNodes, branchInfo) {
  const byLevel = new Map();
  rawNodes.forEach((n) => {
    const depth = branchInfo[n.id]?.depth ?? 0;
    if (!byLevel.has(depth)) byLevel.set(depth, []);
    byLevel.get(depth).push(n);
  });

  const levels = [...byLevel.keys()].sort((a, b) => a - b);
  let xCursor = 0;
  let maxY = 0;
  const placed = [];

  for (const level of levels) {
    const group = byLevel.get(level);
    const isFileLevel = group.some((n) => n.kind === "file" || n.kind === "module");
    // Cards render wider/taller than their base size — root files and
    // heavily-connected nodes get a fan-based size boost (up to +34px, see
    // fanSizeBoost), and description text can wrap to a second line. Reserve
    // the worst case per slot so two cards can never collide even when both
    // render at their largest; this trades a bit of extra whitespace for a
    // layout that's never wrong.
    const cellW = isFileLevel ? 360 : 320;
    const cellH = isFileLevel ? 140 : 110;
    const cols = Math.max(1, Math.round(Math.sqrt(group.length)));

    group.forEach((n, i) => {
      const col = i % cols;
      const row = Math.floor(i / cols);
      const x = xCursor + col * (cellW + GRID_GAP_X);
      const y = row * (cellH + GRID_GAP_Y);
      placed.push({ ...n, position: { x, y } });
      maxY = Math.max(maxY, y + cellH);
    });

    xCursor += cols * (cellW + GRID_GAP_X) - GRID_GAP_X + LEVEL_GAP;
  }

  return { placed, width: Math.max(0, xCursor - LEVEL_GAP), height: maxY };
}

// Lay out each branch independently, then stack the lanes with a generous
// gutter so unrelated branches never crowd each other's space — closely
// coupled nodes still cluster tightly via their own branch's grid, while
// edges crossing between branches (rendered separately, see rfEdges) become
// long connectors instead of distorting either branch's layout.
const LANE_GAP = 90;
const LANE_PAD = 30;

function layoutBranches(rawNodes, branchInfo, orderedRoots) {
  const byRoot = new Map(orderedRoots.map((r) => [r, []]));
  rawNodes.forEach((n) => {
    const root = branchInfo[n.id]?.root ?? n.id;
    if (!byRoot.has(root)) byRoot.set(root, []);
    byRoot.get(root).push(n);
  });

  let yCursor = 0;
  const out = [];
  const lanes = [];
  for (const [root, groupNodes] of byRoot) {
    if (!groupNodes.length) continue;
    const { placed, width, height } = compactLevelLayout(groupNodes, branchInfo);
    placed.forEach((n) => {
      out.push({ ...n, position: { x: n.position.x, y: n.position.y + yCursor } });
    });
    lanes.push({
      root,
      hue: branchInfo[root]?.hue ?? BRANCH_HUES[0],
      x: -LANE_PAD, y: yCursor - LANE_PAD,
      width: width + LANE_PAD * 2, height: height + LANE_PAD * 2,
    });
    yCursor += height + LANE_GAP;
  }
  return { positioned: out, lanes };
}

// ─────────────────────────────────────────────────────────────────────────────
// Empty state overlay
// ─────────────────────────────────────────────────────────────────────────────
function EmptyState() {
  return (
    <div className="absolute inset-0 flex flex-col items-center justify-center pointer-events-none select-none">
      <div className="text-center fade-in">
        <div className="w-20 h-20 mx-auto mb-6 rounded-2xl bg-blue-950/60 border border-blue-800/40 flex items-center justify-center">
          <svg aria-hidden="true" width="36" height="36" viewBox="0 0 24 24" fill="none" stroke="#3b82f6" strokeWidth="1.5">
            <circle cx="11" cy="11" r="8"/><circle cx="11" cy="11" r="3"/>
            <line x1="21" y1="21" x2="16.65" y2="16.65"/>
          </svg>
        </div>
        <h2 className="text-xl font-semibold text-white/80 mb-2">No Graph Loaded</h2>
        <div className="text-sm text-slate-500 max-w-xs leading-relaxed">
          Enter a repository path in the sidebar and click <strong className="text-slate-400">Ingest</strong> to explore its code graph.
        </div>
        <div className="mt-6 flex items-center gap-6 text-xs text-slate-600">
          <span>Click a node to inspect it</span>
          <span className="w-1 h-1 rounded-full bg-slate-700" />
          <span>Click ▸ to expand</span>
          <span className="w-1 h-1 rounded-full bg-slate-700" />
          <span>Ask questions below</span>
        </div>
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Main component
// ─────────────────────────────────────────────────────────────────────────────
function GraphViewInner() {
  const visibleNodes   = useFlowStore((s) => s.nodes);
  const visibleEdges   = useFlowStore((s) => s.edges);
  const expanded       = useFlowStore((s) => s.expanded);
  const rootIds        = useFlowStore((s) => s.rootIds);
  const selectedId     = useFlowStore((s) => s.selectedId);
  const highlight      = useFlowStore((s) => s.highlightPath);
  const toggleExpand   = useFlowStore((s) => s.toggleExpand);
  const deepExpand     = useFlowStore((s) => s.deepExpand);
  const showAllCallees = useFlowStore((s) => s.showAllCallees);
  const selectNode     = useFlowStore((s) => s.selectNode);
  const clearSelection = useFlowStore((s) => s.clearSelection);
  const nodeMetrics    = useFlowStore((s) => s.nodeMetrics);
  const fileMetrics    = useFlowStore((s) => s.fileMetrics);
  const nodeBudget     = useFlowStore((s) => s.nodeBudget);
  const focusLaneId    = useFlowStore((s) => s.focusLaneId);
  const focusLane      = useFlowStore((s) => s.focusLane);
  const vw             = useRFStore((s) => s.width);
  const vh             = useRFStore((s) => s.height);

  const [nodes, setNodes, onNodesChange] = useNodesState([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState([]);
  const { fitView } = useReactFlow();
  const prevKeyRef = useRef("");
  // Hover-focus: dim everything except the hovered node and its direct
  // neighbors, and brighten the edges that connect them. Purely a rendering
  // concern — not persisted in the store.
  const [hoveredId, setHoveredId] = useState(null);

  const childrenOf = useMemo(() => {
    const map = {};
    const ids = new Set(Object.keys(visibleNodes));
    Object.values(visibleNodes).forEach((n) => {
      if (n.parent && ids.has(n.parent)) (map[n.parent] ||= []).push(n.id);
    });
    return map;
  }, [visibleNodes]);

  // Normalize heatmap metrics against the max seen across the WHOLE repo
  // (not just visible nodes), so "important" tracks global significance.
  const metricsStats = useMemo(() => {
    let maxFan = 0, maxPr = 0;
    Object.values(nodeMetrics).forEach((m) => {
      maxFan = Math.max(maxFan, (m.in_degree || 0) + (m.out_degree || 0));
      maxPr = Math.max(maxPr, m.pagerank || 0);
    });
    return { maxFan, maxPr };
  }, [nodeMetrics]);

  // File-level pagerank/fan are sums over each file's functions, so they live
  // on a different scale than function-level values — normalize separately
  // rather than mixing them into one scale where files would always win.
  const fileMetricsStats = useMemo(() => {
    let maxFan = 0, maxPr = 0;
    Object.values(fileMetrics).forEach((m) => {
      maxFan = Math.max(maxFan, (m.in_degree || 0) + (m.out_degree || 0));
      maxPr = Math.max(maxPr, m.pagerank || 0);
    });
    return { maxFan, maxPr };
  }, [fileMetrics]);

  // Which branch (root lineage) each node belongs to, its depth within that
  // branch, and the branch's assigned hue.
  const { branchInfo, orderedRoots, lanes, laneOf, laneEdges, members } = useLanes();

  // classic  — few roots: every lane stacked, as before.
  // overview — many roots: one card per lane, laid out left→right by call flow.
  // focus    — one lane in full, its neighbouring lanes as cards on either side.
  const overviewCapable = rootIds.length >= LANE_OVERVIEW_MIN;
  const focusValid = overviewCapable && !!focusLaneId && (members.get(focusLaneId)?.length ?? 0) > 0;
  const mode = !overviewCapable ? "classic" : focusValid ? "focus" : "overview";
  const focusSet = useMemo(
    () => (mode === "focus" ? new Set(members.get(focusLaneId)) : null),
    [mode, members, focusLaneId],
  );
  const laneByRoot = useMemo(() => new Map(lanes.map((l) => [l.id, l])), [lanes]);

  // Selecting a node traces its callers (upstream) and callees (downstream)
  // a few hops out and dims the rest, so one path reads without zooming.
  const selectionFlow = useMemo(() => {
    const none = { activeNodes: null, upstreamNodes: new Set(), downstreamNodes: new Set(), upstreamEdges: new Set(), downstreamEdges: new Set() };
    if (!selectedId || !visibleNodes[selectedId]) return none;

    const incoming = {}, outgoing = {};
    Object.values(visibleEdges).forEach((e) => {
      if (e.kind === "CONTAINS") return;
      (incoming[e.target] ||= []).push(e);
      (outgoing[e.source] ||= []).push(e);
    });

    const trace = (adj, next) => {
      const nodes = new Set(), edgeIds = new Set(), seen = new Set([selectedId]);
      let frontier = [selectedId];
      for (let hop = 0; hop < FLOW_TRACE_HOPS && frontier.length; hop++) {
        const nextFrontier = [];
        for (const id of frontier) {
          for (const e of adj[id] || []) {
            edgeIds.add(e.id);
            const other = next(e);
            if (!seen.has(other)) { seen.add(other); nodes.add(other); nextFrontier.push(other); }
          }
        }
        frontier = nextFrontier;
      }
      return { nodes, edgeIds };
    };

    const up = trace(incoming, (e) => e.source);
    const down = trace(outgoing, (e) => e.target);
    return {
      activeNodes: new Set([selectedId, ...up.nodes, ...down.nodes]),
      upstreamNodes: up.nodes,
      downstreamNodes: down.nodes,
      upstreamEdges: up.edgeIds,
      downstreamEdges: down.edgeIds,
    };
  }, [selectedId, visibleNodes, visibleEdges]);

  // Build RF edges (with CONTAINS fan-out pruning + branch/semantic coloring)
  const rfEdges = useMemo(() => {
    const hl = new Set(highlight);
    const { upstreamEdges, downstreamEdges } = selectionFlow;
    if (mode === "overview") return [];
    return Object.values(visibleEdges).map((e) => {
      // Focus mode draws only the focused lane here; edges crossing into a
      // neighbouring lane are drawn as card edges (see focusExtras).
      if (focusSet && !(focusSet.has(e.source) && focusSet.has(e.target))) return null;
      const isContains = e.kind === "CONTAINS";
      const isModuleFlow = e.kind === "FLOW"; // aggregated module/file-level call edge
      const isHighlightedEdge = hl.has(e.source) && hl.has(e.target);

      if (isContains) {
        const siblings = childrenOf[e.source] || [];
        if (siblings.length > 3) {
          const first = siblings[0], last = siblings[siblings.length - 1];
          if (e.target !== first && e.target !== last) return null;
        }
      }

      const targetNode = visibleNodes[e.target];
      const targetSemanticKind = targetNode?.adapter_metadata?.semantic_kind || "CALLS";
      const semCfg = getSemanticConfig(targetSemanticKind);
      const hasSemanticLabel = !isContains && !!semCfg.label;

      const srcBranch = branchInfo[e.source];
      const tgtBranch = branchInfo[e.target];
      const sameBranch = srcBranch && tgtBranch && srcBranch.root === tgtBranch.root;

      // Resolve the edge's color the same way regardless of hover — hover
      // only changes how PROMINENT it is, not what it means.
      let stroke, dash, baseWidth;
      if (isContains) {
        stroke = "#1e3a5a"; dash = "5 4"; baseWidth = 1;
      } else if (hasSemanticLabel) {
        stroke = semCfg.edgeColor; dash = undefined; baseWidth = 1.5;
      } else if (sameBranch) {
        stroke = branchEdgeColor(srcBranch.hue, Math.max(srcBranch.depth, tgtBranch.depth));
        dash = undefined;
        baseWidth = isModuleFlow ? 1.5 : 1.2;
      } else {
        // Cross-branch interaction: a de-emphasized long connector rather
        // than clutter competing with either branch's identity.
        stroke = "#5b6a85"; dash = "2 5"; baseWidth = 1;
      }

      // The graph should emphasize nodes, not edges, by default — edges stay
      // faint until something calls them out (search path or hover). Hover
      // itself is intentionally NOT a dependency here — it's applied by a
      // separate targeted effect below so a mouse-move doesn't rebuild every
      // edge in the graph, only the handful touching the hovered node.
      let width, opacity;
      if (isHighlightedEdge) {
        stroke = "#f59e0b"; dash = undefined; width = 2.5; opacity = 0.95;
      } else if (upstreamEdges.has(e.id)) {
        stroke = "#38bdf8"; dash = "6 4"; width = 2; opacity = 0.95;
      } else if (downstreamEdges.has(e.id)) {
        stroke = "#10b981"; dash = "6 4"; width = 2; opacity = 0.95;
      } else {
        width = 1; opacity = isContains ? 0.28 : 0.2;
        if (selectedId) opacity = 0.06;
      }
      const isFlowEdge = upstreamEdges.has(e.id) || downstreamEdges.has(e.id);

      return {
        id: e.id,
        source: e.source,
        target: e.target,
        animated: isHighlightedEdge || isFlowEdge,
        type: "smoothstep",
        label: (hasSemanticLabel && !isHighlightedEdge) ? semCfg.label : undefined,
        labelStyle: { fill: semCfg.color, fontSize: 8, fontWeight: 600 },
        labelBgStyle: { fill: "#07090f", fillOpacity: 0.8 },
        markerEnd: isContains ? undefined : { type: MarkerType.ArrowClosed, width: 14, height: 14, color: stroke },
        style: { stroke, strokeDasharray: dash, strokeWidth: width, opacity },
        data: { isContains, isHighlightedEdge, baseWidth },
      };
    }).filter(Boolean);
  }, [mode, focusSet, visibleEdges, visibleNodes, childrenOf, highlight, branchInfo, selectionFlow, selectedId]);

  // Each branch gets its own compact grid pass, then lanes stack without overlap.
  const { positioned, lanes: laneBoxes } = useMemo(() => {
    if (mode === "overview") return { positioned: [], lanes: [] };
    if (mode === "focus") {
      return layoutBranches([...focusSet].map((id) => visibleNodes[id]), branchInfo, [focusLaneId]);
    }
    return layoutBranches(Object.values(visibleNodes), branchInfo, orderedRoots);
  }, [mode, focusSet, focusLaneId, visibleNodes, branchInfo, orderedRoots]);

  const rootIdSet = useMemo(() => new Set(rootIds), [rootIds]);

  // Lane background nodes render first so real nodes stack on top of them.
  const laneNodes = useMemo(() => laneBoxes.map((lane) => ({
    id: `lanebg::${lane.root}`,
    type: "laneBg",
    position: { x: lane.x, y: lane.y },
    draggable: false,
    selectable: false,
    style: { pointerEvents: "none" },
    data: { width: lane.width, height: lane.height, hue: lane.hue },
  })), [laneBoxes]);

  // Build RF nodes with custom types
  const rfNodes = useMemo(() => {
    const hl = new Set(highlight);
    const { activeNodes, upstreamNodes, downstreamNodes } = selectionFlow;
    const functionNodes = positioned.map((n) => {
      const isFile      = n.kind === "file" || n.kind === "module";
      const isSelected  = n.id === selectedId;
      const isHighlighted = hl.has(n.id);
      const isExpanded  = !!expanded[n.id];
      const branch = branchInfo[n.id];
      // Hover dimming is applied by a separate targeted effect below, not
      // here — a mouse-move must not reallocate every node's `data`. Selection
      // dimming is rebuilt here because it only changes on click.
      const isDimmed = activeNodes ? !activeNodes.has(n.id) : false;
      // Function nodes look up their own metrics; file nodes get the same
      // signal aggregated across their functions (see fileMetrics), each
      // normalized against its own scale (file sums vs. function values).
      const metrics = isFile ? fileMetrics[n.file_path] : nodeMetrics[n.id];
      const stats = isFile ? fileMetricsStats : metricsStats;
      const fan = metrics ? (metrics.in_degree || 0) + (metrics.out_degree || 0) : 0;

      return {
        id: n.id,
        type: isFile ? "fileNode" : "functionNode",
        position: n.position,
        data: {
          label:        n.label,
          filePath:     isFile ? n.file_path || n.description : (n.file_path || null),
          description:  isFile ? null : (n.description || null),
          fnCount:      n.function_count ?? 0,
          kind:         n.type || n.kind,
          semanticKind: n.adapter_metadata?.semantic_kind || "CALLS",
          isExpanded,
          isHighlighted,
          isSelected,
          importance: n.importance,
          importanceLevel: n.importance_level,
          hiddenCallees: n.hiddenCallees || 0,
          showAllCallees,
          isUpstream: upstreamNodes.has(n.id),
          isDownstream: downstreamNodes.has(n.id),
          isDimmed,
          isRoot: isFile && rootIdSet.has(n.id),
          // Always offer the expand chevron for function/class nodes — we
          // can't know client-side whether a not-yet-expanded node has
          // callees without asking the backend, so show it optimistically
          // (same as file nodes). If it truly has none, expanding is a no-op.
          hasCallees:  true,
          // Stable store-action references (zustand functions never change
          // identity across renders) rather than a fresh closure allocated
          // per node per rebuild — the components call these with `id`,
          // which they get as ReactFlow's own node prop. Keeping `data`
          // free of anything that reallocates on every render is what lets
          // React.memo on the node components actually skip re-renders.
          toggleExpand,
          deepExpand,
          branchColor: branch ? branchNodeColor(branch.hue, branch.depth) : null,
          // Heatmap: size = fan-in/out, glow = PageRank, border = complexity.
          metrics,
          fanRatio: stats.maxFan ? fan / stats.maxFan : 0,
          prRatio: metrics && stats.maxPr ? metrics.pagerank / stats.maxPr : 0,
        },
      };
    });
    return [...laneNodes, ...functionNodes];
  }, [positioned, highlight, expanded, selectedId, selectionFlow, childrenOf, toggleExpand, deepExpand, showAllCallees, branchInfo, rootIdSet, laneNodes, nodeMetrics, metricsStats, fileMetrics, fileMetricsStats]);

  const overviewGraph = useMemo(() => {
    if (mode !== "overview") return { nodes: [], edges: [] };
    const pos = layoutOverview(lanes, laneEdges, { w: vw || 1000, h: vh || 700 });
    const highlightedLanes = new Set(highlight.map((id) => laneOf[id]).filter(Boolean));
    return {
      nodes: lanes.map((l) => ({
        id: l.id,
        type: "laneCard",
        position: pos.get(l.id) ?? { x: 0, y: 0 },
        draggable: false,
        data: { ...laneCardData(l, focusLane), isHighlighted: highlightedLanes.has(l.id) },
      })),
      edges: laneEdges.map(({ source, target, count }) => laneEdge(source, target, count, laneByRoot.get(source)?.hue)),
    };
  }, [mode, lanes, laneEdges, laneOf, laneByRoot, highlight, vw, vh, focusLane]);

  // Focus mode: the lanes the focused one calls / is called by, as cards to
  // its right / left, plus one aggregated edge per function↔card connection.
  const focusExtras = useMemo(() => {
    const empty = { nodes: [], edges: [], hiddenCallers: 0, hiddenCallees: 0 };
    if (mode !== "focus" || !laneBoxes[0]) return empty;
    const rel = new Map();
    laneEdges.forEach(({ source, target, count }) => {
      const other = source === focusLaneId ? target : target === focusLaneId ? source : null;
      if (!other) return;
      const r = rel.get(other) || { callers: 0, callees: 0 };
      if (source === focusLaneId) r.callees += count; else r.callers += count;
      rel.set(other, r);
    });
    const left = [], right = [];
    rel.forEach((r, id) => (r.callees >= r.callers ? right : left).push({ id, weight: r.callers + r.callees }));
    const byWeight = (a, b) => b.weight - a.weight;
    left.sort(byWeight); right.sort(byWeight);
    const keptLeft = left.slice(0, NEIGHBOR_CAP), keptRight = right.slice(0, NEIGHBOR_CAP);

    const box = laneBoxes[0];
    const place = (list, relation) => {
      const total = list.length * (CARD_H + 24) - 24;
      const y0 = box.y + box.height / 2 - total / 2;
      const x = relation === "Caller" ? box.x - NEIGHBOR_GAP - CARD_W : box.x + box.width + NEIGHBOR_GAP;
      return list.map((it, i) => ({
        id: it.id,
        type: "laneCard",
        position: { x, y: y0 + i * (CARD_H + 24) },
        draggable: false,
        data: { ...laneCardData(laneByRoot.get(it.id), focusLane), relation },
      }));
    };

    const kept = new Set([...keptLeft, ...keptRight].map((it) => it.id));
    const agg = new Map();
    Object.values(visibleEdges).forEach((e) => {
      if (e.kind === "CONTAINS") return;
      const sIn = focusSet.has(e.source), tIn = focusSet.has(e.target);
      if (sIn === tIn) return;
      const other = laneOf[sIn ? e.target : e.source];
      if (!kept.has(other)) return;
      const key = sIn ? `${e.source}\u0000${other}` : `${other}\u0000${e.target}`;
      agg.set(key, (agg.get(key) || 0) + 1);
    });
    const edges = [...agg].map(([key, count]) => {
      const [source, target] = key.split("\u0000");
      const hue = laneByRoot.get(source)?.hue ?? laneByRoot.get(target)?.hue;
      return { ...laneEdge(source, target, count, hue), id: `x:${source}>${target}` };
    });
    return {
      nodes: [...place(keptLeft, "Caller"), ...place(keptRight, "Callee")],
      edges,
      hiddenCallers: left.length - keptLeft.length,
      hiddenCallees: right.length - keptRight.length,
    };
  }, [mode, laneBoxes, laneEdges, focusLaneId, focusSet, laneByRoot, laneOf, visibleEdges, focusLane]);

  const graphNodes = useMemo(
    () => (mode === "overview" ? overviewGraph.nodes : mode === "focus" ? [...rfNodes, ...focusExtras.nodes] : rfNodes),
    [mode, overviewGraph, rfNodes, focusExtras],
  );
  const graphEdges = useMemo(
    () => (mode === "overview" ? overviewGraph.edges : mode === "focus" ? [...rfEdges, ...focusExtras.edges] : rfEdges),
    [mode, overviewGraph, rfEdges, focusExtras],
  );

  // Direct neighbors of the hovered node (both directions) — everything else
  // dims while hovering, so the eye reads "this node's connections" first.
  // Classic mode reads the store's edges; the other modes read what's drawn,
  // since their cards stand in for whole lanes.
  const hoverNeighbors = useMemo(() => {
    if (!hoveredId) return null;
    const set = new Set([hoveredId]);
    (mode === "classic" ? Object.values(visibleEdges) : graphEdges).forEach((e) => {
      if (e.source === hoveredId) set.add(e.target);
      if (e.target === hoveredId) set.add(e.source);
    });
    return set;
  }, [hoveredId, mode, visibleEdges, graphEdges]);

  useEffect(() => {
    setNodes(graphNodes);
    setEdges(graphEdges);
  }, [graphNodes, graphEdges, setNodes, setEdges]);

  // Selecting something from outside the canvas (e.g. Graph Insights) while
  // the overview is up would otherwise select an invisible node — jump to its lane.
  useEffect(() => {
    if (mode !== "overview" || !selectedId) return;
    const lane = laneOf[selectedId];
    if (lane) focusLane(lane, true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedId]);

  // Targeted hover patch — runs only when hover changes, and only touches
  // the nodes whose dimmed state actually flips (the hovered node's direct
  // neighborhood), instead of rebuilding the whole `rfNodes` array. Nodes
  // whose `data` reference doesn't change are skipped by React.memo.
  useEffect(() => {
    if (selectedId) return;
    setNodes((nds) => nds.map((n) => {
      if (n.type === "laneBg") return n;
      const shouldDim = !!hoverNeighbors && !hoverNeighbors.has(n.id);
      if (!!n.data.isDimmed === shouldDim) return n;
      return { ...n, data: { ...n.data, isDimmed: shouldDim } };
    }));
  }, [hoverNeighbors, selectedId, setNodes]);

  // Targeted hover patch for edges — same idea: only edges touching the
  // hovered node (or that were touching the previously hovered node) get a
  // new style object; everything else keeps its reference.
  useEffect(() => {
    if (selectedId) return;
    setEdges((eds) => eds.map((e) => {
      const d = e.data;
      if (!d || d.isHighlightedEdge) return e; // search-highlighted edges ignore hover
      const touchesHover = hoveredId && (e.source === hoveredId || e.target === hoveredId);
      let width, opacity;
      if (touchesHover) {
        width = d.baseWidth + 0.8; opacity = 0.95;
      } else if (hoveredId) {
        width = d.restWidth ?? 1; opacity = d.isContains ? 0.05 : 0.06;
      } else {
        width = d.restWidth ?? 1; opacity = d.restOpacity ?? (d.isContains ? 0.28 : 0.2);
      }
      if (e.style.strokeWidth === width && e.style.opacity === opacity) return e;
      return { ...e, style: { ...e.style, strokeWidth: width, opacity } };
    }));
  }, [hoveredId, selectedId, setEdges]);

  useEffect(() => {
    const count = graphNodes.length;
    const key = `${mode}:${focusLaneId}:${count}`;
    if (count === 0 || key === prevKeyRef.current) return;
    prevKeyRef.current = key;
    // Animating fitView is a nice touch on small graphs but adds a 350ms
    // stretch of continuous layout/paint work across every node on large
    // ones — skip the animation once the view is big enough for that to be
    // felt as lag rather than seen as polish.
    const animate = count <= FIT_VIEW_ANIMATE_THRESHOLD && !prefersReducedMotion();
    const id = setTimeout(
      () => fitView(animate ? { padding: 0.12, duration: 350 } : { padding: 0.12, duration: 0 }),
      60,
    );
    return () => clearTimeout(id);
  }, [graphNodes, mode, focusLaneId, fitView]);

  const isEmpty = Object.keys(visibleNodes).length === 0;

  return (
    <div className="w-full h-full relative">
      {isEmpty && <EmptyState />}
      {mode !== "classic" && (
        <nav
          aria-label="Graph Navigation"
          className="absolute top-3 left-3 z-10 max-w-[60%] flex items-center gap-2 px-3 py-1.5 rounded-lg text-xs bg-[#0a0f1a]/90 border border-[#1a2540] backdrop-blur-sm"
        >
          <button
            type="button"
            onClick={() => focusLane(null)}
            aria-current={mode === "overview" ? "page" : undefined}
            disabled={mode === "overview"}
            className="shrink-0 font-semibold text-blue-300 hover:text-blue-200 disabled:text-white disabled:cursor-default"
          >
            Overview
          </button>
          {mode === "overview" ? (
            <span className="text-slate-500 truncate">
              {fmt.format(lanes.length)} lanes · calls read left to right, wrapping like text · select a lane to focus
            </span>
          ) : (
            <>
              <span aria-hidden="true" className="text-slate-600">›</span>
              <span translate="no" className="font-semibold text-white truncate">{laneByRoot.get(focusLaneId)?.label}</span>
              {(focusExtras.hiddenCallers > 0 || focusExtras.hiddenCallees > 0) && (
                <span className="shrink-0 text-slate-500 tabular-nums">
                  · +{fmt.format(focusExtras.hiddenCallers + focusExtras.hiddenCallees)} more connected lanes not shown
                </span>
              )}
            </>
          )}
          <span role="status" className="sr-only">
            {mode === "overview" ? "Showing lane overview" : `Focused on ${laneByRoot.get(focusLaneId)?.label}`}
          </span>
        </nav>
      )}
      {nodeBudget?.truncated && (
        <div role="status" className="absolute top-3 left-1/2 -translate-x-1/2 z-10 px-3 py-1.5 rounded-full text-[11px] font-medium text-amber-100 bg-amber-950/90 border border-amber-700/60 shadow-lg backdrop-blur-sm">
          {nodeBudget.tiered
            ? `Showing ${fmt.format(nodeBudget.shown)} top-level functions of ${fmt.format(nodeBudget.total)} — click a node's ▸ to reveal what it calls`
            : `Showing ${fmt.format(nodeBudget.shown)} of ${fmt.format(nodeBudget.total)} nodes — zoom in or expand a module to see more`}
        </div>
      )}
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        onNodeClick={(_, node) => { if (node.type !== "laneBg" && node.type !== "laneCard") selectNode(node.id); }}
        onNodeMouseEnter={(_, node) => { if (node.type !== "laneBg") setHoveredId(node.id); }}
        onNodeMouseLeave={() => setHoveredId(null)}
        onPaneClick={clearSelection}
        fitView
        proOptions={{ hideAttribution: true }}
        nodesDraggable={false}
        nodesConnectable={false}
        elementsSelectable
        panOnDrag
        zoomOnScroll
        minZoom={0.1}
        maxZoom={2}
        onlyRenderVisibleElements
      >
        <Background
          variant={BackgroundVariant.Dots}
          gap={24}
          size={1}
          color="#1a2540"
        />
        <Controls position="bottom-left" showInteractive={false} aria-label="Graph View Controls" />
        <MiniMap
          ariaLabel="Graph Minimap"
          pannable
          zoomable
          nodeColor={(n) => n.type === "laneBg" ? "transparent" : n.type === "laneCard" ? branchNodeColor(n.data.hue, 0) : (n.data?.branchColor || (n.type === "fileNode" ? "#1e3a8a" : "#312e81"))}
          maskColor="rgba(7,9,15,0.85)"
          style={{ borderRadius: 10 }}
        />
      </ReactFlow>
    </div>
  );
}

export default function GraphView() {
  return <GraphViewInner />;
}
