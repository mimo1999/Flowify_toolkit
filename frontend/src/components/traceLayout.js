import { BRANCH_HUES } from "./laneLayout.js";

export const TRACE_W = 250;
export const TRACE_H = 92;
const GAP_X = 90;
const GAP_Y = 22;

const hueOf = (name) => {
  if (!name) return BRANCH_HUES[0];
  let h = 0;
  for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) >>> 0;
  return BRANCH_HUES[h % BRANCH_HUES.length];
};

export const ctxLabel = { cond: "if / else", loop: "loop", try: "try", except: "on error" };

// A trace is a tree of calls. Each function is drawn once, at its first visit
// (a later visit is an extra edge back to that card): depth is the column, and
// rows are handed out leaf by leaf so a parent sits level with its children.
// `step` = -1 shows the whole trace; otherwise later steps are dimmed.
export function traceGraph(trace, step = -1) {
  const steps = trace.steps;
  const expanded = steps.filter((s) => s.repeat_of === undefined && !s.cycle);
  const kids = new Map(expanded.map((s) => [s.index, []]));
  expanded.forEach((s) => { if (s.parent !== null && kids.has(s.parent)) kids.get(s.parent).push(s); });

  const row = new Map();
  let next = 0;
  const place = (s) => {
    const cs = kids.get(s.index);
    if (!cs.length) { row.set(s.index, next++); return; }
    cs.forEach(place);
    row.set(s.index, (row.get(cs[0].index) + row.get(cs[cs.length - 1].index)) / 2);
  };
  place(expanded[0]);

  // every step number a function appears at (repeats and cycles included)
  const appearsAt = new Map();
  steps.forEach((s) => appearsAt.set(s.id, [...(appearsAt.get(s.id) || []), s.index]));

  const nodes = expanded.map((s) => {
    const at = appearsAt.get(s.id);
    return {
      id: s.id,
      type: "traceNode",
      position: { x: s.depth * (TRACE_W + GAP_X), y: row.get(s.index) * (TRACE_H + GAP_Y) },
      draggable: false,
      data: {
        step: s.index,
        label: s.name,
        filePath: s.file_path,
        line: s.lineno,
        module: s.module,
        hue: hueOf(s.module),
        ctx: s.via?.ctx || null,
        enters: s.enters_module,
        ambiguous: s.unresolved_count || 0,
        again: at.length - 1,
        cycle: steps.some((t) => t.id === s.id && t.cycle),
        hidden: s.hidden_calls || 0,
        cut: s.cut || 0,
        isCurrent: step >= 0 && at.includes(step),
        isDim: step >= 0 && Math.min(...at) > step,
      },
    };
  });

  const edges = steps
    .filter((s) => s.parent !== null)
    .map((s) => {
      const future = step >= 0 && s.index > step;
      const stroke = s.cycle ? "#f87171" : s.via?.ctx ? "#f59e0b" : "#60a5fa";
      return {
        id: `t:${s.index}`,
        source: steps[s.parent].id,
        target: s.id,
        type: "smoothstep",
        label: String(s.index),
        labelStyle: { fill: "#cbd5e1", fontSize: 11, fontWeight: 600 },
        labelBgStyle: { fill: "#07090f", fillOpacity: 0.85 },
        markerEnd: { type: "arrowclosed", width: 14, height: 14, color: stroke },
        animated: step === s.index,
        style: {
          stroke,
          strokeWidth: step === s.index ? 2.5 : 1.5,
          strokeDasharray: s.cycle || s.repeat_of !== undefined ? "5 4" : undefined,
          opacity: future ? 0.12 : 0.75,
        },
        data: { restWidth: 1.5, restOpacity: future ? 0.12 : 0.75, baseWidth: 1.5 },
      };
    });

  return { nodes, edges };
}
