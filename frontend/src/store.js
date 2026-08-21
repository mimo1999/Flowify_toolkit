import { create } from "zustand";

// In dev, Vite's own proxy used to rewrite /api/* to the backend (see
// vite.config.js) — the backend now serves /api/* itself, in every mode, so
// the same path works against a same-origin backend (Docker/HF Spaces) and,
// via VITE_API_BASE, against a backend on a different origin (e.g. a
// Vercel-hosted frontend pointed at a Hugging Face Space).
export const API = import.meta.env.VITE_API_BASE ?? "/api";

// Anonymous per-browser session id. Server mode scopes ingested graphs to
// this id (isolation, not authentication — see backend/app/main.py's
// _check_owned) so one visitor can't casually read another visitor's
// ingested code via /export, /graph, /query, etc. Local mode ignores the
// header entirely, so this is a no-op cost there.
export function getSessionId() {
  let id = localStorage.getItem("flowify_session");
  if (!id) {
    id = crypto.randomUUID();
    localStorage.setItem("flowify_session", id);
  }
  return id;
}

// Thin fetch wrapper: every backend call goes through this so the session
// header (and, later, a BYO LLM-provider header) is attached exactly once
// rather than re-typed at each of the ~15 call sites below.
export function apiFetch(url, options = {}) {
  return fetch(url, {
    ...options,
    headers: { ...(options.headers || {}), "X-Flowify-Session": getSessionId() },
  });
}

// Shared by toggleExpand and deepExpand — walk an `expanded` map to find every
// descendant of `id`, then strip them (and optionally `id` itself) out of the
// node/edge maps in place.
function collectDescendants(exp, id, out = new Set()) {
  for (const k of (exp[id] || [])) { out.add(k); collectDescendants(exp, k, out); }
  return out;
}

