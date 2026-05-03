import React, { useMemo, useEffect, useRef } from "react";
import ReactFlow, {
  Background, Controls, MiniMap,
  useNodesState, useEdgesState,
  NodeToolbar, Position,
  useReactFlow,
} from "reactflow";
import { useFlowStore } from "../store.js";

// Hierarchical layout: roots in zigzag grid, children expand to the right
// Children are positioned relative to their parent's Y position
function treeLayout(rawNodes) {
  const COL_W = 300;
  const ROW_H = 100;
  const DEPTH_OFFSET = 400; // Horizontal spacing between depth levels
  const NODES_PER_ROW = 5;
  const CHILD_SPACING = 90; // Vertical spacing between children

  const byId = {};
  rawNodes.forEach((n) => (byId[n.id] = n));

  // Identify roots and build parent-child relationships
  const roots = rawNodes.filter((n) => !n.parent || !byId[n.parent]);
  const childrenOf = {};
  rawNodes.forEach((n) => {
    if (n.parent && byId[n.parent]) {
      (childrenOf[n.parent] ||= []).push(n);
    }
  });

  // Calculate depth for each node
  const depthMap = {};
  const calculateDepth = (nodeId, depth = 0) => {
    if (depthMap[nodeId] !== undefined) return;
    depthMap[nodeId] = depth;
    const kids = childrenOf[nodeId] || [];
    kids.forEach(child => calculateDepth(child.id, depth + 1));
  };
  roots.forEach(root => calculateDepth(root.id, 0));

  const positions = {};
  
  // Track next available Y position for each depth level
  const nextYByDepth = {};

  // Place roots first in zigzag pattern
  roots.forEach((root, idx) => {
    const row = Math.floor(idx / NODES_PER_ROW);
    const col = idx % NODES_PER_ROW;
    
    // Zigzag: even rows left-to-right, odd rows right-to-left
    const x = (row % 2 === 0)
      ? col * COL_W
      : (NODES_PER_ROW - 1 - col) * COL_W;
    const y = row * ROW_H;
    
    positions[root.id] = { x, y, depth: 0 };
  });

  // Place children relative to their parent's position
  const placeChildren = (parentId) => {
    const kids = childrenOf[parentId] || [];
    if (kids.length === 0) return;

    const parentPos = positions[parentId];
    if (!parentPos) return;

    const parentDepth = depthMap[parentId];
    const childDepth = parentDepth + 1;
    const childX = childDepth * DEPTH_OFFSET;

    // Get or initialize next Y for this depth
    if (nextYByDepth[childDepth] === undefined) {
      nextYByDepth[childDepth] = 0;
    }

    // Center children around parent's Y position
    const totalHeight = (kids.length - 1) * CHILD_SPACING;
    let startY = parentPos.y - totalHeight / 2;

    // Ensure we don't overlap with previous nodes at this depth
    if (startY < nextYByDepth[childDepth]) {
      startY = nextYByDepth[childDepth];
    }

    kids.forEach((child, idx) => {
      const y = startY + idx * CHILD_SPACING;
      positions[child.id] = { x: childX, y, depth: childDepth };
      
      // Update next available Y for this depth
      nextYByDepth[childDepth] = Math.max(nextYByDepth[childDepth], y + CHILD_SPACING);
      
      // Recursively place this child's children
      placeChildren(child.id);
    });
  };

  roots.forEach(root => placeChildren(root.id));

  return rawNodes.map((n) => ({
    ...n,
    position: positions[n.id] || { x: 0, y: 0 },
    depth: depthMap[n.id] || 0
  }));
}

// Depth-based color schemes for visual hierarchy
const DEPTH_COLORS = [
  // Depth 0 (roots/modules)
  { bg: "#1e3a8a", border: "#3b82f6", fg: "#f8fafc" },
  // Depth 1 (files/submodules)
  { bg: "#065f46", border: "#10b981", fg: "#f0fdf4" },
  // Depth 2 (functions)
  { bg: "#7c2d12", border: "#f97316", fg: "#fff7ed" },
  // Depth 3+ (methods/nested)
  { bg: "#4c1d95", border: "#a78bfa", fg: "#faf5ff" },
];

const KIND_STYLE = {
  file:      { bg: "#1e3a8a", border: "#3b82f6", fg: "#f8fafc" },
  module:    { bg: "#1e3a8a", border: "#3b82f6", fg: "#f8fafc" },
  submodule: { bg: "#0f172a", border: "#475569", fg: "#e2e8f0" },
  function:  { bg: "#0b1220", border: "#334155", fg: "#cbd5e1" },
  method:    { bg: "#0b1220", border: "#334155", fg: "#cbd5e1" },
  class:     { bg: "#172554", border: "#3b82f6", fg: "#dbeafe" },
};

