import React, { useState } from "react";
import { useFlowStore } from "../store.js";
import { CloseIcon } from "./ImpactPanel.jsx";

const TABS = [
  { key: "god_nodes",  label: "God nodes",  icon: "⭐" },
  { key: "high_risk",  label: "High risk",  icon: "⚠️" },
  { key: "cycles",     label: "Cycles",     icon: "🔁" },
  { key: "dead_code",  label: "Dead code",  icon: "🪦" },
  { key: "couplings",  label: "Couplings",  icon: "🧵" },
];

function shortPath(p) {
  return (p || "").split("/").slice(-2).join("/");
}

function NodeRow({ node, extra, onClick }) {
  return (
    <button
      onClick={onClick}
      className="w-full flex items-start justify-between gap-2 px-3 py-2 rounded-lg text-left hover:bg-[#111a30] transition-colors border-b border-[#141d33] last:border-0"
    >
      <div className="min-w-0">
        <div className="font-mono text-xs text-white/85 truncate">{node.name}</div>
        <div className="text-[10px] text-slate-600 truncate">{shortPath(node.file_path)}</div>
        {node.summary && (
          <div className="text-[10px] text-slate-500 mt-0.5 line-clamp-2">{node.summary}</div>
        )}
      </div>
      {extra && <div className="shrink-0 text-right text-[10px] text-slate-400">{extra}</div>}
    </button>
  );
}

