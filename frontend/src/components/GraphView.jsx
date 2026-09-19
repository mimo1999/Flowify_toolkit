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

const FIT_VIEW_ANIMATE_THRESHOLD = 150;

// ─────────────────────────────────────────────────────────────────────────────
// Role badge fallback rules
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
  if (semCfg.label) return semCfg;
  const p = (filePath || "").toLowerCase();
  for (const rule of ROLE_FALLBACK_RULES) {
    if (rule.test(p)) return rule;
  }
  return semCfg;
}

// ─────────────────────────────────────────────────────────────────────────────
// Heatmap encoding
// ─────────────────────────────────────────────────────────────────────────────
const COMPLEXITY_BORDER_WIDTH = { low: 1, medium: 1.5, high: 2.5, very_high: 4 };

function complexityBorderWidth(complexity) {
  return COMPLEXITY_BORDER_WIDTH[complexity] || 1;
}

function fanSizeBoost(fanRatio) {
  if (!fanRatio) return 0;
  return Math.round(Math.sqrt(fanRatio) * 34);
}

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
// Branch coloring
// ─────────────────────────────────────────────────────────────────────────────
const BRANCH_HUES = [10, 30, 48, 330, 300, 275, 355, 50];

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

function computeBranchInfo(rawNodes, rootIds) {
  const byId = {};
  rawNodes.forEach((n) => (byId[n.id] = n));

  const rootCache = {};
  const depthCache = {};
  function resolve(id, seen) {
    if (rootCache[id] !== undefined) return rootCache[id];
    if (seen.has(id)) { rootCache[id] = id; depthCache[id] = 0; return id; }
    seen.add(id);
    const n = byId[id];
    if (!n || !n.parent || !byId[n.parent]) {
      rootCache[id] = id;
      depthCache[id] = 0;
      return id;
    }
    const parentRoot = resolve(n.parent, seen);
    rootCache[id] = parentRoot;
    depthCache[id] = (depthCache[n.parent] ?? 0) + 1;
    return parentRoot;
  }
  rawNodes.forEach((n) => resolve(n.id, new Set()));

  const discovered = [...new Set(Object.values(rootCache))];
  const orderedRoots = [
    ...rootIds.filter((id) => discovered.includes(id)),
    ...discovered.filter((id) => !rootIds.includes(id)),
  ];
  const hueOf = {};
  orderedRoots.forEach((rid, i) => { hueOf[rid] = BRANCH_HUES[i % BRANCH_HUES.length]; });

  const info = {};
  rawNodes.forEach((n) => {
    const root = rootCache[n.id];
    info[n.id] = { root, depth: depthCache[n.id] ?? 0, hue: hueOf[root] ?? BRANCH_HUES[0] };
  });
  return { info, orderedRoots };
}

