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
// Above this many nodes in a single view, ReactFlow keeps up but the render
// starts to feel heavy even with culling/memoization — cap the view and show
// a banner (see `nodeBudget` below) rather than silently dropping nodes or
// letting the view degrade with no explanation. Ranked by degree so what's
// kept is what's actually connected, not an arbitrary prefix.
export const MAX_RENDERED_NODES = 600;

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

// If `nodes` exceeds MAX_RENDERED_NODES, keep only the top-N by degree
// (fan-in + fan-out within this view) and drop edges that no longer have
// both endpoints — mutates `nodes`/`edges` in place, returns budget info for
// the UI to render an honest "showing N of M" banner instead of a silent cap.
function applyNodeBudget(nodes, edges) {
  const total = Object.keys(nodes).length;
  if (total <= MAX_RENDERED_NODES) return { truncated: false, shown: total, total };
  const degree = {};
  Object.values(edges).forEach((e) => {
    degree[e.source] = (degree[e.source] || 0) + 1;
    degree[e.target] = (degree[e.target] || 0) + 1;
  });
  const ranked = Object.keys(nodes).sort((a, b) => (degree[b] || 0) - (degree[a] || 0));
  const keep = new Set(ranked.slice(0, MAX_RENDERED_NODES));
  Object.keys(nodes).forEach((id) => { if (!keep.has(id)) delete nodes[id]; });
  Object.keys(edges).forEach((eid) => {
    const e = edges[eid];
    if (!nodes[e.source] || !nodes[e.target]) delete edges[eid];
  });
  return { truncated: true, shown: keep.size, total };
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

  // Set by loadDepthView when the requested depth would render more than
  // MAX_RENDERED_NODES — { truncated, shown, total } | null. Drives the
  // "Showing N of M" banner so a capped view is disclosed, not silent.
  nodeBudget: null,

  // Onboarding modal — shown once per browser (see WelcomeModal.jsx), and
  // reopenable anytime via the sidebar's "?" button.
  helpOpen: false,
  openHelp: () => set({ helpOpen: true }),
  closeHelp: () => set({ helpOpen: false }),

  // Repository flow summary (generated at ingest, served by /flow_summary)
  flowSummary: null,
  flowSummaryLoading: false,
  flowSummaryError: "",
  summaryOpen: false,

  // Impact analysis panel
  impactData: null,
  nodeSource: null,
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

  // Root id of the lane shown in focus mode; null = lane overview. Only
  // meaningful when there are enough roots for the overview (see GraphView).
  focusLaneId: null,

  // Dry-run trace: source-ordered walk of everything one function calls.
  // `traceStep` is the step being shown (-1 = the whole trace at once).
  // Review a change: functions touched by a git diff, ranked by who depends on them
  reviewOpen: false,
  review: null,
  reviewLoading: false,
  reviewError: "",
  // What is being reviewed: null = the queue screen; otherwise a branch opened into its own
  // worktree graph, or the author's working copy (`author: true`, the loaded graph itself).
  reviewTarget: null,
  queue: null,
  queueBase: "main",
  queueDays: 30,
  queueLoading: false,
  queueError: "",
  openingRef: "",
  aiResults: {},
  aiLoading: false,
  aiError: "",
  aiUsage: null,
  testRuns: {},
  traceOpen: false,
  trace: null,
  traceStep: -1,
  traceStarts: [],
  traceLoading: false,
  traceError: "",

  setRepoPath: (repoPath) => set({ repoPath }),

  // Snapshot the current view onto the undo stack before a navigation action
  // changes it. Safe without cloning: every action below builds a fresh
  // object (`{...nodes}`) rather than mutating the current one in place, so
  // the reference captured here is never touched after this point. Capped so
  // a very long session doesn't grow the stack unbounded.
  _pushHistory: () => {
    const { nodes, edges, expanded, rootIds, currentDepth, focusLaneId, viewHistory } = get();
    const next = [...viewHistory, { nodes, edges, expanded, rootIds, currentDepth, focusLaneId }];
    set({ viewHistory: next.length > 50 ? next.slice(-50) : next });
  },

  ingest: async () => {
    const { repoPath } = get();
    if (!repoPath.trim()) { set({ error: "Enter a repository path or git URL, then click Ingest." }); return; }
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
        viewHistory: [], currentDepth: null, focusLaneId: null,
        trace: null, traceStep: -1, traceStarts: [], traceError: "", review: null, reviewOpen: false, reviewError: "",
        reviewTarget: null, queue: null, aiResults: {}, testRuns: {},
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
    set({ nodes, edges, expanded: {}, rootIds: g.nodes.map((n) => n.id), selectedId: null, focusLaneId: null });
    // Fire-and-forget: populate heatmap metrics (pagerank/degree/complexity)
    // for the interactive graph without blocking the initial view from showing.
    if (!get().nodeMetrics || Object.keys(get().nodeMetrics).length === 0) {
      get().fetchInsights();
    }
  },

  selectNode: (id) => {
    set({ selectedId: id, nodeSource: null });
    // Auto-fetch impact + doc references + source when a function node is selected
    if (id && !id.startsWith("file::") && !id.startsWith("mod_")) {
      get().fetchImpact(id);
      get().fetchNodeRefs(id);
      get().fetchSource(id);
    } else {
      set({ impactData: null, nodeRefs: null });
    }
  },

  // Select a function and make sure it is on the canvas: if it is not visible
  // (e.g. picked from search or from a caller list), re-root the canvas on it
  // with its callees expanded. Callers stay in the inspector.
  revealFunction: async (id) => {
    const { graphId, nodes } = get();
    if (!graphId) return;
    if (nodes[id]) { get().selectNode(id); return; }
    const r = await apiFetch(`${API}/focus?graph_id=${graphId}&node_id=${encodeURIComponent(id)}`);
    if (!r.ok) { set({ error: await r.text() }); return; }
    const node = await r.json();
    get()._pushHistory();
    set({ nodes: { [id]: node }, edges: {}, expanded: {}, rootIds: [id], currentDepth: null, focusLaneId: null });
    get().selectNode(id);
    await get().toggleExpand(id);
  },

  fetchSource: async (nodeId) => {
    const { graphId } = get();
    if (!graphId || !nodeId) return;
    try {
      const r = await apiFetch(`${API}/node_source?graph_id=${graphId}&node_id=${encodeURIComponent(nodeId)}`);
      if (get().selectedId !== nodeId) return;
      set({ nodeSource: r.ok ? await r.json() : null });
    } catch (_) {
      set({ nodeSource: null });
    }
  },
  openTrace: async (startId) => {
    get().clearQuery();
    set({ traceOpen: true, traceError: "" });
    if (startId) await get().runTrace(startId);
    else if (!get().traceStarts.length) await get().searchTraceStarts("");
  },
  closeTrace: () => set({ traceOpen: false, trace: null, traceStep: -1 }),
  clearTrace: () => set({ trace: null, traceStep: -1 }),
  setTraceStep: (i) => set({ traceStep: i }),

  runPath: async (sourceId, targetId) => {
    const { graphId } = get();
    if (!graphId) return;
    set({ traceLoading: true, traceError: "" });
    try {
      const r = await apiFetch(`${API}/trace_path?graph_id=${graphId}&source=${encodeURIComponent(sourceId)}&target=${encodeURIComponent(targetId)}`);
      if (!r.ok) throw new Error(await r.text());
      set({ trace: await r.json(), traceStep: -1, traceOpen: true });
    } catch (e) {
      set({ traceError: String(e) });
    } finally {
      set({ traceLoading: false });
    }
  },

  searchTraceStarts: async (q) => {
    const { graphId } = get();
    if (!graphId) return;
    set({ traceLoading: true, traceError: "" });
    try {
      const r = await apiFetch(`${API}/trace_starts?graph_id=${graphId}&q=${encodeURIComponent(q)}`);
      if (!r.ok) throw new Error(await r.text());
      set({ traceStarts: (await r.json()).starts });
    } catch (e) {
      set({ traceError: String(e) });
    } finally {
      set({ traceLoading: false });
    }
  },

  runTrace: async (startId) => {
    const { graphId } = get();
    if (!graphId) return;
    set({ traceLoading: true, traceError: "" });
    try {
      const r = await apiFetch(`${API}/trace?graph_id=${graphId}&start=${encodeURIComponent(startId)}`);
      if (!r.ok) throw new Error(await r.text());
      set({ trace: await r.json(), traceStep: -1, traceOpen: true });
    } catch (e) {
      set({ traceError: String(e) });
    } finally {
      set({ traceLoading: false });
    }
  },

  // Enter focus mode on one lane (or return to the overview with null).
  // `keepSelection` lets a selection made elsewhere (e.g. Graph Insights)
  // survive the jump to the lane that contains it.
  focusLane: (id, keepSelection = false) => {
    if (get().focusLaneId === id) return;
    get()._pushHistory();
    set(keepSelection ? { focusLaneId: id } : { focusLaneId: id, selectedId: null, impactData: null, nodeRefs: null, nodeSource: null });
  },
  clearSelection: () => set({ selectedId: null, impactData: null, nodeRefs: null, nodeSource: null }),
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
      // Functions view starts at the top call-graph tier only; callees are
      // pulled in one click at a time (see toggleExpand).
      const tierParam = depth === 3 ? "&tier=1" : "";
      const r = await apiFetch(`${API}/graph?graph_id=${graphId}&depth=${depth}${tierParam}`);
      if (!r.ok) { set({ error: await r.text() }); return; }
      const data = await r.json();
      const nodes = {};
      const edges = {};
      (data.nodes || []).forEach((n) => (nodes[n.id] = n));
      (data.edges || []).forEach((e) => {
        if (nodes[e.source] && nodes[e.target]) edges[e.id] = e;
      });
      let nodeBudget = applyNodeBudget(nodes, edges);
      if (data.tier1 && !nodeBudget.truncated) {
        nodeBudget = { truncated: data.tier1.shown < data.tier1.total, shown: data.tier1.shown, total: data.tier1.total, tiered: true };
      }
      // Only true roots (no parent in this view) count as roots — a tier-1
      // function has its module as parent, so expanding it must not trigger the
      // root-sibling collapse and fold away other functions the user opened.
      const rootIds = Object.keys(nodes).filter((id) => !nodes[id].parent || !nodes[nodes[id].parent]);
      get()._pushHistory();
      set({ nodes, edges, expanded: {}, rootIds, selectedId: null, currentDepth: depth, nodeBudget, focusLaneId: null });
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
      if (nn[nodeId]?.hiddenCallees) nn[nodeId] = { ...nn[nodeId], hiddenCallees: 0 };
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

    // Only children this click newly adds are recorded as owned: collapsing
    // must not delete nodes that were already on screen (e.g. another tier-1
    // function that this one also calls).
    const childIds = [];
    for (const c of data.children) {
      if (!nn[c.id]) { nn[c.id] = c; childIds.push(c.id); }
    }
    for (const e of data.edges) {
      if (nn[e.source] && nn[e.target]) ne[e.id] = e;
    }
    nexp[nodeId] = childIds;
    // Callees arrive ranked by importance and capped; remember how many were
    // left out on the node itself so Back/collapse restore it for free.
    if (data.hidden) nn[nodeId] = { ...nn[nodeId], hiddenCallees: data.hidden };

    get()._pushHistory();
    set({ nodes: nn, edges: ne, expanded: nexp });
  },

  // "+N more" on an expanded node: fetch its full, uncapped callee list and
  // add whatever isn't on screen yet (still ranked most-important first).
  showAllCallees: async (nodeId) => {
    const { graphId, nodes, edges, expanded } = get();
    if (!graphId || !nodes[nodeId]) return;
    const r = await apiFetch(`${API}/expand?graph_id=${graphId}&node_id=${encodeURIComponent(nodeId)}&action=callees&limit=0`);
    if (!r.ok) { set({ error: await r.text() }); return; }
    const data = await r.json();
    const nn = { ...nodes }, ne = { ...edges }, nexp = { ...expanded };
    const owned = [...(nexp[nodeId] || [])];
    for (const c of data.children) {
      if (!nn[c.id]) { nn[c.id] = c; owned.push(c.id); }
    }
    for (const e of data.edges) {
      if (nn[e.source] && nn[e.target]) ne[e.id] = e;
    }
    nexp[nodeId] = owned;
    nn[nodeId] = { ...nn[nodeId], hiddenCallees: 0 };
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
          if (!nn[c.id]) { nn[c.id] = c; added++; childIds.push(c.id); }
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
      rootIds: prev.rootIds, currentDepth: prev.currentDepth, focusLaneId: prev.focusLaneId ?? null,
      viewHistory: viewHistory.slice(0, -1), selectedId: null,
    });
  },

  resetView: async () => {
    get()._pushHistory();
    set({ nodes: {}, edges: {}, expanded: {}, selectedId: null, currentDepth: null, focusLaneId: null });
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
        // Results can land in any lane — show the overview so highlighted
        // lanes are visible instead of staying inside one focused lane.
        focusLaneId: null,

  // Dry-run trace: source-ordered walk of everything one function calls.
  // `traceStep` is the step being shown (-1 = the whole trace at once).
  traceOpen: false,
  trace: null,
  traceStep: -1,
  traceStarts: [],
  traceLoading: false,
  traceError: "",
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

  // Review opens on the queue; a branch or the working copy is picked from there.
  openReview: () => {
    set({ reviewOpen: true });
    if (!get().reviewTarget && !get().queue && !get().queueLoading) get().loadQueue();
  },
  closeReview: () => set({ reviewOpen: false }),
  backToQueue: () => {
    set({ reviewTarget: null, review: null, reviewError: "", aiResults: {}, aiUsage: null, testRuns: {} });
    get().loadQueue();  // opened checkouts and disk use changed
  },

  loadQueue: async (base, days) => {
    const { graphId } = get();
    const b = (base ?? get().queueBase).trim() || "main";
    const d = days ?? (b === get().queueBase ? get().queueDays : 30);  // a refresh keeps the window, a new base resets it
    if (!graphId) return;
    set({ queueLoading: true, queueError: "", queueBase: b, queueDays: d });
    try {
      const r = await apiFetch(`${API}/queue?graph_id=${graphId}&base=${encodeURIComponent(b)}&days=${d}`);
      const body = await r.json().catch(() => null);
      if (!r.ok) throw new Error(body?.detail || `Could not list branches (${r.status})`);
      set({ queue: body });
    } catch (e) {
      set({ queue: null, queueError: e.message || String(e) });
    } finally {
      set({ queueLoading: false });
    }
  },

  // The author's own change: the working copy against the base, uncommitted edits included.
  openWorkingCopy: () => {
    const { graphId, queueBase } = get();
    set({ reviewTarget: { graphId, ref: null, base: queueBase, committed: false, author: true, label: "My working copy" },
          aiResults: {}, aiUsage: null, testRuns: {} });
    get().runReview();
  },

  // A branch from the queue: checked out into a Flowify-owned worktree, ingested, then reviewed.
  openBranch: async (ref) => {
    const { graphId, queueBase } = get();
    set({ openingRef: ref, queueError: "" });
    try {
      const r = await apiFetch(`${API}/queue/open`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ graph_id: graphId, ref, base: queueBase }),
      });
      const body = await r.json().catch(() => null);
      if (!r.ok) throw new Error(body?.detail || `Could not open ${ref} (${r.status})`);
      set({ reviewTarget: { graphId: body.graph_id, ref, base: queueBase, committed: true, author: false, label: ref },
            aiResults: {}, aiUsage: null, testRuns: {} });
      await get().runReview();
    } catch (e) {
      set({ queueError: e.message || String(e) });
    } finally {
      set({ openingRef: "" });
    }
  },

  removeBranch: async (ref) => {
    const { graphId } = get();
    await apiFetch(`${API}/queue/worktree?graph_id=${graphId}&ref=${encodeURIComponent(ref)}`, { method: "DELETE" }).catch(() => {});
    await get().loadQueue();
  },

  runReview: async (base) => {
    const t = get().reviewTarget;
    if (!t) return;
    if (base) set({ reviewTarget: { ...t, base } });
    const b = base || t.base;
    set({ reviewLoading: true, reviewError: "" });
    try {
      // Line numbers come from the stored graph, so re-read changed files first.
      // A failed sync is not fatal: the diff itself reports anything actually wrong.
      await apiFetch(`${API}/update`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ graph_id: t.graphId }),
      }).catch(() => {});
      const r = await apiFetch(`${API}/review?graph_id=${t.graphId}&base=${encodeURIComponent(b)}&committed=${t.committed}`);
      if (!r.ok) {
        const body = await r.json().catch(() => null);
        throw new Error(body?.detail || `Review failed (${r.status})`);
      }
      set({ review: await r.json() });
    } catch (e) {
      set({ review: null, reviewError: e.message || String(e) });
    } finally {
      set({ reviewLoading: false });
    }
  },

  // Claude's notes on a few chunks. Costs money, so only ever on a person's click.
  runAI: async (chunkIds) => {
    const t = get().reviewTarget;
    if (!t || !chunkIds.length) return;
    set({ aiLoading: true, aiError: "" });
    try {
      const r = await apiFetch(`${API}/review/ai`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ graph_id: t.graphId, base: t.base, chunk_ids: chunkIds, author: t.author }),
      });
      const body = await r.json().catch(() => null);
      if (!r.ok) throw new Error(body?.detail || `Claude review failed (${r.status})`);
      set({ aiResults: { ...get().aiResults, ...body.results }, aiUsage: body.usage });
    } catch (e) {
      set({ aiError: e.message || String(e) });
    } finally {
      set({ aiLoading: false });
    }
  },

  runChunkTests: async (chunkId) => {
    const t = get().reviewTarget;
    if (!t) return;
    set({ testRuns: { ...get().testRuns, [chunkId]: { running: true } } });
    try {
      const r = await apiFetch(`${API}/review/tests`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ graph_id: t.graphId, base: t.base, chunk_id: chunkId }),
      });
      const body = await r.json().catch(() => null);
      if (!r.ok) throw new Error(body?.detail || `Tests failed to start (${r.status})`);
      set({ testRuns: { ...get().testRuns, [chunkId]: body } });
    } catch (e) {
      set({ testRuns: { ...get().testRuns, [chunkId]: { error: e.message || String(e) } } });
    }
  },

  saveNotes: async (chunks) => {
    const t = get().reviewTarget;
    const r = await apiFetch(`${API}/review/notes`, {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ graph_id: t.graphId, chunks }),
    });
    const body = await r.json().catch(() => null);
    if (!r.ok) throw new Error(body?.detail || `Could not save notes (${r.status})`);
    set({ review: { ...get().review, notes: body } });
    return body;
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
