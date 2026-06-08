import React, { useState } from "react";
import { useFlowStore } from "../store.js";

const CopyIcon = () => (
  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
    <rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>
  </svg>
);
const CloseIcon = () => (
  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
    <line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>
  </svg>
);
const FnIcon = () => (
  <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
    <polyline points="16,18 22,12 16,6"/><polyline points="8,6 2,12 8,18"/>
  </svg>
);

const SEMANTIC_COLORS = {
  EXPOSES_API:    "#10b981",
  USES_DB:        "#8b5cf6",
  EMITS_EVENT:    "#f59e0b",
  CONSUMES_EVENT: "#ef4444",
  CALLS:          "#3b82f6",
};
const SEMANTIC_ICONS = {
  EXPOSES_API: "🔌", USES_DB: "🗄️", EMITS_EVENT: "📤",
  CONSUMES_EVENT: "📥", CALLS: "⚙️",
};

function ExecutionPath({ steps }) {
  if (!steps || steps.length === 0) return null;
  return (
    <div className="px-5 py-4 border-b border-[#1a2540]">
      <div className="text-[10px] uppercase tracking-widest text-slate-600 font-semibold mb-3">
        Execution path
      </div>
      <div className="space-y-0">
        {steps.slice(0, 12).map((step, i) => {
          const color = SEMANTIC_COLORS[step.semantic_kind] || "#3b82f6";
          const icon  = SEMANTIC_ICONS[step.semantic_kind] || "⚙️";
          return (
            <div key={step.id}>
              <div
                className="rounded-lg border px-3 py-2 text-xs fade-in"
                style={{
                  borderColor: `${color}40`,
                  background: `${color}0a`,
                  animationDelay: `${i * 40}ms`,
                }}
              >
                <div className="flex items-center gap-1.5">
                  <span style={{ color }}>{icon}</span>
                  <span className="font-mono text-white/90 truncate flex-1">{step.name}</span>
                  {step.semantic_kind !== "CALLS" && (
                    <span
                      className="text-[8px] font-bold px-1.5 py-0.5 rounded shrink-0"
                      style={{ background: `${color}22`, color }}
                    >
                      {step.semantic_kind.replace("_", " ")}
                    </span>
                  )}
                </div>
                <div className="text-[10px] text-slate-500 truncate mt-0.5 pl-5">
                  {step.file_path?.split("/").slice(-2).join("/")}
                </div>
                {step.summary && !step.summary.startsWith("(stub)") && (
                  <div className="text-[10px] text-slate-400 mt-1 pl-5 line-clamp-1">
                    {step.summary}
                  </div>
                )}
              </div>
              {i < steps.slice(0, 12).length - 1 && (
                <div className="flex items-center justify-start pl-[22px] my-0.5">
                  <div className="w-px h-3 bg-slate-700" />
                  <span className="text-[8px] text-slate-600 ml-1">
                    {step.edge_label && step.edge_label !== "CALLS" ? step.edge_label : "↓"}
                  </span>
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

export default function QueryResults() {
  const explanation        = useFlowStore((s) => s.explanation);
  const queryNodes         = useFlowStore((s) => s.queryNodes);
  const queryId            = useFlowStore((s) => s.queryId);
  const executionSteps     = useFlowStore((s) => s.executionSteps);
  const graphNodesConsulted = useFlowStore((s) => s.graphNodesConsulted);
  const clearQuery         = useFlowStore((s) => s.clearQuery);
  const sendFeedback       = useFlowStore((s) => s.sendFeedback);

  const [copied,   setCopied]   = useState(false);
  const [feedback, setFeedback] = useState(null);

  if (!explanation) return null;

  const isStub = explanation.startsWith("(stub)");

  const buildContext = () => {
    const lines = ["## Relevant code context\n"];
    queryNodes.slice(0, 15).forEach((n) => {
      lines.push(`### ${n.name} — ${n.file_path}`);
      if (n.summary && !n.summary.startsWith("(stub)")) lines.push(n.summary);
      if (n.code_snippet) lines.push("```python\n" + n.code_snippet + "\n```");
      lines.push("");
    });
    lines.push("## AI explanation\n" + explanation);
    return lines.join("\n");
  };

  const handleCopy = () => {
    navigator.clipboard.writeText(buildContext()).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    });
  };

  const handleFeedback = (rating) => {
    sendFeedback(rating);
    setFeedback(rating);
  };

  const visibleNodes = queryNodes.slice(0, 15);

  return (
    <div className="absolute top-0 right-0 h-full w-[340px] z-10 flex flex-col bg-[#0a0f1a]/96 backdrop-blur-md border-l border-[#1a2540] shadow-[−4px_0_32px_rgba(0,0,0,0.5)] slide-in-right">

      {/* Header */}
      <div className="flex items-center justify-between px-5 py-4 border-b border-[#1a2540] shrink-0">
        <div>
          <div className="text-xs font-semibold text-white">Query results</div>
          {graphNodesConsulted > 0 && (
            <div className="flex items-center gap-1.5 mt-1">
              <span className="w-1.5 h-1.5 rounded-full bg-emerald-400" />
              <span className="text-[10px] text-emerald-400">Grounded in {graphNodesConsulted} graph nodes</span>
            </div>
          )}
        </div>
        <button
          onClick={clearQuery}
          className="w-7 h-7 rounded-lg flex items-center justify-center text-slate-500 hover:text-slate-200 hover:bg-slate-800 transition-colors"
          aria-label="close"
        >
          <CloseIcon />
        </button>
      </div>

      {/* Body */}
      <div className="flex-1 overflow-y-auto">

        {/* Stub warning */}
        {isStub && (
          <div className="mx-4 mt-4 px-3 py-2 rounded-lg bg-amber-950/40 border border-amber-800/40 text-amber-400/80 text-[11px] leading-relaxed">
            ⚠ AI not configured — responses are placeholder stubs. Set a provider API key for real analysis.
          </div>
        )}

        {/* Graph-grounded explanation */}
        <div className="px-5 py-4 border-b border-[#1a2540]">
          <div className="text-[10px] uppercase tracking-widest text-slate-600 font-semibold mb-2">
            Graph-grounded explanation
          </div>
          <p className="text-sm text-slate-300 leading-relaxed whitespace-pre-wrap">
            {isStub ? explanation.replace(/^\(stub\)\s*/, "") : explanation}
          </p>
        </div>

        {/* Structured execution path */}
        <ExecutionPath steps={executionSteps} />

        {/* Relevant functions */}
        {visibleNodes.length > 0 && (
          <div className="px-5 py-4">
            <div className="text-[10px] uppercase tracking-widest text-slate-600 font-semibold mb-3">
              Relevant code
            </div>
            <ul className="space-y-2">
              {visibleNodes.map((n, i) => (
                <li
                  key={n.id}
                  className="group rounded-lg bg-[#0f1629] border border-[#1a2540] hover:border-[#2a3f6a] p-3 transition-colors fade-in"
                  style={{ animationDelay: `${i * 30}ms` }}
                >
                  <div className="flex items-start gap-2">
                    <span className="text-indigo-400 mt-0.5 shrink-0"><FnIcon /></span>
                    <div className="min-w-0">
                      <div className="font-mono text-xs text-white truncate">{n.name}</div>
                      <div className="text-[10px] text-slate-500 truncate mt-0.5">{n.file_path}</div>
                      {n.summary && !n.summary.startsWith("(stub)") && (
                        <div className="text-[11px] text-slate-400 mt-1.5 line-clamp-2 leading-relaxed">
                          {n.summary}
                        </div>
                      )}
                    </div>
                  </div>
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>

      {/* Footer */}
      <div className="shrink-0 border-t border-[#1a2540] px-4 py-3 space-y-2.5">
        <button
          onClick={handleCopy}
          className="w-full flex items-center justify-center gap-2 text-xs font-medium rounded-lg px-3 py-2 bg-[#0f1629] border border-[#1e2d4a] hover:border-blue-600/50 hover:text-blue-400 text-slate-300 transition-colors"
        >
          <CopyIcon />
          {copied ? "✓ Copied to clipboard!" : "Copy context for LLM"}
        </button>

        {queryId && (
          <div className="flex items-center gap-2">
            <span className="text-slate-600 text-xs flex-1">Helpful?</span>
            {[
              { r: "helpful",   e: "👍", active: "bg-emerald-900/60 border-emerald-700 text-emerald-300" },
              { r: "neutral",   e: "😐", active: "bg-slate-800 border-slate-600 text-slate-300" },
              { r: "unhelpful", e: "👎", active: "bg-rose-900/60 border-rose-800 text-rose-300" },
            ].map(({ r, e, active }) => (
              <button
                key={r}
                onClick={() => handleFeedback(r)}
                className={`flex-1 text-xs rounded-lg py-1 border transition-colors ${
                  feedback === r
                    ? active
                    : "bg-[#0f1629] border-[#1e2d4a] text-slate-500 hover:text-slate-300"
                }`}
              >
                {e}
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