// ─────────────────────────────────────────────────────────────────────────────
// Icons
// ─────────────────────────────────────────────────────────────────────────────
const FileIcon = () => (
  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
    <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
    <polyline points="14,2 14,8 20,8"/>
  </svg>
);
const FnIcon = () => (
  <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
    <polyline points="16,18 22,12 16,6"/><polyline points="8,6 2,12 8,18"/>
  </svg>
);
const ClassIcon = () => (
  <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
    <rect x="2" y="3" width="20" height="14" rx="2"/><line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/>
  </svg>
);
const ChevronRight = () => (
  <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
    <polyline points="9,18 15,12 9,6"/>
  </svg>
);
const ChevronDown = () => (
  <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
    <polyline points="6,9 12,15 18,9"/>
  </svg>
);

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
// Custom node components with 4-side target & source handles
// ─────────────────────────────────────────────────────────────────────────────
const FileNodeComponent = React.memo(function FileNodeComponent({ id, data }) {
  const { label, filePath, fnCount, description, isExpanded, isHighlighted, isSelected, isUpstream, isDownstream, isRoot, isDimmed, toggleExpand, deepExpand, branchColor, metrics, fanRatio, prRatio } = data;
  const zoom = useRFStore((s) => s.transform[2]);
  const lod = zoom < 0.5;
  const showDesc = !lod && description && description !== filePath && !description.startsWith("backend/") && !description.startsWith("frontend/") && !description.startsWith("tests/");
  const branchBorder = (!isHighlighted && !isSelected && !isUpstream && !isDownstream && branchColor) ? { borderColor: branchColor } : {};
  const boost = fanSizeBoost(fanRatio);
  const sizeStyle = isRoot
    ? { minWidth: 210 + boost, maxWidth: 320 + boost }
    : { minWidth: 170 + boost, maxWidth: 260 + boost };
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
          ? "border-blue-400 shadow-[0_0_16px_rgba(59,130,246,0.5)] ring-2 ring-blue-400/50"
          : isUpstream
          ? "border-sky-400 shadow-[0_0_14px_rgba(56,189,248,0.4)]"
          : isDownstream
          ? "border-emerald-400 shadow-[0_0_14px_rgba(16,185,129,0.4)]"
          : "hover:brightness-125 shadow-[0_2px_12px_rgba(0,0,0,0.4)]",
      ].join(" ")}
      style={{ ...sizeStyle, ...branchBorder, ...borderWidthStyle, opacity: isDimmed ? 0.25 : 1, filter: glow }}
    >
      <BranchStripe color={isHighlighted ? null : branchColor} rounded="rounded-l-xl" />

      {/* Target Handles */}
      <Handle type="target" position={Position.Left} id="target-left" className="!bg-blue-500 !border-blue-700 !w-2 !h-2" />
      <Handle type="target" position={Position.Right} id="target-right" className="!bg-blue-500 !border-blue-700 !w-2 !h-2" />
      <Handle type="target" position={Position.Top} id="target-top" className="!bg-blue-500 !border-blue-700 !w-2 !h-2" />
      <Handle type="target" position={Position.Bottom} id="target-bottom" className="!bg-blue-500 !border-blue-700 !w-2 !h-2" />

      <div className={isRoot ? "px-4 py-3.5" : "px-4 py-3"}>
        <div className="flex items-center gap-2 mb-1">
          <span className="text-blue-400 shrink-0"><FileIcon /></span>
          <span className="text-[10px] uppercase tracking-widest text-blue-400/70 font-semibold">File</span>
          {isUpstream && <span className="text-[9px] font-semibold text-sky-400 bg-sky-950/80 px-1.5 py-0.5 rounded border border-sky-700/50">Caller</span>}
          {isDownstream && <span className="text-[9px] font-semibold text-emerald-400 bg-emerald-950/80 px-1.5 py-0.5 rounded border border-emerald-700/50">Callee</span>}
          <button
            onClick={(e) => { e.stopPropagation(); (e.shiftKey ? deepExpand : toggleExpand)(id); }}
            title={isExpanded ? "Collapse" : "Expand to see functions (Shift-click: expand several levels)"}
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

      {/* Source Handles */}
      <Handle type="source" position={Position.Left} id="source-left" className="!bg-blue-500 !border-blue-700 !w-2 !h-2" />
      <Handle type="source" position={Position.Right} id="source-right" className="!bg-blue-500 !border-blue-700 !w-2 !h-2" />
      <Handle type="source" position={Position.Top} id="source-top" className="!bg-blue-500 !border-blue-700 !w-2 !h-2" />
      <Handle type="source" position={Position.Bottom} id="source-bottom" className="!bg-blue-500 !border-blue-700 !w-2 !h-2" />
    </div>
  );
});

