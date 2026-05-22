import { create } from "zustand";

const API = "/api";

export const useFlowStore = create((set, get) => ({
  graphId: null,
  repoPath: "",

  // Visible graph state — built up incrementally via expand/collapse.
  nodes: {},                  // id -> node
  edges: {},                  // id -> edge
  expanded: {},               // node_id -> [child_id, ...]  (for collapse)
  rootIds: [],                // ids of the entry-point nodes (root level)

  selectedId: null,           // id of node showing description bubble
  highlightPath: [],
  explanation: "",
  queryId: null,
  loading: false,
  error: "",
  
  // Navigation history for breadcrumb trail
  viewHistory: [],            // stack of {nodes, edges, expanded, label}

  setRepoPath: (repoPath) => set({ repoPath }),

  ingest: async () => {
    const { repoPath } = get();
    if (!repoPath) return;
    set({ loading: true, error: "" });
    try {
      const r = await fetch(`${API}/ingest_repo`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ repo_path: repoPath }),
      });
      if (!r.ok) throw new Error(await r.text());
      const { graph_id } = await r.json();
      set({ graphId: graph_id, nodes: {}, edges: {}, expanded: {}, rootIds: [], selectedId: null });
      await get().loadInitial();
    } catch (e) {
      set({ error: String(e) });
    } finally {
      set({ loading: false });
    }
  },

  loadInitial: async () => {
    const { graphId } = get();
    if (!graphId) return;
    const r = await fetch(`${API}/entry_points?graph_id=${graphId}&max_count=4`);
    if (!r.ok) { set({ error: await r.text() }); return; }
    const g = await r.json();
    const nodes = {};
    const edges = {};
    g.nodes.forEach((n) => (nodes[n.id] = n));
    (g.edges || []).forEach((e) => {
      if (nodes[e.source] && nodes[e.target]) edges[e.id] = e;
    });
    set({
      nodes, edges,
      expanded: {},
      rootIds: g.nodes.map((n) => n.id),
      selectedId: null,
    });
  },

  selectNode: (id) => set({ selectedId: id }),
  clearSelection: () => set({ selectedId: null }),

  toggleExpand: async (nodeId, options = {}) => {
    const { graphId, expanded, nodes, edges, rootIds } = get();
    if (!graphId || !nodes[nodeId]) return;

    // Collect all node IDs in the subtree rooted at `id` (excluding `id` itself).
    const collectDescendants = (newExpanded, id, out = new Set()) => {
      for (const k of (newExpanded[id] || [])) {
        out.add(k);
        collectDescendants(newExpanded, k, out);
      }
      return out;
    };

    const removeSubtreeFrom = (newNodes, newEdges, newExpanded, id, keepSelf) => {
      const removed = collectDescendants(newExpanded, id);
      // Delete descendant nodes and their expanded entries
      for (const k of removed) {
        delete newNodes[k];
        delete newExpanded[k];
      }
      delete newExpanded[id];
      if (!keepSelf) removed.add(id), delete newNodes[id];

      // Single edge sweep over all removed nodes
      for (const eid of Object.keys(newEdges)) {
        const e = newEdges[eid];
        if (removed.has(e.source) || removed.has(e.target)) delete newEdges[eid];
      }
    };

    // Already expanded → collapse this subtree.
    if (expanded[nodeId]) {
      const newNodes = { ...nodes };
      const newEdges = { ...edges };
      const newExpanded = { ...expanded };
      removeSubtreeFrom(newNodes, newEdges, newExpanded, nodeId, true);
      set({ nodes: newNodes, edges: newEdges, expanded: newExpanded });
      return;
    }

    // Build mutable copies to modify before saving.
    let newNodes = { ...nodes };
    let newEdges = { ...edges };
    let newExpanded = { ...expanded };

    // Rule: if expanding a ROOT node, collapse every other root's subtree first
    // (but keep the root nodes themselves visible).
    const rootIdSet = new Set(rootIds);
    const isRoot = rootIdSet.has(nodeId);
    if (isRoot) {
      for (const rid of rootIds) {
        if (rid !== nodeId && newExpanded[rid]) {
          removeSubtreeFrom(newNodes, newEdges, newExpanded, rid, true);
        }
      }
    }

    // Expand: fetch children and merge.
    const action = options.action || "callees";
    
    // Save current state to history before drilling into functions (level 3)
    if (action === "functions") {
      const currentState = {
        nodes: { ...nodes },
        edges: { ...edges },
        expanded: { ...expanded },
        label: `Before drilling into ${nodes[nodeId].label}`,
      };
      set({ viewHistory: [...get().viewHistory, currentState] });
    }
    
    const url = `${API}/expand?graph_id=${graphId}&node_id=${encodeURIComponent(nodeId)}&action=${action}`;
    const r = await fetch(url);
    if (!r.ok) { set({ error: await r.text() }); return; }
    const data = await r.json();
    
    // Focus mode: When drilling into functions (depth 3), apply filtering BEFORE adding children
    if (action === "functions") {
      // Build ancestry chain for the node being expanded (using current nodes state)
      const getAncestors = (id, nodeMap) => {
        const ancestors = [id];
        let current = nodeMap[id];
        while (current && current.parent && nodeMap[current.parent]) {
          ancestors.unshift(current.parent);
          current = nodeMap[current.parent];
        }
        return ancestors;
      };
      
      const ancestryChain = new Set(getAncestors(nodeId, newNodes));
      
      // Filter existing nodes to keep only ancestry chain
      const focusedNodes = {};
      for (const nid of ancestryChain) {
        if (newNodes[nid]) {
          focusedNodes[nid] = newNodes[nid];
        }
      }
      
      // Now add the new children
      const childIds = [];
      for (const c of data.children) {
        focusedNodes[c.id] = c;
        childIds.push(c.id);
      }
      
      // Build nodesToKeep set for edge filtering
      const nodesToKeep = new Set([...ancestryChain, ...childIds]);
      
      // Filter edges to keep only those between kept nodes
      const focusedEdges = {};
      for (const e of data.edges) {
        if (nodesToKeep.has(e.source) && nodesToKeep.has(e.target)) {
          focusedEdges[e.id] = e;
        }
      }
      
      // Also keep existing edges between ancestry nodes
      for (const eid of Object.keys(newEdges)) {
        const e = newEdges[eid];
        if (nodesToKeep.has(e.source) && nodesToKeep.has(e.target)) {
          focusedEdges[eid] = e;
        }
      }
      
      // Filter expanded state
      const focusedExpanded = {};
      for (const nid of nodesToKeep) {
        if (newExpanded[nid]) {
          focusedExpanded[nid] = newExpanded[nid].filter(cid => nodesToKeep.has(cid));
        }
      }
      focusedExpanded[nodeId] = childIds;
      
      newNodes = focusedNodes;
      newEdges = focusedEdges;
      newExpanded = focusedExpanded;
    } else {
      // Normal expansion (not functions)
      const childIds = [];
      for (const c of data.children) {
        newNodes[c.id] = c;
        childIds.push(c.id);
      }
      for (const e of data.edges) {
        if (newNodes[e.source] && newNodes[e.target]) {
          newEdges[e.id] = e;
        }
      }
      newExpanded[nodeId] = childIds;
    }
    
    set({ nodes: newNodes, edges: newEdges, expanded: newExpanded });
  },
  
  goBack: () => {
    const { viewHistory } = get();
    if (viewHistory.length === 0) return;
    
    const previousState = viewHistory[viewHistory.length - 1];
    const newHistory = viewHistory.slice(0, -1);
    
    set({
      nodes: previousState.nodes,
      edges: previousState.edges,
      expanded: previousState.expanded,
      viewHistory: newHistory,
      selectedId: null,
    });
  },
  
  resetView: async () => {
    set({
      nodes: {},
      edges: {},
      expanded: {},
      viewHistory: [],
      selectedId: null,
    });
    await get().loadInitial();
  },

  runQuery: async (query) => {
    const { graphId } = get();
    if (!graphId || !query) return;
    set({ loading: true, error: "" });
    try {
      const r = await fetch(`${API}/query`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ graph_id: graphId, query, depth: 2 }),
      });
      if (!r.ok) throw new Error(await r.text());
      const data = await r.json();
      set({ explanation: data.explanation, highlightPath: data.path, queryId: data.query_id ?? null });
    } catch (e) {
      set({ error: String(e) });
    } finally {
      set({ loading: false });
    }
  },

  sendFeedback: async (rating) => {
    const { graphId, queryId } = get();
    if (!graphId || !queryId) return;
    await fetch(`${API}/feedback`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ graph_id: graphId, query_id: queryId, rating }),
    });
  },

  update: async () => {
    const { graphId } = get();
    if (!graphId) return;
    await fetch(`${API}/update`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ graph_id: graphId }),
    });
    await get().loadInitial();
  },
}));
