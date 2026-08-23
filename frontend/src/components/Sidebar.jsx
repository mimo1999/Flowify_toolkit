import React, { useEffect, useState } from "react";
import { useFlowStore, API, apiFetch } from "../store.js";

// ── Icons (export) ────────────────────────────────────────────────────────────
const DownloadIcon = () => (
  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
    <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
    <polyline points="7,10 12,15 17,10"/><line x1="12" y1="15" x2="12" y2="3"/>
  </svg>
);
const CopyIcon = () => (
  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
    <rect x="9" y="9" width="13" height="13" rx="2" ry="2"/>
    <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>
  </svg>
);

// ── Icons ────────────────────────────────────────────────────────────────────
const RepoIcon = () => (
  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
    <path d="M3 3h18v18H3z" rx="2"/><path d="M3 9h18"/><path d="M9 21V9"/>
  </svg>
);
const BackIcon = () => (
  <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
    <polyline points="15,18 9,12 15,6"/>
  </svg>
);
const ResetIcon = () => (
  <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
    <polyline points="1,4 1,10 7,10"/>
    <path d="M3.51 15a9 9 0 1 0 .49-3.93"/>
  </svg>
);
const PowerIcon = () => (
  <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
    <path d="M18.36 6.64a9 9 0 1 1-12.73 0"/><line x1="12" y1="2" x2="12" y2="12"/>
  </svg>
);
const InsightsIcon = () => (
  <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
    <line x1="18" y1="20" x2="18" y2="10"/><line x1="12" y1="20" x2="12" y2="4"/>
    <line x1="6" y1="20" x2="6" y2="14"/>
  </svg>
);
const SummaryIcon = () => (
  <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
    <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
    <polyline points="14,2 14,8 20,8"/><line x1="8" y1="13" x2="16" y2="13"/>
    <line x1="8" y1="17" x2="16" y2="17"/><line x1="8" y1="9" x2="11" y2="9"/>
  </svg>
);

// ── Shutdown button (two-step confirm) ────────────────────────────────────────
function ShutdownButton() {
  const shutdownServers = useFlowStore((s) => s.shutdownServers);
  const [confirming, setConfirming] = useState(false);

  useEffect(() => {
    if (!confirming) return;
    const t = setTimeout(() => setConfirming(false), 3000);
    return () => clearTimeout(t);
  }, [confirming]);

  return (
    <button
      onClick={() => (confirming ? shutdownServers() : setConfirming(true))}
      title="Stop the backend and frontend dev servers"
      className={`w-full flex items-center justify-center gap-1.5 px-3 py-2 rounded-lg text-xs font-medium border transition-colors ${
        confirming
          ? "border-rose-600 bg-rose-600 text-white hover:bg-rose-500"
          : "border-rose-900/60 bg-rose-950/30 text-rose-400 hover:text-rose-300 hover:border-rose-700"
      }`}
    >
      <PowerIcon />
      {confirming ? "Click again to confirm" : "Shut down servers"}
    </button>
  );
}

// ── Full-screen overlay shown once servers are stopped ────────────────────────
function StoppedOverlay() {
  return (
    <div className="fixed inset-0 z-50 flex flex-col items-center justify-center gap-3 bg-[#070b14]/95 backdrop-blur-sm text-center px-6">
      <div className="w-12 h-12 rounded-full border border-rose-800/60 bg-rose-950/40 flex items-center justify-center text-rose-400">
        <PowerIcon />
      </div>
      <div className="text-slate-200 text-sm font-medium">Servers stopped</div>
      <div className="text-slate-500 text-xs max-w-xs">
        The backend and frontend dev servers have been shut down. You can close
        this tab. Restart with <code className="text-slate-400">bash run.sh</code>.
      </div>
    </div>
  );
}

// ── Deployment config (server vs local mode) ──────────────────────────────────
// Drives two things: hiding the shutdown button (an unauthenticated
// process-kill switch — fine on a single-user local instance, not fine on a
// shared public one) and the repo-path input's placeholder/hint text.
function useServerConfig() {
  const [config, setConfig] = useState(null);
  useEffect(() => {
    apiFetch(`${API}/config`).then(r => r.json()).then(setConfig).catch(() => {});
  }, []);
  return config;
}