const FunctionNodeComponent = React.memo(function FunctionNodeComponent({ id, data }) {
  const { label, kind, filePath, description, isHighlighted, isSelected, isUpstream, isDownstream, isExpanded, hasCallees, semanticKind, isDimmed, toggleExpand, deepExpand, branchColor, metrics, fanRatio, prRatio } = data;
  const zoom = useRFStore((s) => s.transform[2]);
  const lod = zoom < 0.5;

  const isClass = kind === "class";
  const role = resolveRole(semanticKind || "CALLS", filePath);
  const accent = isClass
    ? { border: "border-emerald-700/60", hoverBorder: "hover:border-emerald-500/80", text: "text-emerald-400", bg: "from-emerald-950/80 to-emerald-900/30" }
    : { border: "border-indigo-800/60", hoverBorder: "hover:border-indigo-500/80", text: "text-indigo-400", bg: "from-indigo-950/80 to-indigo-900/30" };

  const branchBorder = (!isHighlighted && !isSelected && !isUpstream && !isDownstream && branchColor) ? { borderColor: branchColor } : {};
  const showDesc = !lod && description && !description.startsWith("(stub)") && description.length > 4;
  const boost = fanSizeBoost(fanRatio);
  const sizeStyle = isClass
    ? { minWidth: 180 + boost, maxWidth: 280 + boost }
    : { minWidth: 145 + boost, maxWidth: 225 + boost };
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
          ? "border-blue-400 shadow-[0_0_16px_rgba(59,130,246,0.5)] ring-2 ring-blue-400/50"
          : isUpstream
          ? "border-sky-400 shadow-[0_0_12px_rgba(56,189,248,0.4)]"
          : isDownstream
          ? "border-emerald-400 shadow-[0_0_12px_rgba(16,185,129,0.4)]"
          : `${accent.border} hover:brightness-125 shadow-[0_1px_8px_rgba(0,0,0,0.3)]`,
      ].join(" ")}
      style={{ ...sizeStyle, ...branchBorder, ...borderWidthStyle, opacity: isDimmed ? 0.25 : 1, filter: glow }}
    >
      <BranchStripe color={isHighlighted ? null : branchColor} rounded="rounded-l-lg" />

      {/* Target Handles */}
      <Handle type="target" position={Position.Left} id="target-left" className="!bg-indigo-500 !border-indigo-700 !w-1.5 !h-1.5" />
      <Handle type="target" position={Position.Right} id="target-right" className="!bg-indigo-500 !border-indigo-700 !w-1.5 !h-1.5" />
      <Handle type="target" position={Position.Top} id="target-top" className="!bg-indigo-500 !border-indigo-700 !w-1.5 !h-1.5" />
      <Handle type="target" position={Position.Bottom} id="target-bottom" className="!bg-indigo-500 !border-indigo-700 !w-1.5 !h-1.5" />

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
              {role.icon} {role.label}
            </span>
          )}
          {isUpstream && <span className="text-[8px] font-bold text-sky-400 bg-sky-950 px-1 rounded border border-sky-800">Caller</span>}
          {isDownstream && <span className="text-[8px] font-bold text-emerald-400 bg-emerald-950 px-1 rounded border border-emerald-800">Callee</span>}
          {hasCallees && (
            <button
              onClick={(e) => { e.stopPropagation(); (e.shiftKey ? deepExpand : toggleExpand)(id); }}
              title={isExpanded ? "Collapse" : "Expand to see what this calls (Shift-click: expand several levels)"}
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
      </div>

      {/* Source Handles */}
      <Handle type="source" position={Position.Left} id="source-left" className="!bg-indigo-500 !border-indigo-700 !w-1.5 !h-1.5" />
      <Handle type="source" position={Position.Right} id="source-right" className="!bg-indigo-500 !border-indigo-700 !w-1.5 !h-1.5" />
      <Handle type="source" position={Position.Top} id="source-top" className="!bg-indigo-500 !border-indigo-700 !w-1.5 !h-1.5" />
      <Handle type="source" position={Position.Bottom} id="source-bottom" className="!bg-indigo-500 !border-indigo-700 !w-1.5 !h-1.5" />
    </div>
  );
});

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

const nodeTypes = {
  fileNode:     FileNodeComponent,
  functionNode: FunctionNodeComponent,
  laneBg:       LaneBackgroundComponent,
};

