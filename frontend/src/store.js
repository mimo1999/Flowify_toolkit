import { create } from "zustand";

const API = "/api";

export const useFlowStore = create((set, get) => ({
  graphId: null,
  repoPath: "",

  // Visible graph state — built up incrementally via expand/collapse.
  nodes: {},          // id -> node
  edges: {},          // id -> edge
  expanded: {},       // node_id -> [child_id, ...]  (for collapse)
  rootIds: [],        // ids of the entry-point nodes (root level)

  selectedId: null,
  highlightPath: [],
  explanation: "",
  queryId: null,
  queryNodes: [],     // relevant nodes from the last query (for results panel)
  loading: false,
  error: "",

  // Navigation history
  viewHistory: [],    // stack of {nodes, edges, expanded, label}

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
      set({
        graphId: graph_id, nodes: {}, edges: {}, expanded: {},
        rootIds: [], selectedId: null, explanation: "", queryNodes: [],
      });
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
    set({ nodes, edges, expanded: {}, rootIds: g.nodes.map((n) => n.id), selectedId: null });
  },

  selectNode: (id) => set({ selectedId: id }),
  clearSelection: () => set({ selectedId: null }),
  clearQuery: () => set({ explanation: "", queryNodes: [], highlightPath: [], queryId: null }),

  toggleExpand: async (nodeId) => {
    const { graphId, expanded, nodes, edges, rootIds } = get();
    if (!graphId || !nodes[nodeId]) return;

    // ── helpers ──────────────────────────────────────────────────────────────
    const collectDescendants = (exp, id, out = new Set()) => {
      for (const k of (exp[id] || [])) { out.add(k); collectDescendants(exp, k, out); }
      return out;
    };

    const removeSubtree = (nn, ne, nexp, id, keepSelf) => {
      const removed = collectDescendants(nexp, id);
      for (const k of removed) { delete nn[k]; delete nexp[k]; }
      delete nexp[id];
      if (!keepSelf) { removed.add(id); delete nn[id]; }
      for (const eid of Object.keys(ne)) {
        const e = ne[eid];
        if (removed.has(e.source) || removed.has(e.target)) delete ne[eid];
      }
    };

    // ── collapse ──────────────────────────────────────────────────────────────
    if (expanded[nodeId]) {
      const nn = { ...nodes }, ne = { ...edges }, nexp = { ...expanded };
      removeSubtree(nn, ne, nexp, nodeId, true);
      set({ nodes: nn, edges: ne, expanded: nexp });
      return;
    }

    // ── expand ────────────────────────────────────────────────────────────────
    let nn = { ...nodes }, ne = { ...edges }, nexp = { ...expanded };

    // Collapse siblings when expanding a root node
    if (new Set(rootIds).has(nodeId)) {
      for (const rid of rootIds) {
        if (rid !== nodeId && nexp[rid]) removeSubtree(nn, ne, nexp, rid, true);
      }
    }

    // Choose action:
    //   file node  → show its functions directly (2-level hierarchy)
    //   everything else → show its callees (function → called functions)
    const node = nodes[nodeId];
    const action = node.kind === "file" ? "functions" : "callees";

    // Save view state to history when going into a file's functions
    if (action === "functions") {
      set({ viewHistory: [...get().viewHistory, {
        nodes: { ...nodes }, edges: { ...edges }, expanded: { ...expanded },
        label: `Before ${node.label}`,
      }]});
    }

    const url = `${API}/expand?graph_id=${graphId}&node_id=${encodeURIComponent(nodeId)}&action=${action}`;
    const r = await fetch(url);
    if (!r.ok) { set({ error: await r.text() }); return; }
    const data = await r.json();

    const childIds = [];
    for (const c of data.children) {
      if (!nn[c.id]) nn[c.id] = c;  // skip-if-exists: preserve existing node's parent
      childIds.push(c.id);
    }
    for (const e of data.edges) {
      if (nn[e.source] && nn[e.target]) ne[e.id] = e;
    }
    nexp[nodeId] = childIds;

    set({ nodes: nn, edges: ne, expanded: nexp });
  },

  goBack: () => {
    const { viewHistory } = get();
    if (viewHistory.length === 0) return;
    const prev = viewHistory[viewHistory.length - 1];
    set({
      nodes: prev.nodes, edges: prev.edges, expanded: prev.expanded,
      viewHistory: viewHistory.slice(0, -1), selectedId: null,
    });
  },

  resetView: async () => {
    set({ nodes: {}, edges: {}, expanded: {}, viewHistory: [], selectedId: null });
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
      set({
        explanation: data.explanation,
        highlightPath: data.path,
        queryId: data.query_id ?? null,
        queryNodes: data.subgraph?.nodes ?? [],
      });
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
