import React, { useMemo, useEffect, useRef, useCallback } from "react";
import ReactFlow, {
  Background, Controls, MiniMap,
  useNodesState, useEdgesState,
  Handle, Position,
  useReactFlow,
  BackgroundVariant,
} from "reactflow";
import dagre from "@dagrejs/dagre";
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

// ─────────────────────────────────────────────────────────────────────────────
// Icons (inline SVG, no external dep)
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

// ─────────────────────────────────────────────────────────────────────────────
// Custom node types
// ─────────────────────────────────────────────────────────────────────────────
function FileNodeComponent({ data }) {
  const { label, filePath, fnCount, description, isExpanded, isHighlighted, isSelected } = data;
  // Don't show description if it's just the file path repeated
  const showDesc = description && description !== filePath && !description.startsWith("backend/") && !description.startsWith("frontend/") && !description.startsWith("tests/");

  return (
    <div
      className={[
        "group relative rounded-xl border transition-all duration-150 select-none",
        "bg-gradient-to-br from-blue-950/80 to-blue-900/40",
        isHighlighted
          ? "border-amber-400 shadow-[0_0_0_2px_rgba(251,191,36,0.25)] node-highlighted"
          : isSelected
          ? "border-blue-400 shadow-[0_0_16px_rgba(59,130,246,0.3)]"
          : "border-blue-800/60 hover:border-blue-500/80 shadow-[0_2px_12px_rgba(0,0,0,0.4)]",
      ].join(" ")}
      style={{ minWidth: 180, maxWidth: 280 }}
    >
      <Handle type="target" position={Position.Left} className="!bg-blue-500 !border-blue-700 !w-2 !h-2" />

      <div className="px-4 py-3">
        <div className="flex items-center gap-2 mb-1">
          <span className="text-blue-400 shrink-0"><FileIcon /></span>
          <span className="text-[10px] uppercase tracking-widest text-blue-400/70 font-semibold">File</span>
          <span className="ml-auto text-blue-400/50 shrink-0">
            {isExpanded ? <ChevronDown /> : <ChevronRight />}
          </span>
        </div>
        <div className="font-semibold text-sm text-white leading-snug truncate">{label}</div>
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
}

function FunctionNodeComponent({ data }) {
  const { label, kind, description, isHighlighted, isSelected, isExpanded, hasCallees, semanticKind } = data;

  const isClass = kind === "class";
  const semCfg = getSemanticConfig(semanticKind || "CALLS");
  const accent = isClass
    ? { border: "border-emerald-700/60", hoverBorder: "hover:border-emerald-500/80", text: "text-emerald-400", bg: "from-emerald-950/80 to-emerald-900/30" }
    : { border: "border-indigo-800/60", hoverBorder: "hover:border-indigo-500/80", text: "text-indigo-400", bg: "from-indigo-950/80 to-indigo-900/30" };

  const borderStyle = semCfg.label ? { borderColor: `${semCfg.color}60` } : {};
  // Show description only if it's a real one (not a file path, not a stub)
  const showDesc = description && !description.startsWith("(stub)") && description.length > 4;

  return (
    <div
      className={[
        "rounded-lg border transition-all duration-150 select-none",
        `bg-gradient-to-br ${accent.bg}`,
        isHighlighted
          ? "border-amber-400 shadow-[0_0_0_2px_rgba(251,191,36,0.2)] node-highlighted"
          : isSelected
          ? `border-opacity-100 ${accent.border.replace('/60','')}`
          : `${accent.border} ${accent.hoverBorder} shadow-[0_1px_8px_rgba(0,0,0,0.3)]`,
      ].join(" ")}
      style={{ minWidth: 160, maxWidth: 260, ...(!isHighlighted && !isSelected ? borderStyle : {}) }}
    >
      <Handle type="target" position={Position.Left} className="!bg-indigo-500 !border-indigo-700 !w-1.5 !h-1.5" />

      <div className="px-3 py-2.5">
        <div className="flex items-center gap-1.5 mb-0.5">
          <span className={`${accent.text} shrink-0`}>{isClass ? <ClassIcon /> : <FnIcon />}</span>
          <span className={`text-[9px] uppercase tracking-wider font-semibold ${accent.text} opacity-70`}>
            {kind || "fn"}
          </span>
          {semCfg.label && (
            <span
              className="ml-1 text-[8px] font-bold px-1 rounded"
              style={{ background: `${semCfg.color}22`, color: semCfg.color }}
            >
              {semCfg.icon} {semCfg.label}
            </span>
          )}
          {hasCallees && (
            <span className="ml-auto shrink-0 text-slate-500">
              {isExpanded ? <ChevronDown /> : <ChevronRight />}
            </span>
          )}
        </div>
        <div className="font-mono text-xs text-white/90 truncate leading-snug">{label}</div>
        {showDesc && (
          <div className="text-[10px] text-slate-400/80 mt-1 italic leading-snug line-clamp-2">{description}</div>
        )}
      </div>

      <Handle type="source" position={Position.Right} className="!bg-indigo-500 !border-indigo-700 !w-1.5 !h-1.5" />
    </div>
  );
}

const nodeTypes = {
  fileNode:     FileNodeComponent,
  functionNode: FunctionNodeComponent,
};

// ─────────────────────────────────────────────────────────────────────────────
// Dagre layout
// ─────────────────────────────────────────────────────────────────────────────
function dagreLayout(rawNodes, edgeList) {
  const g = new dagre.graphlib.Graph();
  g.setDefaultEdgeLabel(() => ({}));
  g.setGraph({ rankdir: "LR", nodesep: 50, ranksep: 130, marginx: 60, marginy: 60 });

  rawNodes.forEach((n) => {
    const isFile = n.kind === "file" || n.kind === "module";
    g.setNode(n.id, { width: isFile ? 240 : 190, height: isFile ? 88 : 64 });
  });
  edgeList.forEach((e) => {
    if (g.hasNode(e.source) && g.hasNode(e.target)) g.setEdge(e.source, e.target);
  });
  dagre.layout(g);

  const byId = {};
  rawNodes.forEach((n) => (byId[n.id] = n));
  const depthMap = {};
  const calcDepth = (id, d) => {
    if (depthMap[id] !== undefined) return;
    depthMap[id] = d;
    rawNodes.filter((n) => n.parent === id).forEach((c) => calcDepth(c.id, d + 1));
  };
  rawNodes.filter((n) => !n.parent || !byId[n.parent]).forEach((r) => calcDepth(r.id, 0));

  return rawNodes.map((n) => {
    const pos = g.node(n.id);
    return {
      ...n,
      position: pos ? { x: pos.x - pos.width / 2, y: pos.y - pos.height / 2 } : { x: 0, y: 0 },
      depth: depthMap[n.id] ?? 0,
    };
  });
}

// ─────────────────────────────────────────────────────────────────────────────
// Empty state overlay
// ─────────────────────────────────────────────────────────────────────────────
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
        <div className="mt-6 flex items-center gap-6 text-xs text-slate-600">
          <span>Click nodes to expand</span>
          <span className="w-1 h-1 rounded-full bg-slate-700" />
          <span>Ask questions below</span>
          <span className="w-1 h-1 rounded-full bg-slate-700" />
          <span>Copy context for LLMs</span>
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
  const selectedId     = useFlowStore((s) => s.selectedId);
  const highlight      = useFlowStore((s) => s.highlightPath);
  const toggleExpand   = useFlowStore((s) => s.toggleExpand);
  const selectNode     = useFlowStore((s) => s.selectNode);
  const clearSelection = useFlowStore((s) => s.clearSelection);

  const [nodes, setNodes, onNodesChange] = useNodesState([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState([]);
  const { fitView } = useReactFlow();
  const prevCountRef = useRef(0);

  const childrenOf = useMemo(() => {
    const map = {};
    const ids = new Set(Object.keys(visibleNodes));
    Object.values(visibleNodes).forEach((n) => {
      if (n.parent && ids.has(n.parent)) (map[n.parent] ||= []).push(n.id);
    });
    return map;
  }, [visibleNodes]);

  // Build RF edges (with CONTAINS fan-out pruning + semantic kind coloring)
  const rfEdges = useMemo(() => {
    const hl = new Set(highlight);
    return Object.values(visibleEdges).map((e) => {
      const isContains = e.kind === "CONTAINS";
      const isFlow     = e.kind === "FLOW";
      const isHighlightedEdge = hl.has(e.source) && hl.has(e.target);

      if (isContains) {
        const siblings = childrenOf[e.source] || [];
        if (siblings.length > 3) {
          const first = siblings[0], last = siblings[siblings.length - 1];
          if (e.target !== first && e.target !== last) return null;
        }
      }

      // Color edge by the TARGET node's semantic kind
      const targetNode = visibleNodes[e.target];
      const targetSemanticKind = targetNode?.adapter_metadata?.semantic_kind || "CALLS";
      const semCfg = getSemanticConfig(targetSemanticKind);
      const baseEdgeColor = (isContains || !semCfg.label)
        ? (isFlow ? "#3b82f6" : "#2a3f5a")
        : semCfg.edgeColor;

      return {
        id: e.id,
        source: e.source,
        target: e.target,
        animated: isHighlightedEdge,
        type: "smoothstep",
        label: (!isContains && semCfg.label && !isHighlightedEdge) ? semCfg.label : undefined,
        labelStyle: { fill: semCfg.color, fontSize: 8, fontWeight: 600 },
        labelBgStyle: { fill: "#07090f", fillOpacity: 0.8 },
        style: {
          stroke: isContains
            ? "#1e3a5a"
            : isHighlightedEdge
            ? "#f59e0b"
            : baseEdgeColor,
          strokeDasharray: isContains ? "5 4" : undefined,
          strokeWidth: isHighlightedEdge ? 2.5 : isFlow ? 1.5 : (semCfg.label ? 1.5 : 1),
          opacity: isContains ? 0.5 : 0.8,
        },
      };
    }).filter(Boolean);
  }, [visibleEdges, visibleNodes, childrenOf, highlight]);

  // All edges for dagre layout (no pruning — every child needs an edge to be ranked)
  const dagreEdges = useMemo(
    () => Object.values(visibleEdges).map((e) => ({ source: e.source, target: e.target })),
    [visibleEdges],
  );

  // Run dagre with full edge set so middle siblings are not orphaned
  const positioned = useMemo(
    () => dagreLayout(Object.values(visibleNodes), dagreEdges),
    [visibleNodes, dagreEdges],
  );

  // Build RF nodes with custom types
  const rfNodes = useMemo(() => {
    const hl = new Set(highlight);
    return positioned.map((n) => {
      const isFile      = n.kind === "file" || n.kind === "module";
      const isSelected  = n.id === selectedId;
      const isHighlighted = hl.has(n.id);
      const isExpanded  = !!expanded[n.id];

      return {
        id: n.id,
        type: isFile ? "fileNode" : "functionNode",
        position: n.position,
        data: {
          label:        n.label,
          filePath:     isFile ? n.file_path || n.description : null,
          description:  isFile ? null : (n.description || null),
          fnCount:      n.function_count ?? 0,
          kind:         n.type || n.kind,
          semanticKind: n.adapter_metadata?.semantic_kind || "CALLS",
          isExpanded,
          isHighlighted,
          isSelected,
          hasCallees:  isExpanded || (childrenOf[n.id]?.length ?? 0) > 0,
          raw: n,
        },
      };
    });
  }, [positioned, highlight, expanded, selectedId, childrenOf]);

  useEffect(() => {
    setNodes(rfNodes);
    setEdges(rfEdges);
  }, [rfNodes, rfEdges, setNodes, setEdges]);

  useEffect(() => {
    const count = rfNodes.length;
    if (count === 0 || count === prevCountRef.current) return;
    prevCountRef.current = count;
    const id = setTimeout(() => fitView({ padding: 0.12, duration: 350 }), 60);
    return () => clearTimeout(id);
  }, [rfNodes, fitView]);

  const isEmpty = Object.keys(visibleNodes).length === 0;

  return (
    <div className="w-full h-full relative">
      {isEmpty && <EmptyState />}
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        onNodeClick={(_, node) => {
          selectNode(node.id);
          toggleExpand(node.id);
        }}
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
          nodeColor={(n) => n.type === "fileNode" ? "#1e3a8a" : "#312e81"}
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