// ─────────────────────────────────────────────────────────────────────────────
// Serpentine / Snake Flow Layout Algorithm
// ─────────────────────────────────────────────────────────────────────────────
// Orders nodes sequentially in a branch by depth & connections, placing them
// in serpentine rows:
//   Row 0: Left -> Right [0, 1, 2, 3]
//   Row 1: Right -> Left [7, 6, 5, 4]  (drops down 1 step from 3 to 4)
//   Row 2: Left -> Right [8, 9, 10, 11] (drops down 1 step from 7 to 8)
// This fits screen constraints cleanly without horizontal overflow while
// making execution direction visually obvious.
// ─────────────────────────────────────────────────────────────────────────────
function serpentineLayout(rawNodes, branchInfo) {
  if (!rawNodes || rawNodes.length === 0) return { placed: [], width: 0, height: 0 };

  const sorted = [...rawNodes].sort((a, b) => {
    const depthA = branchInfo[a.id]?.depth ?? 0;
    const depthB = branchInfo[b.id]?.depth ?? 0;
    if (depthA !== depthB) return depthA - depthB;
    if (a.parent !== b.parent) return (a.parent || "").localeCompare(b.parent || "");
    return (a.label || a.id).localeCompare(b.label || b.id);
  });

  const MAX_COLS = Math.min(4, Math.max(2, Math.ceil(Math.sqrt(sorted.length))));
  const cellW = 310;
  const cellH = 120;
  const GAP_X = 50;
  const GAP_Y = 60;

  const placed = [];
  let maxRow = 0;

  sorted.forEach((n, i) => {
    const row = Math.floor(i / MAX_COLS);
    const posInRow = i % MAX_COLS;
    maxRow = Math.max(maxRow, row);

    const isEvenRow = row % 2 === 0;
    const col = isEvenRow ? posInRow : (MAX_COLS - 1) - posInRow;

    const x = col * (cellW + GAP_X);
    const y = row * (cellH + GAP_Y);

    placed.push({
      ...n,
      position: { x, y },
      flowDirection: isEvenRow ? "LR" : "RL",
      flowRow: row,
      flowCol: col,
    });
  });

  const width = MAX_COLS * cellW + (MAX_COLS - 1) * GAP_X;
  const height = (maxRow + 1) * cellH + maxRow * GAP_Y;

  return { placed, width, height };
}

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
    const { placed, width, height } = serpentineLayout(groupNodes, branchInfo);
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
// Dynamic Handle Selection for Directional Edges
// ─────────────────────────────────────────────────────────────────────────────
function getEdgeHandles(sPos, tPos) {
  if (!sPos || !tPos) return { sourceHandle: "source-right", targetHandle: "target-left" };

  const dx = tPos.x - sPos.x;
  const dy = tPos.y - sPos.y;

  if (dx > 60) {
    return { sourceHandle: "source-right", targetHandle: "target-left" };
  } else if (dx < -60) {
    return { sourceHandle: "source-left", targetHandle: "target-right" };
  } else {
    if (dy >= 0) {
      return { sourceHandle: "source-bottom", targetHandle: "target-top" };
    } else {
      return { sourceHandle: "source-top", targetHandle: "target-bottom" };
    }
  }
}

