import React, { useState } from "react";
import { useFlowStore } from "../store.js";

export default function QueryPanel() {
  const [q, setQ] = useState("");
  const runQuery = useFlowStore((s) => s.runQuery);
  const explanation = useFlowStore((s) => s.explanation);
  const queryId = useFlowStore((s) => s.queryId);
  const sendFeedback = useFlowStore((s) => s.sendFeedback);
  const loading = useFlowStore((s) => s.loading);

  return (
    <div className="p-3 border-t border-slate-800 flex flex-col gap-2 bg-slate-900">
      <div className="flex gap-2">
        <input
          className="flex-1 bg-slate-950 border border-slate-700 rounded px-2 py-1 text-sm"
          placeholder="Ask: how does authentication work?"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && runQuery(q)}
        />
        <button
          className="bg-amber-600 hover:bg-amber-500 px-3 py-1 rounded text-sm disabled:opacity-50"
          onClick={() => runQuery(q)}
          disabled={loading}
        >
          Ask
        </button>
      </div>
      {explanation && (
        <>
          <div className="text-sm text-slate-200 max-h-48 overflow-auto whitespace-pre-wrap bg-slate-950 p-2 rounded border border-slate-800">
            {explanation}
          </div>
          {queryId && (
            <div className="flex gap-2 text-xs">
              <span className="text-slate-500 self-center">Was this helpful?</span>
              <button onClick={() => sendFeedback("helpful")}
                className="bg-emerald-700 hover:bg-emerald-600 px-2 py-0.5 rounded">👍</button>
              <button onClick={() => sendFeedback("neutral")}
                className="bg-slate-700 hover:bg-slate-600 px-2 py-0.5 rounded">😐</button>
              <button onClick={() => sendFeedback("unhelpful")}
                className="bg-rose-700 hover:bg-rose-600 px-2 py-0.5 rounded">👎</button>
            </div>
          )}
        </>
      )}
    </div>
  );
}