// ── Provider badge ────────────────────────────────────────────────────────────
function ProviderBadge() {
  const [info, setInfo] = useState(null);
  useEffect(() => {
    apiFetch(`${API}/provider_info`).then(r => r.json()).then(setInfo).catch(() => {});
  }, []);
  if (!info) return null;
  return (
    <div className={`flex items-center gap-1.5 text-[10px] px-2.5 py-1 rounded-full border w-fit ${
      info.is_stub
        ? "text-amber-400 border-amber-800/50 bg-amber-950/40"
        : "text-emerald-400 border-emerald-800/50 bg-emerald-950/40"
    }`}>
      <span className={`w-1.5 h-1.5 rounded-full ${info.is_stub ? "bg-amber-400" : "bg-emerald-400"}`} />
      {info.display_name}
    </div>
  );
}

// ── Stat row ──────────────────────────────────────────────────────────────────
function Stat({ label, value }) {
  return (
    <div className="flex justify-between items-center">
      <span className="text-slate-500 text-xs">{label}</span>
      <span className="text-slate-300 text-xs font-mono">{value}</span>
    </div>
  );
}

// ── Legend item ───────────────────────────────────────────────────────────────
function LegendItem({ children, color, dashed }) {
  return (
    <div className="flex items-center gap-2.5">
      <span
        className="inline-block w-6 shrink-0"
        style={{
          height: 2,
          background: dashed ? "transparent" : color,
          borderTop: dashed ? `2px dashed ${color}` : undefined,
        }}
      />
      <span className="text-slate-500 text-xs">{children}</span>
    </div>
  );
}