function EmptyState() {
  return (
    <div className="absolute inset-0 flex flex-col items-center justify-center pointer-events-none select-none">
      <div className="text-center fade-in">
        <div className="w-20 h-20 mx-auto mb-6 rounded-2xl bg-blue-950/60 border border-blue-800/40 flex items-center justify-center">
          <svg width="36" height="36" viewBox="0 0 24 24" fill="none" stroke="#3b82f6" strokeWidth="1.5">
            <circle cx="11" cy="11" r="8"/><circle cx="11" cy="11" r="3"/>
            <line x1="21" y1="21" x2="16.65" y2="16.65"/>
          </svg>
        </div>
        <div className="text-xl font-semibold text-white/80 mb-2">No graph loaded</div>
        <div className="text-sm text-slate-500 max-w-xs leading-relaxed">
          Enter a repository path in the sidebar and click <strong className="text-slate-400">Ingest</strong> to explore its code graph.
        </div>
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Main GraphView Inner Component
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
  const selectNode     = useFlowStore((s) => s.selectNode);
  const clearSelection = useFlowStore((s) => s.clearSelection);
  const nodeMetrics    = useFlowStore((s) => s.nodeMetrics);
  const fileMetrics    = useFlowStore((s) => s.fileMetrics);
  const nodeBudget     = useFlowStore((s) => s.nodeBudget);

  const [nodes, setNodes, onNodesChange] = useNodesState([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState([]);
  const { fitView } = useReactFlow();
  const prevCountRef = useRef(0);
  const [hoveredId, setHoveredId] = useState(null);

  const childrenOf = useMemo(() => {
    const map = {};
    const ids = new Set(Object.keys(visibleNodes));
    Object.values(visibleNodes).forEach((n) => {
      if (n.parent && ids.has(n.parent)) (map[n.parent] ||= []).push(n.id);
    });
    return map;
  }, [visibleNodes]);

  const metricsStats = useMemo(() => {
    let maxFan = 0, maxPr = 0;
    Object.values(nodeMetrics).forEach((m) => {
      maxFan = Math.max(maxFan, (m.in_degree || 0) + (m.out_degree || 0));
      maxPr = Math.max(maxPr, m.pagerank || 0);
    });
    return { maxFan, maxPr };
  }, [nodeMetrics]);

  const fileMetricsStats = useMemo(() => {
    let maxFan = 0, maxPr = 0;
    Object.values(fileMetrics).forEach((m) => {
      maxFan = Math.max(maxFan, (m.in_degree || 0) + (m.out_degree || 0));
      maxPr = Math.max(maxPr, m.pagerank || 0);
    });
    return { maxFan, maxPr };
  }, [fileMetrics]);

  const { info: branchInfo, orderedRoots } = useMemo(
    () => computeBranchInfo(Object.values(visibleNodes), rootIds),
    [visibleNodes, rootIds],
  );

  const { positioned, lanes } = useMemo(
    () => layoutBranches(Object.values(visibleNodes), branchInfo, orderedRoots),
    [visibleNodes, branchInfo, orderedRoots],
  );

  // Position lookup map for edge handle calculation
  const nodePosMap = useMemo(() => {
    const map = {};
    positioned.forEach((n) => { map[n.id] = n.position; });
    return map;
  }, [positioned]);

  // Selection Flow Tracing (Upstream callers & Downstream callees)
  const selectionFlow = useMemo(() => {
    if (!selectedId || !visibleNodes[selectedId]) {
      return { activeNodes: null, upstreamNodes: new Set(), downstreamNodes: new Set(), upstreamEdges: new Set(), downstreamEdges: new Set(), activeEdges: new Set() };
    }

    const upstreamNodes = new Set();
    const downstreamNodes = new Set();
    const upstreamEdges = new Set();
    const downstreamEdges = new Set();

    const incomingMap = {};
    const outgoingMap = {};
    Object.values(visibleEdges).forEach((e) => {
      if (e.kind === "CONTAINS") return;
      (incomingMap[e.target] ||= []).push(e);
      (outgoingMap[e.source] ||= []).push(e);
    });

    // BFS Upstream Callers
    const upQueue = [selectedId];
    const upVisited = new Set([selectedId]);
    while (upQueue.length > 0) {
      const curr = upQueue.shift();
      const inc = incomingMap[curr] || [];
      inc.forEach((e) => {
        upstreamEdges.add(e.id);
        if (!upVisited.has(e.source)) {
          upVisited.add(e.source);
          upstreamNodes.add(e.source);
          upQueue.push(e.source);
        }
      });
    }

    // BFS Downstream Callees
    const downQueue = [selectedId];
    const downVisited = new Set([selectedId]);
    while (downQueue.length > 0) {
      const curr = downQueue.shift();
      const out = outgoingMap[curr] || [];
      out.forEach((e) => {
        downstreamEdges.add(e.id);
        if (!downVisited.has(e.target)) {
          downVisited.add(e.target);
          downstreamNodes.add(e.target);
          downQueue.push(e.target);
        }
      });
    }

    const activeNodes = new Set([selectedId, ...upstreamNodes, ...downstreamNodes]);
    const activeEdges = new Set([...upstreamEdges, ...downstreamEdges]);

    return { selectedId, upstreamNodes, downstreamNodes, activeNodes, upstreamEdges, downstreamEdges, activeEdges };
  }, [selectedId, visibleNodes, visibleEdges]);

  // Hover Neighbors
  const hoverNeighbors = useMemo(() => {
    if (!hoveredId) return null;
    const set = new Set([hoveredId]);
    Object.values(visibleEdges).forEach((e) => {
      if (e.source === hoveredId) set.add(e.target);
      if (e.target === hoveredId) set.add(e.source);
    });
    return set;
  }, [hoveredId, visibleEdges]);

  // RF Edges with Directional Handles & Arrowheads
  const rfEdges = useMemo(() => {
    const hl = new Set(highlight);
    const { activeEdges, upstreamEdges, downstreamEdges } = selectionFlow;

    return Object.values(visibleEdges).map((e) => {
      const isContains = e.kind === "CONTAINS";
      const isModuleFlow = e.kind === "FLOW";
      const isHighlightedEdge = hl.has(e.source) && hl.has(e.target);
      const isUpstreamEdge = upstreamEdges.has(e.id);
      const isDownstreamEdge = downstreamEdges.has(e.id);
      const isActiveFlowEdge = isUpstreamEdge || isDownstreamEdge;

      if (isContains) {
        const siblings = childrenOf[e.source] || [];
        if (siblings.length > 3) {
          const first = siblings[0], last = siblings[siblings.length - 1];
          if (e.target !== first && e.target !== last) return null;
        }
      }

      const sPos = nodePosMap[e.source];
      const tPos = nodePosMap[e.target];
      const { sourceHandle, targetHandle } = getEdgeHandles(sPos, tPos);

      const targetNode = visibleNodes[e.target];
      const targetSemanticKind = targetNode?.adapter_metadata?.semantic_kind || "CALLS";
      const semCfg = getSemanticConfig(targetSemanticKind);
      const hasSemanticLabel = !isContains && !!semCfg.label;

      const srcBranch = branchInfo[e.source];
      const tgtBranch = branchInfo[e.target];
      const sameBranch = srcBranch && tgtBranch && srcBranch.root === tgtBranch.root;

      let stroke = sameBranch
        ? branchEdgeColor(srcBranch.hue, Math.max(srcBranch.depth, tgtBranch.depth))
        : "#5b6a85";
      let dash = isContains ? "5 4" : (!sameBranch ? "2 5" : undefined);
      let baseWidth = isContains ? 1 : (isModuleFlow ? 1.5 : 1.2);
      let animated = false;
      let opacity = isContains ? 0.28 : 0.45;

      if (isHighlightedEdge) {
        stroke = "#f59e0b";
        dash = undefined;
        baseWidth = 2.5;
        animated = true;
        opacity = 0.95;
      } else if (isUpstreamEdge) {
        stroke = "#38bdf8"; // cyan caller
        dash = "6 4";
        baseWidth = 2.5;
        animated = true;
        opacity = 0.95;
      } else if (isDownstreamEdge) {
        stroke = "#10b981"; // emerald callee
        dash = "6 4";
        baseWidth = 2.5;
        animated = true;
        opacity = 0.95;
      } else if (selectedId) {
        opacity = 0.08;
      }

      return {
        id: e.id,
        source: e.source,
        target: e.target,
        sourceHandle,
        targetHandle,
        animated,
        type: "smoothstep",
        label: (hasSemanticLabel && !isHighlightedEdge) ? semCfg.label : undefined,
        labelStyle: { fill: semCfg.color, fontSize: 8, fontWeight: 600 },
        labelBgStyle: { fill: "#07090f", fillOpacity: 0.8 },
        markerEnd: {
          type: MarkerType.ArrowClosed,
          width: isActiveFlowEdge ? 16 : 12,
          height: isActiveFlowEdge ? 16 : 12,
          color: stroke,
        },
        style: { stroke, strokeDasharray: dash, strokeWidth: baseWidth, opacity },
        data: { isContains, isHighlightedEdge, baseWidth },
      };
    }).filter(Boolean);
  }, [visibleEdges, visibleNodes, childrenOf, highlight, branchInfo, nodePosMap, selectionFlow, selectedId]);

  const rootIdSet = useMemo(() => new Set(rootIds), [rootIds]);

  const laneNodes = useMemo(() => lanes.map((lane) => ({
    id: `lanebg::${lane.root}`,
    type: "laneBg",
    position: { x: lane.x, y: lane.y },
    draggable: false,
    selectable: false,
    style: { pointerEvents: "none" },
    data: { width: lane.width, height: lane.height, hue: lane.hue },
  })), [lanes]);

  const rfNodes = useMemo(() => {
    const hl = new Set(highlight);
    const { activeNodes, upstreamNodes, downstreamNodes } = selectionFlow;

    const functionNodes = positioned.map((n) => {
      const isFile      = n.kind === "file" || n.kind === "module";
      const isSelected  = n.id === selectedId;
      const isUpstream  = upstreamNodes.has(n.id);
      const isDownstream = downstreamNodes.has(n.id);
      const isHighlighted = hl.has(n.id);
      const isExpanded  = !!expanded[n.id];
      const branch = branchInfo[n.id];

      const isDimmed = activeNodes ? !activeNodes.has(n.id) : false;

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
          isUpstream,
          isDownstream,
          isDimmed,
          isRoot: isFile && rootIdSet.has(n.id),
          hasCallees:  true,
          toggleExpand,
          deepExpand,
          branchColor: branch ? branchNodeColor(branch.hue, branch.depth) : null,
          metrics,
          fanRatio: stats.maxFan ? fan / stats.maxFan : 0,
          prRatio: metrics && stats.maxPr ? metrics.pagerank / stats.maxPr : 0,
        },
      };
    });
    return [...laneNodes, ...functionNodes];
  }, [positioned, highlight, expanded, selectedId, selectionFlow, toggleExpand, deepExpand, branchInfo, rootIdSet, laneNodes, nodeMetrics, metricsStats, fileMetrics, fileMetricsStats]);

  useEffect(() => {
    setNodes(rfNodes);
    setEdges(rfEdges);
  }, [rfNodes, rfEdges, setNodes, setEdges]);

  // Hover dimming patch (when no node is explicitly selected)
  useEffect(() => {
    if (selectedId) return; // selection flow active
    setNodes((nds) => nds.map((n) => {
      if (n.type === "laneBg") return n;
      const shouldDim = !!hoverNeighbors && !hoverNeighbors.has(n.id);
      if (!!n.data.isDimmed === shouldDim) return n;
      return { ...n, data: { ...n.data, isDimmed: shouldDim } };
    }));
  }, [hoverNeighbors, selectedId, setNodes]);

  useEffect(() => {
    if (selectedId) return;
    setEdges((eds) => eds.map((e) => {
      const d = e.data;
      if (!d || d.isHighlightedEdge) return e;
      const touchesHover = hoveredId && (e.source === hoveredId || e.target === hoveredId);
      let width, opacity;
      if (touchesHover) {
        width = d.baseWidth + 0.8; opacity = 0.95;
      } else if (hoveredId) {
        width = 1; opacity = d.isContains ? 0.05 : 0.06;
      } else {
        width = d.baseWidth; opacity = d.isContains ? 0.28 : 0.45;
      }
      if (e.style.strokeWidth === width && e.style.opacity === opacity) return e;
      return { ...e, style: { ...e.style, strokeWidth: width, opacity } };
    }));
  }, [hoveredId, selectedId, setEdges]);

  useEffect(() => {
    const count = rfNodes.length;
    if (count === 0 || count === prevCountRef.current) return;
    prevCountRef.current = count;
    const animate = count <= FIT_VIEW_ANIMATE_THRESHOLD;
    const id = setTimeout(
      () => fitView(animate ? { padding: 0.12, duration: 350 } : { padding: 0.12, duration: 0 }),
      60,
    );
    return () => clearTimeout(id);
  }, [rfNodes, fitView]);

  const isEmpty = Object.keys(visibleNodes).length === 0;

  return (
    <div className="w-full h-full relative">
      {isEmpty && <EmptyState />}
      {nodeBudget?.truncated && (
        <div className="absolute top-3 left-1/2 -translate-x-1/2 z-10 px-3 py-1.5 rounded-full text-[11px] font-medium text-amber-100 bg-amber-950/90 border border-amber-700/60 shadow-lg backdrop-blur-sm">
          Showing {nodeBudget.shown} of {nodeBudget.total} nodes — zoom in or expand a module to see more
        </div>
      )}
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        onNodeClick={(_, node) => { if (node.type !== "laneBg") selectNode(node.id); }}
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
        <Controls position="bottom-left" showInteractive={false} />
        <MiniMap
          pannable
          zoomable
          nodeColor={(n) => n.type === "laneBg" ? "transparent" : (n.data?.branchColor || (n.type === "fileNode" ? "#1e3a8a" : "#312e81"))}
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
