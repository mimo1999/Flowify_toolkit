import React from "react";
import { useFlowStore } from "../store.js";

export default function Sidebar() {
  const { repoPath, setRepoPath, ingest, update, graphId, loading, error, nodes, expanded, viewHistory, goBack, resetView } =
    useFlowStore();

  const moduleCount = Object.values(nodes).filter((n) => n.kind === "module").length;
  const visibleCount = Object.keys(nodes).length;
  const expandedCount = Object.keys(expanded).length;
  const canGoBack = viewHistory.length > 0;

  return (
    <aside className="w-72 bg-slate-900 border-r border-slate-800 p-4 flex flex-col gap-4 text-sm">
      <div>
        <h1 className="text-lg font-semibold tracking-tight">Flowify AI</h1>
        <p className="text-xs text-slate-500 mt-0.5">Click a module to expand its files; click a file to expand its functions.</p>
      </div>

      <label className="flex flex-col gap-1">
        <span className="text-slate-400 text-xs uppercase tracking-wide">Repository</span>
        <input
          className="bg-slate-950 border border-slate-700 rounded px-2 py-1.5 focus:outline-none focus:border-sky-500"
          value={repoPath}
          onChange={(e) => setRepoPath(e.target.value)}
          placeholder="/abs/path/to/repo"
        />
      </label>

      <div className="flex gap-2">
        <button onClick={ingest} disabled={loading || !repoPath}
          className="flex-1 bg-emerald-600 hover:bg-emerald-500 rounded px-2 py-1.5 disabled:opacity-50 font-medium">
          {loading ? "…" : graphId ? "Re-ingest" : "Ingest"}
        </button>
        <button onClick={update} disabled={!graphId || loading}
          className="flex-1 bg-sky-600 hover:bg-sky-500 rounded px-2 py-1.5 disabled:opacity-50 font-medium">
          Update
        </button>
      </div>

      {graphId && (
        <div className="flex gap-2">
          <button
            onClick={goBack}
            disabled={!canGoBack}
            className="flex-1 bg-slate-700 hover:bg-slate-600 rounded px-2 py-1.5 disabled:opacity-30 disabled:cursor-not-allowed font-medium text-xs"
            title="Go back to previous view"
          >
            ← Back
          </button>
          <button
            onClick={resetView}
            disabled={loading}
            className="flex-1 bg-slate-700 hover:bg-slate-600 rounded px-2 py-1.5 disabled:opacity-50 font-medium text-xs"
            title="Reset to initial view"
          >
            ⟲ Reset
          </button>
        </div>
      )}

      {graphId && (
        <div className="bg-slate-950 border border-slate-800 rounded p-2 text-xs space-y-1">
          <div className="flex justify-between"><span className="text-slate-500">graph_id</span>
            <span className="font-mono text-slate-300">{graphId.slice(0, 8)}…</span></div>
          <div className="flex justify-between"><span className="text-slate-500">modules</span>
            <span className="text-slate-200">{moduleCount}</span></div>
          <div className="flex justify-between"><span className="text-slate-500">visible nodes</span>
            <span className="text-slate-200">{visibleCount}</span></div>
          <div className="flex justify-between"><span className="text-slate-500">expanded</span>
            <span className="text-slate-200">{expandedCount}</span></div>
        </div>
      )}

      <div className="text-xs text-slate-500 leading-relaxed">
        <div className="flex items-center gap-2 mb-1">
          <span className="inline-block w-3 h-3 rounded-sm bg-blue-900 border border-blue-500" /> module
        </div>
        <div className="flex items-center gap-2 mb-1">
          <span className="inline-block w-3 h-3 rounded-sm bg-slate-950 border border-slate-600" /> file / function
        </div>
        <div className="flex items-center gap-2 mb-1">
          <span className="inline-block w-6 h-0.5 bg-sky-500" /> FLOW
        </div>
        <div className="flex items-center gap-2 mb-1">
          <span className="inline-block w-6 h-0.5 bg-slate-400" /> CALLS
        </div>
        <div className="flex items-center gap-2">
          <span className="inline-block w-6 border-t border-dashed border-slate-500" /> CONTAINS
        </div>
      </div>

      <div className="flex-1" />

      {error && <div className="text-xs text-rose-400 break-words bg-rose-950/40 border border-rose-900 p-2 rounded">{error}</div>}
    </aside>
  );
}