// ── Depth selector ────────────────────────────────────────────────────────────
function DepthSelector({ graphId }) {
  const currentDepth  = useFlowStore((s) => s.currentDepth);
  const loadDepthView = useFlowStore((s) => s.loadDepthView);
  const resetView     = useFlowStore((s) => s.resetView);

  const levels = [
    { depth: 1, label: "Modules",   icon: "📦" },
    { depth: 2, label: "Files",     icon: "📄" },
    { depth: 3, label: "Functions", icon: "⚙️" },
  ];

  if (!graphId) return null;

  return (
    <div>
      <div className="text-[10px] uppercase tracking-widest text-slate-500 font-semibold mb-2">
        Jump to a level
      </div>
      <div className="flex gap-1">
        <button
          onClick={resetView}
          title="Interactive explore mode"
          className={`flex-1 flex flex-col items-center py-1.5 rounded-lg text-[10px] border transition-colors ${
            currentDepth === null
              ? "border-blue-500/80 bg-blue-900/30 text-blue-300"
              : "border-[#1e2d4a] text-slate-500 hover:text-slate-300 hover:border-slate-500"
          }`}
        >
          <span className="text-sm">🔍</span>
          <span>Explore</span>
        </button>
        {levels.map(({ depth, label, icon }) => (
          <button
            key={depth}
            onClick={() => loadDepthView(depth)}
            title={`View at ${label} level`}
            className={`flex-1 flex flex-col items-center py-1.5 rounded-lg text-[10px] border transition-colors ${
              currentDepth === depth
                ? "border-blue-500/80 bg-blue-900/30 text-blue-300"
                : "border-[#1e2d4a] text-slate-500 hover:text-slate-300 hover:border-slate-500"
            }`}
          >
            <span className="text-sm">{icon}</span>
            <span>{label}</span>
          </button>
        ))}
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
export default function Sidebar() {
  const {
    repoPath, setRepoPath, ingest, update,
    graphId, loading, error,
    nodes, expanded, viewHistory,
    goBack, resetView,
    exportGraph, exportStatus,
    serverStopped, openSummary, openInsights, downloadReport, openHelp,
  } = useFlowStore();
  const config = useServerConfig();
  const serverMode = config?.server_mode ?? false;

  const fileCount     = Object.values(nodes).filter(n => n.kind === "file").length;
  const fnCount       = Object.values(nodes).filter(n => n.kind !== "file" && n.kind !== "module").length;
  const expandedCount = Object.keys(expanded).length;
  const canGoBack     = viewHistory.length > 0;

  return (
    <aside className="w-72 shrink-0 flex flex-col bg-[#0a0f1a] border-r border-[#1a2540] overflow-hidden">

      {/* Brand */}
      <div className="px-5 pt-5 pb-4 border-b border-[#1a2540]">
        <div className="flex items-center gap-2.5 mb-1">
          <div className="w-7 h-7 rounded-lg bg-blue-600 flex items-center justify-center shrink-0">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="white">
              <circle cx="12" cy="12" r="3"/>
              <circle cx="5" cy="5" r="2.5"/><circle cx="19" cy="5" r="2.5"/>
              <circle cx="5" cy="19" r="2.5"/><circle cx="19" cy="19" r="2.5"/>
              <line x1="12" y1="9" x2="5" y2="5" stroke="white" strokeWidth="1.5"/>
              <line x1="12" y1="9" x2="19" y2="5" stroke="white" strokeWidth="1.5"/>
              <line x1="12" y1="15" x2="5" y2="19" stroke="white" strokeWidth="1.5"/>
              <line x1="12" y1="15" x2="19" y2="19" stroke="white" strokeWidth="1.5"/>
            </svg>
          </div>
          <div className="min-w-0 flex-1">
            <div className="font-semibold text-white text-sm leading-tight">Flowify AI</div>
            <div className="text-[10px] text-slate-500">Code graph explorer</div>
          </div>
          <button
            onClick={openHelp}
            title="What is this? How do I use it?"
            className="shrink-0 w-6 h-6 rounded-full flex items-center justify-center text-[11px] font-semibold text-slate-400 border border-[#1e2d4a] hover:text-slate-100 hover:border-blue-600 hover:bg-blue-950/40 transition-colors"
          >
            ?
          </button>
        </div>
        <div className="mt-3">
          <ProviderBadge />
        </div>
      </div>

      {/* Scrollable body */}
      <div className="flex-1 overflow-y-auto px-4 py-4 flex flex-col gap-4">

        {/* Repository input */}
        <div>
          <label className="block text-[10px] uppercase tracking-widest text-slate-500 font-semibold mb-2">
            {serverMode ? "GitHub / GitLab URL" : "Repository path or git URL"}
          </label>
          <input
            className="w-full bg-[#0f1629] border border-[#1e2d4a] rounded-lg px-3 py-2 text-sm text-slate-200 placeholder:text-slate-600 focus:outline-none focus:border-blue-500 transition-colors"
            placeholder={serverMode ? "https://github.com/owner/repo" : "/absolute/path/to/repo or https://github.com/owner/repo"}
            value={repoPath}
            onChange={(e) => setRepoPath(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && ingest()}
          />
          {serverMode && (
            <p className="mt-1.5 text-[10px] text-slate-500 leading-snug">
              Public repos only. Cloned, graphed, then deleted from the server —
              nothing is kept beyond the graph itself, which expires after 24h.
            </p>
          )}
        </div>

        {/* Action buttons */}
        <div className="flex gap-2">
          <button
            onClick={ingest}
            disabled={loading || !repoPath}
            className="flex-1 bg-blue-600 hover:bg-blue-500 disabled:opacity-40 disabled:cursor-not-allowed rounded-lg px-3 py-2 text-sm font-medium text-white transition-colors"
          >
            {loading ? (
              <span className="flex items-center justify-center gap-2">
                <svg className="animate-spin" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3">
                  <path d="M12 2v4M12 18v4M4.93 4.93l2.83 2.83M16.24 16.24l2.83 2.83M2 12h4M18 12h4"/>
                </svg>
                Loading…
              </span>
            ) : graphId ? "Re-ingest" : "Ingest"}
          </button>
          <button
            onClick={update}
            disabled={!graphId || loading}
            className="px-3 py-2 rounded-lg text-sm font-medium border border-[#1e2d4a] text-slate-400 hover:text-slate-200 hover:border-slate-500 disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
          >
            Sync
          </button>
        </div>

        {/* Navigation */}
        {graphId && (
          <div className="flex gap-2">
            <button
              onClick={goBack}
              disabled={!canGoBack}
              title="Undo the last navigation action — expand, collapse, level switch, or search"
              className="flex-1 flex items-center justify-center gap-1.5 px-3 py-1.5 rounded-lg text-xs border border-[#1e2d4a] text-slate-400 hover:text-slate-200 hover:border-slate-500 disabled:opacity-25 disabled:cursor-not-allowed transition-colors"
            >
              <BackIcon /> Back
            </button>
            <button
              onClick={resetView}
              disabled={loading}
              title="Reset to root view"
              className="flex-1 flex items-center justify-center gap-1.5 px-3 py-1.5 rounded-lg text-xs border border-[#1e2d4a] text-slate-400 hover:text-slate-200 hover:border-slate-500 disabled:opacity-25 disabled:cursor-not-allowed transition-colors"
            >
              <ResetIcon /> Reset
            </button>
          </div>
        )}

        {/* Repository summary */}
        {graphId && (
          <button
            onClick={openSummary}
            title="View the generated repository summary"
            className="w-full flex items-center justify-center gap-2 px-3 py-2 rounded-lg text-sm font-medium border border-blue-800/50 bg-blue-950/30 text-blue-300 hover:bg-blue-900/40 hover:border-blue-700 transition-colors"
          >
            <SummaryIcon /> Repository summary
          </button>
        )}

        {/* Graph insights (analytics) */}
        {graphId && (
          <button
            onClick={openInsights}
            title="God nodes, cycles, dead code, and cross-module couplings"
            className="w-full flex items-center justify-center gap-2 px-3 py-2 rounded-lg text-sm font-medium border border-purple-800/50 bg-purple-950/30 text-purple-300 hover:bg-purple-900/40 hover:border-purple-700 transition-colors"
          >
            <InsightsIcon /> Graph insights
          </button>
        )}

        {/* Drill-down view selector */}
        {graphId && <DepthSelector graphId={graphId} />}

        {/* Graph stats */}
        {graphId && (
          <div className="rounded-lg bg-[#0f1629] border border-[#1a2540] p-3 space-y-2">
            <div className="text-[10px] uppercase tracking-widest text-slate-600 font-semibold mb-2">Graph</div>
            <Stat label="ID" value={`${graphId.slice(0, 8)}…`} />
            <Stat label="Visible files" value={fileCount} />
            <Stat label="Visible symbols" value={fnCount} />
            <Stat label="Expanded" value={expandedCount} />
          </div>
        )}

        {/* Export */}
        {graphId && (
          <div>
            <div className="text-[10px] uppercase tracking-widest text-slate-500 font-semibold mb-2">
              Export graph
            </div>
            <div className="flex gap-2">
              <button
                onClick={() => exportGraph("json")}
                disabled={exportStatus === "downloading"}
                title="Download graph as JSON"
                className="flex-1 flex items-center justify-center gap-1.5 px-3 py-1.5 rounded-lg text-xs border border-[#1e2d4a] text-slate-400 hover:text-slate-200 hover:border-slate-500 disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
              >
                <DownloadIcon /> JSON
              </button>
              <button
                onClick={() => exportGraph("mermaid")}
                disabled={exportStatus === "downloading"}
                title="Copy Mermaid diagram to clipboard"
                className={`flex-1 flex items-center justify-center gap-1.5 px-3 py-1.5 rounded-lg text-xs border transition-colors ${
                  exportStatus === "copied"
                    ? "border-emerald-700 bg-emerald-950/40 text-emerald-400"
                    : "border-[#1e2d4a] text-slate-400 hover:text-slate-200 hover:border-slate-500 disabled:opacity-30 disabled:cursor-not-allowed"
                }`}
              >
                <CopyIcon />
                {exportStatus === "copied" ? "Copied!" : "Mermaid"}
              </button>
              <button
                onClick={downloadReport}
                disabled={exportStatus === "downloading"}
                title="Download the architecture report (GRAPH_REPORT.md)"
                className="flex-1 flex items-center justify-center gap-1.5 px-3 py-1.5 rounded-lg text-xs border border-[#1e2d4a] text-slate-400 hover:text-slate-200 hover:border-slate-500 disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
              >
                <DownloadIcon /> Report
              </button>
            </div>
            <button
              onClick={() => exportGraph("llm")}
              disabled={exportStatus === "downloading"}
              title="Copy the architecture report to your clipboard, ready to paste into ChatGPT/Claude/any LLM"
              className={`mt-2 w-full flex items-center justify-center gap-1.5 px-3 py-1.5 rounded-lg text-xs border transition-colors ${
                exportStatus === "copied"
                  ? "border-emerald-700 bg-emerald-950/40 text-emerald-400"
                  : "border-[#1e2d4a] text-slate-400 hover:text-slate-200 hover:border-slate-500 disabled:opacity-30 disabled:cursor-not-allowed"
              }`}
            >
              <CopyIcon />
              {exportStatus === "copied" ? "Copied!" : "Copy for LLM (no API key needed)"}
            </button>
          </div>
        )}

        {/* Legend */}
        <div className="rounded-lg bg-[#0f1629] border border-[#1a2540] p-3">
          <div className="text-[10px] uppercase tracking-widest text-slate-600 font-semibold mb-3">Legend</div>
          <div className="space-y-2">
            <div className="text-[9px] uppercase tracking-widest text-slate-700 font-semibold mt-1">Nodes</div>
            <div className="flex items-center gap-2.5">
              <span className="w-6 h-4 rounded shrink-0 bg-gradient-to-br from-blue-950 to-blue-900 border border-blue-800/60" />
              <span className="text-slate-500 text-xs">File</span>
            </div>
            <div className="flex items-center gap-2.5">
              <span className="w-6 h-4 rounded shrink-0 bg-gradient-to-br from-indigo-950 to-indigo-900 border border-indigo-800/60" />
              <span className="text-slate-500 text-xs">Function / Method</span>
            </div>
            <div className="flex items-center gap-2.5">
              <span className="w-6 h-4 rounded shrink-0 bg-gradient-to-br from-emerald-950 to-emerald-900 border border-emerald-800/60" />
              <span className="text-slate-500 text-xs">Class</span>
            </div>
            <div className="my-1 border-t border-[#1a2540]" />
            <div className="text-[9px] uppercase tracking-widest text-slate-700 font-semibold">Semantic roles</div>
            {[
              { icon: "🔌", color: "#10b981", label: "API endpoint (EXPOSES_API)" },
              { icon: "🗄️",  color: "#8b5cf6", label: "Database access (USES_DB)" },
              { icon: "📤", color: "#f59e0b", label: "Event emitter (EMITS_EVENT)" },
              { icon: "📥", color: "#ef4444", label: "Event consumer (CONSUMES_EVENT)" },
            ].map(({ icon, color, label }) => (
              <div key={label} className="flex items-center gap-2.5">
                <span className="text-sm w-6 shrink-0 text-center">{icon}</span>
                <span className="text-slate-500 text-[10px]">{label}</span>
              </div>
            ))}
            <div className="my-1 border-t border-[#1a2540]" />
            <div className="text-[9px] uppercase tracking-widest text-slate-700 font-semibold">Edges</div>
            <LegendItem color="#3b82f6">Calls</LegendItem>
            <LegendItem color="#8b5cf6">→ DB layer</LegendItem>
            <LegendItem color="#10b981">→ API layer</LegendItem>
            <LegendItem color="#f59e0b">→ Event emit</LegendItem>
            <LegendItem color="#1e3a5a" dashed>Contains</LegendItem>
          </div>
        </div>
      </div>

      {/* Error */}
      {error && (
        <div className="mx-4 mb-4 text-xs text-rose-400 bg-rose-950/40 border border-rose-900/60 rounded-lg p-2.5 break-words">
          {error}
        </div>
      )}

      {/* Footer — shut down servers (local mode only; the backend rejects
          POST /shutdown with 404 in server mode, so hide the affordance too) */}
      {!serverMode && (
        <div className="px-4 py-3 border-t border-[#1a2540] shrink-0">
          <ShutdownButton />
        </div>
      )}

      {serverStopped && <StoppedOverlay />}
    </aside>
  );
}