export default function InsightsPanel() {
  const insightsOpen    = useFlowStore((s) => s.insightsOpen);
  const insights        = useFlowStore((s) => s.insights);
  const insightsLoading = useFlowStore((s) => s.insightsLoading);
  const insightsError   = useFlowStore((s) => s.insightsError);
  const closeInsights   = useFlowStore((s) => s.closeInsights);
  const fetchInsights   = useFlowStore((s) => s.fetchInsights);
  const selectNode      = useFlowStore((s) => s.selectNode);
  const [tab, setTab] = useState("god_nodes");

  if (!insightsOpen) return null;

  const stats = insights?.stats || {};
  const pick = (id) => { selectNode(id); };

  return (
    <div className="fixed inset-0 z-40 flex items-center justify-center bg-black/60 backdrop-blur-sm" onClick={closeInsights}>
      <div
        className="w-[640px] max-w-[92vw] max-h-[80vh] flex flex-col rounded-2xl bg-[#0a0f1a] border border-[#1a2540] shadow-2xl overflow-hidden"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-center justify-between px-5 py-3.5 border-b border-[#1a2540] shrink-0">
          <div>
            <div className="text-sm font-semibold text-white">Graph Insights</div>
            <div className="text-[10px] text-slate-500">
              {stats.callable_nodes ?? "?"} functions · {stats.call_edges ?? "?"} call edges ·{" "}
              {stats.cycle_count ?? 0} cycles · {stats.dead_code_count ?? 0} dead-code candidates
            </div>
          </div>
          <div className="flex items-center gap-2">
            <button
              onClick={() => fetchInsights()}
              title="Recompute analytics"
              className="text-[10px] px-2.5 py-1 rounded-lg border border-[#1e2d4a] text-slate-400 hover:text-slate-200 hover:border-slate-500 transition-colors"
            >
              Refresh
            </button>
            <button
              onClick={closeInsights}
              className="w-7 h-7 rounded flex items-center justify-center text-slate-500 hover:text-slate-200 hover:bg-slate-800 transition-colors"
            >
              <CloseIcon size={14} />
            </button>
          </div>
        </div>

        {/* Tabs */}
        <div className="flex gap-1 px-4 pt-3 shrink-0">
          {TABS.map(({ key, label, icon }) => (
            <button
              key={key}
              onClick={() => setTab(key)}
              className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-[11px] border transition-colors ${
                tab === key
                  ? "border-blue-500/80 bg-blue-900/30 text-blue-300"
                  : "border-[#1e2d4a] text-slate-500 hover:text-slate-300 hover:border-slate-500"
              }`}
            >
              <span>{icon}</span>{label}
            </button>
          ))}
        </div>

        {/* Body */}
        <div className="flex-1 overflow-y-auto px-4 py-3">
          {insightsLoading && (
            <div className="flex items-center justify-center py-10 text-slate-500 text-xs">Computing analytics…</div>
          )}
          {insightsError && !insightsLoading && (
            <div className="text-xs text-rose-400 bg-rose-950/40 border border-rose-900/60 rounded-lg p-3">{insightsError}</div>
          )}

          {insights && !insightsLoading && tab === "god_nodes" && (
            <>
              <div className="text-[10px] text-slate-500 mb-2">
                Most influential functions by PageRank, degree, and betweenness centrality. Click to select in the graph.
              </div>
              {(insights.god_nodes || []).map((n, i) => (
                <NodeRow
                  key={n.id}
                  node={n}
                  onClick={() => pick(n.id)}
                  extra={<>#{i + 1}<br />in {n.in_degree} · out {n.out_degree}</>}
                />
              ))}
            </>
          )}

          {insights && !insightsLoading && tab === "high_risk" && (
            <>
              <div className="text-[10px] text-slate-500 mb-2">
                Components matching multiple risk signals — big fan-out, many callers, high complexity.
              </div>
              {(insights.high_risk || []).length === 0 && (
                <div className="text-xs text-slate-500 py-6 text-center">No components matched multiple risk signals. 🎉</div>
              )}
              {(insights.high_risk || []).map((n) => (
                <NodeRow key={n.id} node={n} onClick={() => pick(n.id)}
                  extra={<span className="text-amber-400">{(n.reasons || []).join("; ")}</span>} />
              ))}
            </>
          )}

          {insights && !insightsLoading && tab === "cycles" && (
            <>
              <div className="text-[10px] text-slate-500 mb-2">
                Circular call dependencies — refactoring candidates.
              </div>
              {(insights.cycles || []).length === 0 && (
                <div className="text-xs text-slate-500 py-6 text-center">No circular dependencies found. 🎉</div>
              )}
              {(insights.cycles || []).map((cyc, i) => (
                <div key={i} className="px-3 py-2 border-b border-[#141d33] last:border-0">
                  <div className="flex flex-wrap items-center gap-1 text-[11px] font-mono">
                    {cyc.map((n, j) => (
                      <React.Fragment key={j}>
                        <button onClick={() => pick(n.id)} className="text-rose-300 hover:underline">{n.name}</button>
                        <span className="text-slate-600">→</span>
                      </React.Fragment>
                    ))}
                    <span className="text-rose-300/60">{cyc[0]?.name}</span>
                  </div>
                </div>
              ))}
            </>
          )}

          {insights && !insightsLoading && tab === "dead_code" && (
            <>
              <div className="text-[10px] text-slate-500 mb-2">
                Never invoked anywhere in the graph. Candidates only — dynamic dispatch and framework hooks cause false positives.
              </div>
              {(insights.dead_code || []).length === 0 && (
                <div className="text-xs text-slate-500 py-6 text-center">Every function has at least one caller. 🎉</div>
              )}
              {(insights.dead_code || []).map((n) => (
                <NodeRow key={n.id} node={n} onClick={() => pick(n.id)} />
              ))}
            </>
          )}

          {insights && !insightsLoading && tab === "couplings" && (
            <>
              <div className="text-[10px] text-slate-500 mb-2">
                Surprising cross-module couplings — subsystem pairs joined by only one or two call edges.
              </div>
              {(insights.surprising_couplings || []).length === 0 && (
                <div className="text-xs text-slate-500 py-6 text-center">No thin cross-module threads detected.</div>
              )}
              {(insights.surprising_couplings || []).map((c, i) => (
                <div key={i} className="px-3 py-2 border-b border-[#141d33] last:border-0">
                  <div className="text-[11px] text-slate-300">
                    <span className="text-blue-300">{c.from_module}</span>
                    <span className="text-slate-600"> → </span>
                    <span className="text-blue-300">{c.to_module}</span>
                    <span className="text-slate-500"> · {c.edge_count} edge{c.edge_count !== 1 ? "s" : ""}</span>
                  </div>
                  <div className="text-[10px] text-slate-500 font-mono mt-0.5">
                    e.g. {c.example?.source} → {c.example?.target}
                  </div>
                </div>
              ))}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
