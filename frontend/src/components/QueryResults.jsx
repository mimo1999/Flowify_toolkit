import React, { useState } from "react";
import { useFlowStore } from "../store.js";

export default function QueryResults() {
  const explanation = useFlowStore((s) => s.explanation);
  const queryNodes  = useFlowStore((s) => s.queryNodes);
  const queryId     = useFlowStore((s) => s.queryId);
  const clearQuery  = useFlowStore((s) => s.clearQuery);
  const sendFeedback = useFlowStore((s) => s.sendFeedback);
  const [copied, setCopied]   = useState(false);
  const [feedback, setFeedback] = useState(null);

  if (!explanation) return null;

  // Build a plain-text context block suitable for pasting into an LLM chat
  const buildContext = () => {
    const lines = ["## Relevant code context\n"];
    queryNodes.slice(0, 15).forEach((n) => {
      lines.push(`### ${n.name} (${n.file_path})`);
      if (n.summary) lines.push(n.summary);
      if (n.code_snippet) lines.push("```\n" + n.code_snippet + "\n```");
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

  return (
    <div
      className="
        absolute top-0 right-0 h-full w-80 z-10
        bg-slate-900/95 backdrop-blur-sm border-l border-slate-700
        flex flex-col shadow-2xl
        animate-slide-in-right
      "
    >
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-3 border-b border-slate-700 shrink-0">
        <span className="text-xs font-semibold uppercase tracking-wide text-slate-400">Query results</span>
        <button
          onClick={clearQuery}
          className="text-slate-500 hover:text-slate-200 text-lg leading-none"
          aria-label="close results"
        >×</button>
      </div>

      <div className="flex-1 overflow-y-auto">
        {/* AI explanation */}
        <div className="px-4 py-3 border-b border-slate-800">
          <div className="text-[11px] uppercase tracking-wide text-slate-500 mb-1.5">Explanation</div>
          <p className="text-sm text-slate-200 leading-relaxed whitespace-pre-wrap">{explanation}</p>
        </div>

        {/* Relevant nodes */}
        {queryNodes.length > 0 && (
          <div className="px-4 py-3">
            <div className="text-[11px] uppercase tracking-wide text-slate-500 mb-2">
              Relevant functions ({Math.min(queryNodes.length, 15)})
            </div>
            <ul className="space-y-2">
              {queryNodes.slice(0, 15).map((n) => (
                <li key={n.id} className="bg-slate-800/60 rounded p-2">
                  <div className="font-mono text-xs text-sky-400 truncate">{n.name}</div>
                  <div className="text-[10px] text-slate-500 truncate">{n.file_path}</div>
                  {n.summary && (
                    <div className="text-[11px] text-slate-300 mt-1 line-clamp-2">{n.summary}</div>
                  )}
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>

      {/* Footer: copy + feedback */}
      <div className="shrink-0 border-t border-slate-700 px-4 py-3 space-y-2">
        <button
          onClick={handleCopy}
          className="w-full text-xs bg-sky-700 hover:bg-sky-600 rounded px-3 py-1.5 font-medium transition-colors"
        >
          {copied ? "✓ Copied!" : "Copy context for Claude / LLM"}
        </button>

        {queryId && (
          <div className="flex items-center gap-1.5">
            <span className="text-slate-500 text-xs flex-1">Helpful?</span>
            {[
              { rating: "helpful",   emoji: "👍", active: "bg-emerald-700" },
              { rating: "neutral",   emoji: "😐", active: "bg-slate-600"  },
              { rating: "unhelpful", emoji: "👎", active: "bg-rose-700"   },
            ].map(({ rating, emoji, active }) => (
              <button
                key={rating}
                onClick={() => handleFeedback(rating)}
                className={`px-2 py-0.5 rounded text-xs transition-colors ${
                  feedback === rating ? active : "bg-slate-800 hover:bg-slate-700"
                }`}
              >
                {emoji}
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
