import React, { useState } from "react";
import { useFlowStore } from "../store.js";

export default function QueryPanel() {
  const [q, setQ] = useState("");
  const runQuery  = useFlowStore((s) => s.runQuery);
  const loading   = useFlowStore((s) => s.loading);

  const submit = () => { if (q.trim()) runQuery(q.trim()); };

  return (
    <div className="shrink-0 px-3 py-2 border-t border-slate-800 bg-slate-900 flex gap-2">
      <input
        className="flex-1 bg-slate-950 border border-slate-700 rounded px-3 py-1.5 text-sm focus:outline-none focus:border-sky-500"
        placeholder="Ask: how does ingestion work?"
        value={q}
        onChange={(e) => setQ(e.target.value)}
        onKeyDown={(e) => e.key === "Enter" && submit()}
      />
      <button
        className="bg-amber-600 hover:bg-amber-500 px-4 py-1.5 rounded text-sm font-medium disabled:opacity-50 transition-colors"
        onClick={submit}
        disabled={loading || !q.trim()}
      >
        {loading ? "…" : "Ask"}
      </button>
    </div>
  );
}