function getDepthStyle(depth) {
  return DEPTH_COLORS[Math.min(depth, DEPTH_COLORS.length - 1)];
}

function GraphViewInner() {
  const visibleNodes = useFlowStore((s) => s.nodes);
  const visibleEdges = useFlowStore((s) => s.edges);
  const expanded = useFlowStore((s) => s.expanded);
  const selectedId = useFlowStore((s) => s.selectedId);
  const highlight = useFlowStore((s) => s.highlightPath);
  const toggleExpand = useFlowStore((s) => s.toggleExpand);
  const selectNode = useFlowStore((s) => s.selectNode);
  const clearSelection = useFlowStore((s) => s.clearSelection);

  const [nodes, setNodes, onNodesChange] = useNodesState([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState([]);
  const { fitView } = useReactFlow();
  const prevNodeCountRef = useRef(0);

  const positioned = useMemo(() => treeLayout(Object.values(visibleNodes)), [visibleNodes]);

  const rfNodes = useMemo(() => {
    const hl = new Set(highlight);
    return positioned.map((n) => {
      // Use depth-based colors, fallback to kind-based
      const depthStyle = getDepthStyle(n.depth || 0);
      const kindStyle = KIND_STYLE[n.kind] || KIND_STYLE.function;
      const style = n.depth !== undefined ? depthStyle : kindStyle;
      
      const isHighlighted = hl.has(n.id);
      const isExpanded = !!expanded[n.id];
      return {
        id: n.id,
        data: { label: n.label, raw: n },
        position: n.position,
        style: {
          padding: "12px 16px",
          borderRadius: 10,
          border: `${isHighlighted ? 3 : 2}px solid ${isHighlighted ? "#f59e0b" : style.border}`,
          background: style.bg,
          color: style.fg,
          fontSize: (n.kind === "module" || n.kind === "file") ? 16 : 13,
          fontWeight: (n.kind === "module" || n.kind === "file") ? 700 : 500,
          minWidth: (n.kind === "module" || n.kind === "file") ? 200 : 150,
          maxWidth: 280,
          textAlign: "center",
          cursor: "pointer",
          boxShadow: isExpanded ? "0 0 0 2px rgba(59,130,246,0.45)" : "none",
        },
      };
    });
  }, [positioned, highlight, expanded]);

  const rfEdges = useMemo(() => {
    const nodeIds = new Set(Object.keys(visibleNodes));
    
    // Build parent-child map to identify first/last children
    const childrenOf = {};
    Object.values(visibleNodes).forEach((n) => {
      if (n.parent && nodeIds.has(n.parent)) {
        (childrenOf[n.parent] ||= []).push(n.id);
      }
    });
    
    return Object.values(visibleEdges).map((e) => {
      const isContains = e.kind === "CONTAINS";
      const isFlow = e.kind === "FLOW";
      const isCalls = e.kind === "CALLS";
      
      // For CONTAINS edges: only show connection to first and last child
      let shouldShow = true;
      if (isContains) {
        const children = childrenOf[e.source] || [];
        if (children.length > 2) {
          const firstChild = children[0];
          const lastChild = children[children.length - 1];
          // Only show edge if target is first or last child
          shouldShow = (e.target === firstChild || e.target === lastChild);
        }
      }
      
      if (!shouldShow) return null;
      
      return {
        id: e.id,
        source: e.source, target: e.target,
        animated: isFlow,
        type: "smoothstep",
        style: {
          stroke: isContains ? "#475569" : isFlow ? "#0ea5e9" : "#94a3b8",
          strokeDasharray: isContains ? "4 4" : undefined,
          strokeWidth: isFlow ? 2 : isCalls ? 1.4 : 1,
        },
      };
    }).filter(Boolean); // Remove null edges
  }, [visibleEdges, visibleNodes]);

  useEffect(() => { setNodes(rfNodes); }, [rfNodes, setNodes]);
  useEffect(() => { setEdges(rfEdges); }, [rfEdges, setEdges]);
  
  // Auto-fit view when nodes change (expansion/collapse)
  useEffect(() => {
    const currentNodeCount = rfNodes.length;
    if (currentNodeCount > 0 && currentNodeCount !== prevNodeCountRef.current) {
      // Delay fitView slightly to ensure layout is complete
      setTimeout(() => {
        fitView({ padding: 0.1, duration: 400 });
      }, 50);
      prevNodeCountRef.current = currentNodeCount;
    }
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
              {selectedNode.kind === "file" && (
                <button
                  onClick={(e) => {
                    e.stopPropagation();
                    if (expanded[selectedNode.id]) toggleExpand(selectedNode.id); // collapse first
                    toggleExpand(selectedNode.id, { action: "functions" });
                  }}
                  className="mt-2 text-xs bg-slate-800 hover:bg-slate-700 border border-slate-700 rounded px-2 py-1 w-full"
                >
                  Drill into functions
                </button>
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
