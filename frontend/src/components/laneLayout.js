import { useMemo } from "react";
import { useFlowStore } from "../store.js";

// Prominent hues that never blend into the dark backdrop (no blue/green).
export const BRANCH_HUES = [10, 30, 48, 330, 300, 275, 355, 50];

// With at least this many roots, stacking every lane vertically produces a
// column too tall to read; the graph switches to lane overview + focus.
export const LANE_OVERVIEW_MIN = 6;

// For every visible node, resolve which root it descends from (its "branch"),
// its hop-count from that root, and the root's assigned hue. Root order is
// taken from the store's rootIds where possible so hue assignment stays
// stable across re-renders instead of shuffling with object key order.
export function computeBranchInfo(rawNodes, rootIds) {
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
  const rootSet = new Set(rootIds);
  const discoveredSet = new Set(discovered);
  const orderedRoots = [
    ...rootIds.filter((id) => discoveredSet.has(id)),
    ...discovered.filter((id) => !rootSet.has(id)),
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

// A lane is one root's whole expansion lineage. `laneEdges` collapses every
// call between two different lanes into one weighted edge — that lane-level
// graph is what the overview draws.
function deriveLanes(nodes, edges, info, orderedRoots) {
  const members = new Map(orderedRoots.map((r) => [r, []]));
  const laneOf = {};
  Object.values(nodes).forEach((n) => {
    const root = info[n.id]?.root;
    if (root === undefined) return;
    laneOf[n.id] = root;
    members.get(root)?.push(n.id);
  });

  const cross = new Map();
  Object.values(edges).forEach((e) => {
    if (e.kind === "CONTAINS") return;
    const a = laneOf[e.source], b = laneOf[e.target];
    if (!a || !b || a === b) return;
    const key = `${a}\u0000${b}`;
    cross.set(key, (cross.get(key) || 0) + 1);
  });
  const laneEdges = [...cross].map(([key, count]) => {
    const [source, target] = key.split("\u0000");
    return { source, target, count };
  });

  const calls = {}, calledBy = {};
  laneEdges.forEach(({ source, target, count }) => {
    calls[source] = (calls[source] || 0) + count;
    calledBy[target] = (calledBy[target] || 0) + count;
  });

  const lanes = orderedRoots
    .filter((id) => members.get(id)?.length)
    .map((id) => {
      const root = nodes[id];
      const ids = members.get(id);
      return {
        id,
        label: root?.label ?? id,
        path: root?.file_path || (root?.description?.startsWith("(stub)") ? "" : root?.description) || "",
        kind: root?.kind === "module" ? "Module" : root?.kind === "file" ? "File" : "Symbol",
        unit: ids.some((m) => nodes[m]?.kind !== "file" && nodes[m]?.kind !== "module") ? "symbols" : "files",
        count: ids.length,
        calls: calls[id] || 0,
        calledBy: calledBy[id] || 0,
        hue: info[id]?.hue ?? BRANCH_HUES[0],
      };
    })
    .sort((a, b) => (b.calls + b.calledBy) - (a.calls + a.calledBy) || b.count - a.count);

  return { lanes, laneOf, laneEdges, members };
}

// One derivation shared by the canvas and the sidebar rail.
export function useLanes() {
  const nodes = useFlowStore((s) => s.nodes);
  const edges = useFlowStore((s) => s.edges);
  const rootIds = useFlowStore((s) => s.rootIds);
  return useMemo(() => {
    const { info, orderedRoots } = computeBranchInfo(Object.values(nodes), rootIds);
    return { branchInfo: info, orderedRoots, ...deriveLanes(nodes, edges, info, orderedRoots) };
  }, [nodes, edges, rootIds]);
}

// ── Overview layout ─────────────────────────────────────────────────────────
export const CARD_W = 250;
export const CARD_H = 104;
const GAP_X = 80;
const GAP_Y = 24;
const BAND_GAP = 56;

// Layered left-to-right by call direction: a lane sits one column right of the
// deepest lane that calls it, so reading order matches flow. Back edges of a
// cycle are ignored for ranking. The columns are then cut into vertical stacks
// and wrapped into bands (like lines of text) using whichever stack height and
// stack count per band lets the whole thing render at the largest scale in the
// given viewport — a long call chain wraps instead of shrinking to a sliver.
export function layoutOverview(lanes, laneEdges, viewport = { w: 1000, h: 700 }) {
  const n = lanes.length;
  if (n === 0) return new Map();
  const idx = new Map(lanes.map((l, i) => [l.id, i]));
  const out = Array.from({ length: n }, () => []);
  const inn = Array.from({ length: n }, () => []);
  laneEdges.forEach(({ source, target }) => {
    const s = idx.get(source), t = idx.get(target);
    if (s === undefined || t === undefined) return;
    out[s].push(t);
    inn[t].push(s);
  });

  const state = new Array(n).fill(0);
  const dag = Array.from({ length: n }, () => []);
  const post = [];
  const dfs = (u) => {
    state[u] = 1;
    for (const v of out[u]) {
      if (state[v] === 1) continue;
      dag[u].push(v);
      if (state[v] === 0) dfs(v);
    }
    state[u] = 2;
    post.push(u);
  };
  for (let u = 0; u < n; u++) if (state[u] === 0) dfs(u);

  const rank = new Array(n).fill(0);
  for (const u of post.reverse()) for (const v of dag[u]) rank[v] = Math.max(rank[v], rank[u] + 1);

  const columns = [];
  for (let u = 0; u < n; u++) (columns[rank[u]] ||= []).push(u);
  const slot = new Array(n);
  columns.forEach((col, r) => {
    if (r > 0) {
      const bary = (v) => {
        const ps = inn[v].filter((u) => rank[u] < r).map((u) => slot[u]);
        return ps.length ? ps.reduce((a, b) => a + b, 0) / ps.length : Infinity;
      };
      col.sort((a, b) => bary(a) - bary(b) || a - b);
    }
    col.forEach((v, i) => { slot[v] = i; });
  });

  // fitView leaves ~12% padding, so plan against the usable area.
  const usableW = viewport.w * 0.88, usableH = viewport.h * 0.88;
  const maxN = Math.max(...columns.map((c) => c.length), 1);
  let best = null;
  for (let rows = 1; rows <= maxN; rows++) {
    const stacks = columns.flatMap((col) => {
      const cut = [];
      for (let i = 0; i < col.length; i += rows) cut.push(col.slice(i, i + rows));
      return cut;
    });
    const tallest = Math.min(rows, maxN);
    const stackH = tallest * (CARD_H + GAP_Y) - GAP_Y;
    for (let perBand = 1; perBand <= stacks.length; perBand++) {
      const bands = Math.ceil(stacks.length / perBand);
      const width = perBand * (CARD_W + GAP_X) - GAP_X;
      const height = bands * stackH + (bands - 1) * BAND_GAP;
      const scale = Math.min(usableW / width, usableH / height, 1);
      if (!best || scale > best.scale + 1e-9) best = { scale, stacks, perBand, stackH };
    }
  }

  const pos = new Map();
  best.stacks.forEach((stack, i) => {
    const band = Math.floor(i / best.perBand), col = i % best.perBand;
    const yOffset = (best.stackH - (stack.length * (CARD_H + GAP_Y) - GAP_Y)) / 2;
    stack.forEach((v, r) => pos.set(lanes[v].id, {
      x: col * (CARD_W + GAP_X),
      y: band * (best.stackH + BAND_GAP) + yOffset + r * (CARD_H + GAP_Y),
    }));
  });
  return pos;
}
