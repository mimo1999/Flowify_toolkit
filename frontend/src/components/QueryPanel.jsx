import React, { useState, useRef } from "react";
import { useFlowStore } from "../store.js";

const SearchIcon = () => (
  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
    <circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>
  </svg>
);

export default function QueryPanel() {
  const [q, setQ]     = useState("");
  const runQuery      = useFlowStore((s) => s.runQuery);
  const loading       = useFlowStore((s) => s.loading);
  const explanation   = useFlowStore((s) => s.explanation);
  const inputRef      = useRef(null);

  const submit = () => {
    if (q.trim() && !loading) runQuery(q.trim());
  };

  return (
    <div className="shrink-0 px-4 py-3 border-t border-[#1a2540] bg-[#0a0f1a]">
      <div
        className={`flex items-center gap-3 bg-[#0f1629] rounded-xl border px-4 py-2.5 transition-colors ${
          explanation ? "border-amber-700/40" : "border-[#1e2d4a] focus-within:border-blue-600/60"
        }`}
      >
        <span className="text-slate-500 shrink-0"><SearchIcon /></span>
        <input
          ref={inputRef}
          className="flex-1 bg-transparent text-sm text-slate-200 placeholder:text-slate-600 focus:outline-none"
          placeholder="Ask anything about this codebase…"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && submit()}
        />
        {explanation && (
          <span className="text-[10px] text-amber-500/80 shrink-0 hidden sm:block">Results →</span>
        )}
        <button
          onClick={submit}
          disabled={loading || !q.trim()}
          className="shrink-0 bg-blue-600 hover:bg-blue-500 disabled:opacity-40 disabled:cursor-not-allowed rounded-lg px-3 py-1 text-xs font-medium text-white transition-colors"
        >
          {loading ? "…" : "Ask"}
        </button>
      </div>
      <div className="mt-1.5 px-1 text-[10px] text-slate-700">
        Press Enter to search · results appear in the panel →
      </div>
    </div>
  );
}
