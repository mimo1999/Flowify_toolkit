import React, { useMemo, useEffect, useRef } from "react";
import ReactFlow, {
  Background, Controls, MiniMap,
  useNodesState, useEdgesState,
  NodeToolbar, Position,
  useReactFlow,
} from "reactflow";
import dagre from "@dagrejs/dagre";
import { useFlowStore } from "../store.js";

// ---------------------------------------------------------------------------
// Dagre-based left-to-right layout.
// Replaces the hand-rolled treeLayout which caused overlap on large graphs.
// ---------------------------------------------------------------------------
function dagreLayout(rawNodes, edgeList) {
  const g = new dagre.graphlib.Graph();
  g.setDefaultEdgeLabel(() => ({}));
  g.setGraph({
    rankdir: "LR",   // left-to-right hierarchy
    nodesep: 55,     // vertical gap between sibling nodes
    ranksep: 140,    // horizontal gap between depth levels
    marginx: 40,
    marginy: 40,
  });

  rawNodes.forEach((n) => {
    const isLarge = n.kind === "module" || n.kind === "file";
    g.setNode(n.id, { width: isLarge ? 220 : 170, height: 50 });
  });

  edgeList.forEach((e) => {
    if (g.hasNode(e.source) && g.hasNode(e.target)) {
      g.setEdge(e.source, e.target);
    }
  });

  dagre.layout(g);

  // Depth from parent relationship (drives colour; independent of dagre rank)
  const byId = {};
  rawNodes.forEach((n) => (byId[n.id] = n));
  const depthMap = {};
  const calcDepth = (id, d) => {
    if (depthMap[id] !== undefined) return;
    depthMap[id] = d;
    rawNodes.filter((n) => n.parent === id).forEach((c) => calcDepth(c.id, d + 1));
  };
  rawNodes
    .filter((n) => !n.parent || !byId[n.parent])
    .forEach((r) => calcDepth(r.id, 0));

  return rawNodes.map((n) => {
    const pos = g.node(n.id);
    return {
      ...n,
      position: pos
        ? { x: pos.x - pos.width / 2, y: pos.y - pos.height / 2 }
        : { x: 0, y: 0 },
      depth: depthMap[n.id] ?? 0,
    };
  });
}

// ---------------------------------------------------------------------------
// Depth-based colour schemes
// ---------------------------------------------------------------------------
const DEPTH_COLORS = [
  { bg: "#1e3a8a", border: "#3b82f6", fg: "#f8fafc" },  // depth 0 — roots/files
  { bg: "#065f46", border: "#10b981", fg: "#f0fdf4" },  // depth 1 — callees
  { bg: "#7c2d12", border: "#f97316", fg: "#fff7ed" },  // depth 2 — functions
  { bg: "#4c1d95", border: "#a78bfa", fg: "#faf5ff" },  // depth 3+
];

function getDepthStyle(depth) {
  return DEPTH_COLORS[Math.min(depth, DEPTH_COLORS.length - 1)];
}