function removeSubtree(nn, ne, nexp, id, keepSelf) {
  const removed = collectDescendants(nexp, id);
  for (const k of removed) { delete nn[k]; delete nexp[k]; }
  delete nexp[id];
  if (!keepSelf) { removed.add(id); delete nn[id]; }
  for (const eid of Object.keys(ne)) {
    const e = ne[eid];
    if (removed.has(e.source) || removed.has(e.target)) delete ne[eid];
  }
}

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
  serverStopped: false, // true once the user shuts the servers down

  // Repository flow summary (generated at ingest, served by /flow_summary)
  flowSummary: null,
  flowSummaryLoading: false,
  flowSummaryError: "",
  summaryOpen: false,

  // Impact analysis panel
  impactData: null,
  impactLoading: false,

  // Knowledge layer: docs + rationale referencing the selected node
  nodeRefs: null,
  nodeRefsLoading: false,

  // Graph insights (analytics) modal
  insights: null,
  insightsLoading: false,
  insightsError: "",
  insightsOpen: false,

  // Per-node heatmap metrics (pagerank/degree/complexity), extracted from the
  // same analytics payload as `insights` — keyed by node id, used to encode
  // importance/complexity directly on the interactive graph's nodes.
  nodeMetrics: {},
  // Same heatmap signal aggregated to file level (pagerank summed, degree
  // summed, complexity/criticality = the file's worst offender) — keyed by
  // file_path, since file nodes aren't in the call graph's node_metrics.
  fileMetrics: {},

  // Navigation history — one undo stack shared by every action that changes
  // what's on screen (expand, collapse, depth-view switch, search, reset), so
  // Back always works the same way regardless of how you got here.
  viewHistory: [],    // stack of {nodes, edges, expanded, rootIds, currentDepth}

  // Current drill-down depth (1=modules, 2=files, 3=functions)
  currentDepth: null, // null = lazy expand mode, 1/2/3 = depth view

  setRepoPath: (repoPath) => set({ repoPath }),

  // Snapshot the current view onto the undo stack before a navigation action
  // changes it. Safe without cloning: every action below builds a fresh
  // object (`{...nodes}`) rather than mutating the current one in place, so
  // the reference captured here is never touched after this point. Capped so
  // a very long session doesn't grow the stack unbounded.
  _pushHistory: () => {
    const { nodes, edges, expanded, rootIds, currentDepth, viewHistory } = get();
    const next = [...viewHistory, { nodes, edges, expanded, rootIds, currentDepth }];
    set({ viewHistory: next.length > 50 ? next.slice(-50) : next });
  },

  ingest: async () => {
    const { repoPath } = get();
    if (!repoPath) return;
    set({ loading: true, error: "" });
    try {
      const trimmed = repoPath.trim();
      const isUrl = /^https?:\/\//i.test(trimmed);
      const r = await apiFetch(`${API}/ingest_repo`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(isUrl ? { repo_url: trimmed } : { repo_path: trimmed }),
      });
      if (!r.ok) throw new Error(await r.text());
      const { graph_id } = await r.json();
      set({
        graphId: graph_id, nodes: {}, edges: {}, expanded: {},
        rootIds: [], selectedId: null, explanation: "", queryNodes: [],
        executionSteps: [], graphNodesConsulted: 0, conversationId: null,
        flowSummary: null, flowSummaryError: "",
        insights: null, insightsError: "", nodeRefs: null, nodeMetrics: {}, fileMetrics: {},
        // A fresh ingest starts a new session — old history would otherwise
        // point Back at a different repository's graph.
        viewHistory: [], currentDepth: null,
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
    const r = await apiFetch(`${API}/entry_points?graph_id=${graphId}&max_count=4`);
    if (!r.ok) { set({ error: await r.text() }); return; }
    const g = await r.json();
    const nodes = {};
    const edges = {};
    g.nodes.forEach((n) => (nodes[n.id] = n));
    (g.edges || []).forEach((e) => {
      if (nodes[e.source] && nodes[e.target]) edges[e.id] = e;
    });
    set({ nodes, edges, expanded: {}, rootIds: g.nodes.map((n) => n.id), selectedId: null });
    // Fire-and-forget: populate heatmap metrics (pagerank/degree/complexity)
    // for the interactive graph without blocking the initial view from showing.
    if (!get().nodeMetrics || Object.keys(get().nodeMetrics).length === 0) {
      get().fetchInsights();
    }
  },

  selectNode: (id) => {
    set({ selectedId: id });
    // Auto-fetch impact + doc references when a function node is selected
    if (id && !id.startsWith("file::") && !id.startsWith("mod_")) {
      get().fetchImpact(id);
      get().fetchNodeRefs(id);
    } else {
      set({ impactData: null, nodeRefs: null });
    }
  },
  clearSelection: () => set({ selectedId: null, impactData: null, nodeRefs: null }),
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
      const r = await apiFetch(`${API}/impact?graph_id=${graphId}&node_id=${encodeURIComponent(nodeId)}`);
      if (!r.ok) { set({ impactData: null }); return; }
      set({ impactData: await r.json() });
    } catch (_) {
      set({ impactData: null });
    } finally {
      set({ impactLoading: false });
    }
  },

  fetchNodeRefs: async (nodeId) => {
    const { graphId } = get();
    if (!graphId || !nodeId) return;
    set({ nodeRefsLoading: true });
    try {
      const r = await apiFetch(`${API}/node_references?graph_id=${graphId}&node_id=${encodeURIComponent(nodeId)}`);
      if (!r.ok) { set({ nodeRefs: null }); return; }
      set({ nodeRefs: await r.json() });
    } catch (_) {
      set({ nodeRefs: null });
    } finally {
      set({ nodeRefsLoading: false });
    }
  },

  openInsights: () => {
    set({ insightsOpen: true });
    if (!get().insights) get().fetchInsights();
  },
  closeInsights: () => set({ insightsOpen: false }),

  fetchInsights: async () => {
    const { graphId } = get();
    if (!graphId) return;
    set({ insightsLoading: true, insightsError: "" });
    try {
      const r = await apiFetch(`${API}/graph_analytics/${graphId}`);
      if (!r.ok) throw new Error(await r.text());
      const data = await r.json();
      set({ insights: data, nodeMetrics: data.node_metrics || {}, fileMetrics: data.file_metrics || {} });
    } catch (e) {
      set({ insightsError: String(e) });
    } finally {
      set({ insightsLoading: false });
    }
  },

  downloadReport: async () => {
    const { graphId } = get();
    if (!graphId) return;
    set({ exportStatus: "downloading" });
    try {
      const r = await apiFetch(`${API}/architecture_report/${graphId}`);
      if (!r.ok) throw new Error(await r.text());
      const blob = await r.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `GRAPH_REPORT-${graphId}.md`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
      set({ exportStatus: null });
    } catch (e) {
      set({ error: String(e), exportStatus: null });
    }
  },

  // Load graph at a fixed depth (1=modules, 2=files, 3=functions) — a
  // whole-repo overview, distinct from incrementally expanding one node at a
  // time. Still goes through the same undo stack as every other navigation
  // action, so Back reliably returns to whatever was on screen before.
  loadDepthView: async (depth) => {
    const { graphId } = get();
    if (!graphId) return;
    set({ loading: true, error: "" });
    try {
      const r = await apiFetch(`${API}/graph?graph_id=${graphId}&depth=${depth}`);
      if (!r.ok) { set({ error: await r.text() }); return; }
      const data = await r.json();
      const nodes = {};
      const edges = {};
      (data.nodes || []).forEach((n) => (nodes[n.id] = n));
      (data.edges || []).forEach((e) => {
        if (nodes[e.source] && nodes[e.target]) edges[e.id] = e;
      });
      get()._pushHistory();
      set({ nodes, edges, expanded: {}, rootIds: Object.keys(nodes), selectedId: null, currentDepth: depth });
    } catch (e) {
      set({ error: String(e) });
    } finally {
      set({ loading: false });
    }
  },

  toggleExpand: async (nodeId) => {
    const { graphId, expanded, nodes, edges, rootIds } = get();
    if (!graphId || !nodes[nodeId]) return;

    // ── collapse ──────────────────────────────────────────────────────────────
    if (expanded[nodeId]) {
      const nn = { ...nodes }, ne = { ...edges }, nexp = { ...expanded };
      removeSubtree(nn, ne, nexp, nodeId, true);
      get()._pushHistory();
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

    const url = `${API}/expand?graph_id=${graphId}&node_id=${encodeURIComponent(nodeId)}&action=${action}`;
    const r = await apiFetch(url);
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

    get()._pushHistory();
    set({ nodes: nn, edges: ne, expanded: nexp });
  },

  // Shift-click on a chevron: expand several hops at once instead of one,
  // capped so a dense node can't explode the canvas in a single click. One
  // history entry covers the whole operation, so Back undoes it in one step.
  deepExpand: async (nodeId, maxHops = 3, maxNewNodes = 60) => {
    const { graphId, nodes, edges, expanded, rootIds } = get();
    if (!graphId || !nodes[nodeId]) return;

    let nn = { ...nodes }, ne = { ...edges }, nexp = { ...expanded };

    if (new Set(rootIds).has(nodeId)) {
      for (const rid of rootIds) {
        if (rid !== nodeId && nexp[rid]) removeSubtree(nn, ne, nexp, rid, true);
      }
    }

    let frontier = [nodeId];
    const visitedThisOp = new Set();
    let added = 0;

    for (let hop = 0; hop < maxHops && frontier.length && added < maxNewNodes; hop++) {
      const nextFrontier = [];
      for (const id of frontier) {
        if (visitedThisOp.has(id) || added >= maxNewNodes) continue;
        visitedThisOp.add(id);

        if (nexp[id]) { nextFrontier.push(...nexp[id]); continue; }
        const node = nn[id];
        if (!node) continue;

        const action = node.kind === "file" ? "functions" : "callees";
        const url = `${API}/expand?graph_id=${graphId}&node_id=${encodeURIComponent(id)}&action=${action}`;
        let data;
        try {
          const r = await apiFetch(url);
          if (!r.ok) continue;
          data = await r.json();
        } catch (_) {
          continue;
        }

        const childIds = [];
        for (const c of data.children) {
          if (!nn[c.id]) { nn[c.id] = c; added++; }
          childIds.push(c.id);
        }
        for (const e of data.edges) {
          if (nn[e.source] && nn[e.target]) ne[e.id] = e;
        }
        nexp[id] = childIds;
        nextFrontier.push(...childIds);
      }
      frontier = nextFrontier;
    }

    get()._pushHistory();
    set({ nodes: nn, edges: ne, expanded: nexp });
  },

  // Undo the last navigation action — expand, collapse, depth-view switch,
  // search, or reset — and land exactly where you were before it.
  goBack: () => {
    const { viewHistory } = get();
    if (viewHistory.length === 0) return;
    const prev = viewHistory[viewHistory.length - 1];
    set({
      nodes: prev.nodes, edges: prev.edges, expanded: prev.expanded,
      rootIds: prev.rootIds, currentDepth: prev.currentDepth,
      viewHistory: viewHistory.slice(0, -1), selectedId: null,
    });
  },

  resetView: async () => {
    get()._pushHistory();
    set({ nodes: {}, edges: {}, expanded: {}, selectedId: null, currentDepth: null });
    await get().loadInitial();
  },

  runQuery: async (query) => {
    const { graphId, conversationId, nodes, edges } = get();
    if (!graphId || !query) return;
    set({ loading: true, error: "" });
    try {
      const body = { graph_id: graphId, query, depth: 2 };
      if (conversationId) body.conversation_id = conversationId;
      const r = await apiFetch(`${API}/query`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!r.ok) throw new Error(await r.text());
      const data = await r.json();

      // Merge the query's subgraph into the visible graph so the highlighted
      // path is always on screen — otherwise a search only highlights nodes
      // that happened to already be expanded, and looks like it did nothing.
      const subNodes = data.subgraph?.nodes ?? [];
      const subEdges = data.subgraph?.edges ?? [];
      const nn = { ...nodes };
      subNodes.forEach((n) => {
        if (!nn[n.id]) {
          nn[n.id] = {
            id: n.id,
            label: n.name,
            kind: n.kind,
            type: n.type,
            file_path: n.file_path,
            description: n.summary || "",
            adapter_metadata: n.adapter_metadata || {},
            parent: null,
          };
        }
      });
      const ne = { ...edges };
      subEdges.forEach((e) => {
        const id = `query:${e.source_id}->${e.target_id}`;
        if (nn[e.source_id] && nn[e.target_id] && !ne[id]) {
          ne[id] = { id, source: e.source_id, target: e.target_id, kind: "CALLS" };
        }
      });

      get()._pushHistory();
      set({
        nodes: nn,
        edges: ne,
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
    if (format === "mermaid" || format === "llm") {
      set({ exportStatus: "downloading" });
      try {
        const r = await apiFetch(`${API}/export/${graphId}?format=${format}`);
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
        const r = await apiFetch(`${API}/export/${graphId}?format=json`);
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
    await apiFetch(`${API}/feedback`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ graph_id: graphId, query_id: queryId, rating }),
    });
  },

  update: async () => {
    const { graphId } = get();
    if (!graphId) return;
    await apiFetch(`${API}/update`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ graph_id: graphId }),
    });
    // The graph was just rebuilt from disk — old history entries would point
    // Back at nodes/edges that may no longer reflect the current code.
    set({ viewHistory: [], currentDepth: null });
    await get().loadInitial();
  },

  openSummary: () => {
    set({ summaryOpen: true });
    if (!get().flowSummary) get().fetchFlowSummary();
  },
  closeSummary: () => set({ summaryOpen: false }),

  // Fetch the stored flow summary. If the graph predates the feature (404),
  // generate one on demand via POST. `regenerate` forces a fresh POST.
  fetchFlowSummary: async (regenerate = false) => {
    const { graphId } = get();
    if (!graphId) return;
    set({ flowSummaryLoading: true, flowSummaryError: "" });
    try {
      let r;
      if (regenerate) {
        r = await apiFetch(`${API}/flow_summary/${graphId}`, { method: "POST" });
      } else {
        r = await apiFetch(`${API}/flow_summary/${graphId}`);
        if (r.status === 404) {
          r = await apiFetch(`${API}/flow_summary/${graphId}`, { method: "POST" });
        }
      }
      if (!r.ok) throw new Error(await r.text());
      set({ flowSummary: await r.json() });
    } catch (e) {
      set({ flowSummaryError: String(e) });
    } finally {
      set({ flowSummaryLoading: false });
    }
  },

  // Stop both servers. The backend kills itself and the Vite dev server, so the
  // fetch never returns cleanly — we mark the UI stopped regardless and try to
  // close the tab.
  shutdownServers: async () => {
    set({ serverStopped: true });
    try {
      await apiFetch(`${API}/shutdown`, { method: "POST" });
    } catch (e) {
      /* expected: the server dies mid-response */
    }
    setTimeout(() => { try { window.close(); } catch (_) {} }, 400);
  },
}));
