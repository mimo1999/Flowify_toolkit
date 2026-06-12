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
  conversationId: null, // persisted across queries in the same session
  queryNodes: [],     // relevant nodes from the last query (for results panel)
  executionSteps: [], // structured execution path from last query
  graphNodesConsulted: 0,
  loading: false,
  error: "",
  exportStatus: null, // "copied" | "downloading" | null — transient feedback

  // Impact analysis panel
  impactData: null,
  impactLoading: false,

  // Navigation history
  viewHistory: [],    // stack of {nodes, edges, expanded, label}

  // Current drill-down depth (1=modules, 2=files, 3=functions)
  currentDepth: null, // null = lazy expand mode, 1/2/3 = depth view

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
        executionSteps: [], graphNodesConsulted: 0, conversationId: null,
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

  selectNode: (id) => {
    set({ selectedId: id });
    // Auto-fetch impact when a function node is selected
    if (id && !id.startsWith("file::") && !id.startsWith("mod_")) {
      get().fetchImpact(id);
    } else {
      set({ impactData: null });
    }
  },
  clearSelection: () => set({ selectedId: null, impactData: null }),
  clearQuery: () => set({
    explanation: "", queryNodes: [], highlightPath: [],
    queryId: null, conversationId: null,
    executionSteps: [], graphNodesConsulted: 0,
  }),

  fetchImpact: async (nodeId) => {
    const { graphId } = get();
    if (!graphId || !nodeId) return;
    set({ impactLoading: true });
    try {
      const r = await fetch(`${API}/impact?graph_id=${graphId}&node_id=${encodeURIComponent(nodeId)}`);
      if (!r.ok) { set({ impactData: null }); return; }
      set({ impactData: await r.json() });
    } catch (_) {
      set({ impactData: null });
    } finally {
      set({ impactLoading: false });
    }
  },

  // Load graph at a fixed depth (1=modules, 2=files, 3=functions)
  loadDepthView: async (depth) => {
    const { graphId } = get();
    if (!graphId) return;
    set({ loading: true, error: "", currentDepth: depth });
    try {
      const r = await fetch(`${API}/graph?graph_id=${graphId}&depth=${depth}`);
      if (!r.ok) { set({ error: await r.text() }); return; }
      const data = await r.json();
      const nodes = {};
      const edges = {};
      (data.nodes || []).forEach((n) => (nodes[n.id] = n));
      (data.edges || []).forEach((e) => {
        if (nodes[e.source] && nodes[e.target]) edges[e.id] = e;
      });
      set({ nodes, edges, expanded: {}, rootIds: Object.keys(nodes), selectedId: null, viewHistory: [] });
    } catch (e) {
      set({ error: String(e) });
    } finally {
      set({ loading: false });
    }
  },

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
    const { graphId, conversationId } = get();
    if (!graphId || !query) return;
    set({ loading: true, error: "" });
    try {
      const body = { graph_id: graphId, query, depth: 2 };
      if (conversationId) body.conversation_id = conversationId;
      const r = await fetch(`${API}/query`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!r.ok) throw new Error(await r.text());
      const data = await r.json();
      set({
        explanation: data.explanation,
        highlightPath: data.path,
        queryId: data.query_id ?? null,
        conversationId: data.conversation_id ?? null,
        queryNodes: data.subgraph?.nodes ?? [],
        executionSteps: data.execution_steps ?? [],
        graphNodesConsulted: data.graph_nodes_consulted ?? 0,
      });
    } catch (e) {
      set({ error: String(e) });
    } finally {
      set({ loading: false });
    }
  },

  exportGraph: async (format) => {
    const { graphId } = get();
    if (!graphId) return;
    if (format === "mermaid") {
      set({ exportStatus: "downloading" });
      try {
        const r = await fetch(`${API}/export/${graphId}?format=mermaid`);
        if (!r.ok) throw new Error(await r.text());
        const text = await r.text();
        await navigator.clipboard.writeText(text);
        set({ exportStatus: "copied" });
        setTimeout(() => set({ exportStatus: null }), 2500);
      } catch (e) {
        set({ error: String(e), exportStatus: null });
      }
    } else if (format === "json") {
      set({ exportStatus: "downloading" });
      try {
        const r = await fetch(`${API}/export/${graphId}?format=json`);
        if (!r.ok) throw new Error(await r.text());
        const blob = await r.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = `graph-${graphId}.json`;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
        set({ exportStatus: null });
      } catch (e) {
        set({ error: String(e), exportStatus: null });
      }
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