// ---------------------------------------------------------------------------
// Main graph component
// ---------------------------------------------------------------------------
function GraphViewInner() {
  const visibleNodes  = useFlowStore((s) => s.nodes);
  const visibleEdges  = useFlowStore((s) => s.edges);
  const expanded      = useFlowStore((s) => s.expanded);
  const selectedId    = useFlowStore((s) => s.selectedId);
  const highlight     = useFlowStore((s) => s.highlightPath);
  const toggleExpand  = useFlowStore((s) => s.toggleExpand);
  const selectNode    = useFlowStore((s) => s.selectNode);
  const clearSelection = useFlowStore((s) => s.clearSelection);

  const [nodes, setNodes, onNodesChange] = useNodesState([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState([]);
  const { fitView } = useReactFlow();
  const prevNodeCountRef = useRef(0);

  // Memoise parent→children map for CONTAINS edge pruning
  const childrenOf = useMemo(() => {
    const map = {};
    const nodeIds = new Set(Object.keys(visibleNodes));
    Object.values(visibleNodes).forEach((n) => {
      if (n.parent && nodeIds.has(n.parent)) (map[n.parent] ||= []).push(n.id);
    });
    return map;
  }, [visibleNodes]);

  // Build edge list for dagre (and for RF display)
  const rfEdges = useMemo(() => {
    return Object.values(visibleEdges)
      .map((e) => {
        const isContains = e.kind === "CONTAINS";
        const isFlow     = e.kind === "FLOW";
        const isCalls    = e.kind === "CALLS";

        // Prune CONTAINS fan-out: only draw to first and last child
        if (isContains) {
          const siblings = childrenOf[e.source] || [];
          if (siblings.length > 2) {
            const first = siblings[0];
            const last  = siblings[siblings.length - 1];
            if (e.target !== first && e.target !== last) return null;
          }
        }

        return {
          id: e.id,
          source: e.source,
          target: e.target,
          animated: isFlow,
          type: "smoothstep",
          style: {
            stroke:           isContains ? "#475569" : isFlow ? "#0ea5e9" : "#94a3b8",
            strokeDasharray:  isContains ? "4 4" : undefined,
            strokeWidth:      isFlow ? 2 : isCalls ? 1.4 : 1,
          },
        };
      })
      .filter(Boolean);
  }, [visibleEdges, childrenOf]);

  // Run dagre on every node/edge change
  const positioned = useMemo(
    () => dagreLayout(Object.values(visibleNodes), rfEdges),
    [visibleNodes, rfEdges],
  );

  const rfNodes = useMemo(() => {
    const hl = new Set(highlight);
    return positioned.map((n) => {
      const style       = getDepthStyle(n.depth);
      const isHighlighted = hl.has(n.id);
      const isExpanded  = !!expanded[n.id];
      const isLarge     = n.kind === "module" || n.kind === "file";
      return {
        id: n.id,
        data: { label: n.label, raw: n },
        position: n.position,
        style: {
          padding:      "12px 16px",
          borderRadius: 10,
          border:       `${isHighlighted ? 3 : 2}px solid ${isHighlighted ? "#f59e0b" : style.border}`,
          background:   style.bg,
          color:        style.fg,
          fontSize:     isLarge ? 16 : 13,
          fontWeight:   isLarge ? 700 : 500,
          minWidth:     isLarge ? 200 : 150,
          maxWidth:     280,
          textAlign:    "center",
          cursor:       "pointer",
          boxShadow:    isExpanded ? "0 0 0 2px rgba(59,130,246,0.45)" : "none",
        },
      };
    });
  }, [positioned, highlight, expanded]);

  // Sync RF state in one batch
  useEffect(() => {
    setNodes(rfNodes);
    setEdges(rfEdges);
  }, [rfNodes, rfEdges, setNodes, setEdges]);

  // Auto-fit when node count changes
  useEffect(() => {
    const count = rfNodes.length;
    if (count === 0 || count === prevNodeCountRef.current) return;
    prevNodeCountRef.current = count;
    const id = setTimeout(() => fitView({ padding: 0.15, duration: 400 }), 50);
    return () => clearTimeout(id);
  }, [rfNodes, fitView]);

  const selectedNode = selectedId ? visibleNodes[selectedId] : null;

  return (
    <div className="w-full h-full relative">
      <ReactFlow
        nodes={nodes}
        edges={edges}
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
      >
        <Background gap={28} color="#1e293b" />
        <Controls className="!bg-slate-900 !border-slate-700" />
        <MiniMap pannable zoomable maskColor="rgba(2,6,23,0.85)" />

        {selectedNode && (
          <NodeToolbar
            nodeId={selectedNode.id}
            isVisible
            position={Position.Right}
            offset={12}
          >
            <div className="bg-slate-900 border border-slate-700 rounded-lg shadow-xl p-3 max-w-xs text-slate-100">
              <div className="flex items-start justify-between gap-3">
                <div>
                  <div className="text-xs uppercase tracking-wide text-slate-400">{selectedNode.kind}</div>
                  <div className="font-semibold mt-0.5">{selectedNode.label}</div>
                </div>
                <button
                  onClick={(e) => { e.stopPropagation(); clearSelection(); }}
                  className="text-slate-500 hover:text-slate-200 text-sm leading-none"
                  aria-label="close"
                >×</button>
              </div>
              {selectedNode.description && (
                <p className="text-xs text-slate-300 mt-2 whitespace-pre-wrap">
                  {selectedNode.description}
                </p>
              )}
              {selectedNode.file_path && (
                <div className="text-[11px] text-slate-500 mt-2 break-all">{selectedNode.file_path}</div>
              )}
              {selectedNode.function_count != null && (
                <div className="text-[11px] text-slate-500 mt-1">
                  {selectedNode.function_count} symbols · click to {expanded[selectedNode.id] ? "collapse" : "expand"}
                </div>
              )}
            </div>
          </NodeToolbar>
        )}
      </ReactFlow>
    </div>
  );
}

export default function GraphView() {
  return <GraphViewInner />;
}
