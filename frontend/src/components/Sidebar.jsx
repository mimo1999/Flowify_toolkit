import React, { useEffect, useState } from "react";
import { useFlowStore, API, apiFetch } from "../store.js";
import { LANE_OVERVIEW_MIN, useLanes } from "./laneLayout.js";

// ── Icons (export) ────────────────────────────────────────────────────────────
const DownloadIcon = () => (
  <svg aria-hidden="true" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
    <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
    <polyline points="7,10 12,15 17,10"/><line x1="12" y1="15" x2="12" y2="3"/>
  </svg>
);
const CopyIcon = () => (
  <svg aria-hidden="true" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
    <rect x="9" y="9" width="13" height="13" rx="2" ry="2"/>
    <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>
  </svg>
);

// ── Icons ────────────────────────────────────────────────────────────────────
const RepoIcon = () => (
  <svg aria-hidden="true" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
    <path d="M3 3h18v18H3z" rx="2"/><path d="M3 9h18"/><path d="M9 21V9"/>
  </svg>
);
const BackIcon = () => (
  <svg aria-hidden="true" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
    <polyline points="15,18 9,12 15,6"/>
  </svg>
);
const ResetIcon = () => (
  <svg aria-hidden="true" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
    <polyline points="1,4 1,10 7,10"/>
    <path d="M3.51 15a9 9 0 1 0 .49-3.93"/>
  </svg>
);
const PowerIcon = () => (
  <svg aria-hidden="true" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
    <path d="M18.36 6.64a9 9 0 1 1-12.73 0"/><line x1="12" y1="2" x2="12" y2="12"/>
  </svg>
);
const InsightsIcon = () => (
  <svg aria-hidden="true" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
    <line x1="18" y1="20" x2="18" y2="10"/><line x1="12" y1="20" x2="12" y2="4"/>
    <line x1="6" y1="20" x2="6" y2="14"/>
  </svg>
);
const TraceIcon = () => (
  <svg aria-hidden="true" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
    <circle cx="5" cy="6" r="2.5"/><circle cx="19" cy="12" r="2.5"/><circle cx="7" cy="19" r="2.5"/>
    <path d="M7.5 6h6a4 4 0 0 1 4 4M17 14l-8 3.5"/>
  </svg>
);
const SummaryIcon = () => (
  <svg aria-hidden="true" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
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
      aria-live="polite"
      className={`w-full flex items-center justify-center gap-1.5 px-3 py-1.5 rounded-lg text-xs transition-colors ${
        confirming ? "bg-rose-600 text-white hover:bg-rose-500" : "text-slate-400 hover:text-rose-300"
      }`}
    >
      <PowerIcon />
      {confirming ? "Click again to stop both servers" : "Shut down servers"}
    </button>
  );
}

// ── Full-screen overlay shown once servers are stopped ────────────────────────
function StoppedOverlay() {
  return (
    <div role="alertdialog" aria-labelledby="stopped-title" className="fixed inset-0 z-50 flex flex-col items-center justify-center gap-3 bg-[#070b14]/95 backdrop-blur-sm text-center px-6">
      <div className="w-12 h-12 rounded-full border border-rose-800/60 bg-rose-950/40 flex items-center justify-center text-rose-400">
        <PowerIcon />
      </div>
      <div id="stopped-title" className="text-slate-200 text-sm font-medium">Servers Stopped</div>
      <div className="text-slate-500 text-xs max-w-xs">
        The backend and frontend dev servers have been shut down. You can close
        this tab. Restart with <code translate="no" className="text-slate-400">bash run.sh</code>.
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
    <div className="flex flex-col gap-1">
      <div className={`flex items-center gap-1.5 text-xs px-2.5 py-1 rounded-full border w-fit ${
        info.is_stub
          ? "text-amber-300 border-amber-800/50 bg-amber-950/40"
          : "text-emerald-300 border-emerald-800/50 bg-emerald-950/40"
      }`}>
        <span className={`w-1.5 h-1.5 rounded-full ${info.is_stub ? "bg-amber-400" : "bg-emerald-400"}`} />
        {info.is_stub ? "No AI model connected" : info.display_name}
      </div>
      {info.is_stub && <p className="text-xs text-slate-400 leading-snug">Graph and impact are exact; summaries and answers are basic.</p>}
    </div>
  );
}

const fmt = new Intl.NumberFormat();

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

// ── Find a function ───────────────────────────────────────────────────────────
// "I'm about to change X": type a name, pick it, and the Impact panel shows who depends on it.
function FindFunction({ graphId }) {
  const selectNode = useFlowStore((s) => s.revealFunction);
  const [q, setQ] = useState("");
  const [found, setFound] = useState([]);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    const term = q.trim();
    if (term.length < 2) { setFound([]); setFailed(false); return undefined; }
    let live = true;
    const t = setTimeout(() => {
      apiFetch(`${API}/trace_starts?graph_id=${graphId}&q=${encodeURIComponent(term)}&limit=8`)
        .then((r) => (r.ok ? r.json() : Promise.reject(r.status)))
        .then((d) => { if (live) { setFound(d.starts || []); setFailed(false); } })
        .catch(() => { if (live) setFailed(true); });
    }, 250);
    return () => { live = false; clearTimeout(t); };
  }, [q, graphId]);

  return (
    <div>
      <label htmlFor="find-function" className="block text-xs font-semibold text-slate-400 mb-2">
        Find a function
      </label>
      <input
        id="find-function"
        type="search"
        value={q}
        onChange={(e) => setQ(e.target.value)}
        placeholder="e.g. retrieve_subgraph"
        autoComplete="off"
        spellCheck={false}
        className="w-full rounded-lg bg-[#0a0f1a] border border-[#1e2d4a] px-3 py-2 text-sm text-slate-100 placeholder:text-slate-500 focus-visible:outline focus-visible:outline-2 focus-visible:outline-blue-500"
      />
      {failed && <div role="alert" className="text-[11px] text-rose-400 mt-1">Search failed. Try again.</div>}
      {found.length > 0 && (
        <ul aria-label="Matching functions" className="mt-1 rounded-lg border border-[#1a2540] bg-[#0a0f1a] max-h-56 overflow-y-auto scroll-contain">
          {found.map((f) => (
            <li key={f.id}>
              <button
                type="button"
                onClick={() => { selectNode(f.id); setQ(""); setFound([]); }}
                className="w-full text-left px-2.5 py-1.5 hover:bg-[#111a30] border-b border-[#141d33] last:border-0"
              >
                <span className="block font-mono text-[11px] text-white/85 truncate" translate="no">{f.name}</span>
                <span className="block text-[11px] text-slate-400 truncate">{(f.file_path || "").split("/").slice(-2).join("/")}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
      {!failed && q.trim().length >= 2 && found.length === 0 && (
        <div role="status" className="text-[11px] text-slate-400 mt-1">No function with that name.</div>
      )}
    </div>
  );
}

// ── View switcher ─────────────────────────────────────────────────────────────
// Three ways to look at the same graph, plus Back/Reset for undoing navigation.
function ViewSwitcher() {
  const currentDepth  = useFlowStore((s) => s.currentDepth);
  const loadDepthView = useFlowStore((s) => s.loadDepthView);
  const resetView     = useFlowStore((s) => s.resetView);
  const goBack        = useFlowStore((s) => s.goBack);
  const canGoBack     = useFlowStore((s) => s.viewHistory.length > 0);
  const loading       = useFlowStore((s) => s.loading);

  const views = [
    { depth: null, label: "Start", hint: "The few entry points everything else hangs off. Click ▸ on a node to open it up", run: resetView },
    { depth: 1, label: "Areas", hint: "Groups found from how functions call each other, not from folders", run: () => loadDepthView(1) },
    { depth: 3, label: "Functions", hint: "The most-central functions and the calls between them", run: () => loadDepthView(3) },
  ];

  return (
    <section aria-labelledby="view-title">
      <div className="flex items-center justify-between mb-2">
        <h3 id="view-title" className={SECTION}>View</h3>
        <div className="flex gap-1">
          <button onClick={goBack} disabled={!canGoBack} title="Undo the last expand, collapse or view change" aria-label="Back"
            className="p-1.5 rounded-md text-slate-400 hover:text-white hover:bg-slate-800 disabled:opacity-30 disabled:hover:bg-transparent transition-colors"><BackIcon /></button>
          <button onClick={resetView} disabled={loading} title="Back to the starting view" aria-label="Reset view"
            className="p-1.5 rounded-md text-slate-400 hover:text-white hover:bg-slate-800 disabled:opacity-30 disabled:hover:bg-transparent transition-colors"><ResetIcon /></button>
        </div>
      </div>
      <div role="group" aria-labelledby="view-title" className="flex rounded-lg border border-[#1e2d4a] overflow-hidden">
        {views.map(({ depth, label, hint, run }) => (
          <button
            key={label}
            onClick={run}
            title={hint}
            aria-pressed={currentDepth === depth}
            className={`flex-1 py-1.5 text-sm border-r border-[#1e2d4a] last:border-0 transition-colors ${
              currentDepth === depth ? "bg-blue-600/25 text-white font-medium" : "text-slate-300 hover:bg-slate-800"
            }`}
          >
            {label}
          </button>
        ))}
      </div>
    </section>
  );
}

const SECTION = "text-xs font-semibold text-slate-400";

// One row per analysis tool, all the same weight: the old stack of three
// differently-coloured full-width buttons made everything look equally primary.
function ToolRow({ icon, title, hint, onClick }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="w-full flex items-start gap-3 px-3 py-2 rounded-lg text-left hover:bg-slate-800/70 transition-colors group"
    >
      <span className="mt-0.5 text-slate-400 group-hover:text-blue-300">{icon}</span>
      <span className="min-w-0">
        <span className="block text-sm text-slate-100">{title}</span>
        <span className="block text-xs text-slate-400 leading-snug">{hint}</span>
      </span>
    </button>
  );
}

// ── Lane rail ─────────────────────────────────────────────────────────────────
// Every lane of a many-lane graph as a list, most connected first — a plain,
// keyboard-friendly way to jump between lanes without hunting on the canvas.
function LaneRail() {
  const { lanes } = useLanes();
  const rootCount = useFlowStore((s) => s.rootIds.length);
  const focusLaneId = useFlowStore((s) => s.focusLaneId);
  const focusLane = useFlowStore((s) => s.focusLane);
  if (rootCount < LANE_OVERVIEW_MIN || lanes.length === 0) return null;
  return (
    <nav aria-labelledby="lane-rail-title">
      <h3 id="lane-rail-title" className={`${SECTION} mb-2`}>Lanes ({fmt.format(lanes.length)})</h3>
      <ul className="max-h-56 overflow-y-auto scroll-contain rounded-lg border border-[#1a2540] bg-[#0f1629] divide-y divide-[#141d33]">
        {lanes.map((l) => (
          <li key={l.id}>
            <button
              type="button"
              onClick={() => focusLane(l.id)}
              aria-current={focusLaneId === l.id ? "true" : undefined}
              className={`w-full flex items-center gap-2 px-3 py-1.5 text-left text-sm transition-colors ${
                focusLaneId === l.id ? "bg-blue-900/30 text-blue-200" : "text-slate-300 hover:bg-[#111a30]"
              }`}
            >
              <span aria-hidden="true" className="w-2 h-2 rounded-full shrink-0" style={{ background: `hsl(${l.hue} 80% 58%)` }} />
              <span translate="no" className="flex-1 truncate">{l.label}</span>
              <span className="shrink-0 text-slate-400 tabular-nums">{fmt.format(l.count)}</span>
            </button>
          </li>
        ))}
      </ul>
    </nav>
  );
}

// Collapsed by default: reference material, not something needed every minute.
function Disclosure({ title, children }) {
  return (
    <details className="group rounded-lg border border-[#1a2540]">
      <summary className="cursor-pointer select-none list-none flex items-center justify-between px-3 py-2 text-sm text-slate-300 hover:text-white">
        {title}
        <span aria-hidden="true" className="text-slate-500 group-open:rotate-90 transition-transform">▸</span>
      </summary>
      <div className="px-3 pb-3">{children}</div>
    </details>
  );
}

const repoName = (p) => (p || "").replace(/[\\/]+$/, "").split(/[\\/]/).pop() || p;

// ─────────────────────────────────────────────────────────────────────────────
export default function Sidebar() {
  const {
    repoPath, setRepoPath, ingest, update,
    graphId, loading, error,
    exportGraph, exportStatus,
    serverStopped, openSummary, openInsights, downloadReport, openHelp, openTrace, openReview, closeReview, reviewOpen,
  } = useFlowStore();
  const config = useServerConfig();
  const serverMode = config?.server_mode ?? false;
  // Review needs git on a local checkout; for one, it is the home screen.
  const canReview = !!config && !serverMode && !/^https?:\/\//i.test(repoPath || "");
  useEffect(() => {
    if (graphId && canReview) openReview();
  }, [graphId, canReview]);  // eslint-disable-line react-hooks/exhaustive-deps
  const [editingRepo, setEditingRepo] = useState(false);
  const showForm = !graphId || editingRepo;
  const exporting = exportStatus === "downloading";

  return (
    // On a phone the review takes the whole width; its close button leads back here.
    <aside className={`w-72 shrink-0 ${reviewOpen && canReview ? "hidden md:flex" : "flex"} flex-col bg-[#0a0f1a] border-r border-[#1a2540] overflow-hidden`}>

      {/* Brand */}
      <div className="px-4 pt-4 pb-3 border-b border-[#1a2540]">
        <div className="flex items-center gap-2.5">
          <div className="w-7 h-7 rounded-lg bg-blue-600 flex items-center justify-center shrink-0">
            <svg aria-hidden="true" width="14" height="14" viewBox="0 0 24 24" fill="white">
              <circle cx="12" cy="12" r="3"/>
              <circle cx="5" cy="5" r="2.5"/><circle cx="19" cy="5" r="2.5"/>
              <circle cx="5" cy="19" r="2.5"/><circle cx="19" cy="19" r="2.5"/>
              <line x1="12" y1="9" x2="5" y2="5" stroke="white" strokeWidth="1.5"/>
              <line x1="12" y1="9" x2="19" y2="5" stroke="white" strokeWidth="1.5"/>
              <line x1="12" y1="15" x2="5" y2="19" stroke="white" strokeWidth="1.5"/>
              <line x1="12" y1="15" x2="19" y2="19" stroke="white" strokeWidth="1.5"/>
            </svg>
          </div>
          <div translate="no" className="flex-1 font-semibold text-white text-sm">Flowify</div>
          <button
            onClick={openHelp}
            title="What is this? How do I use it?"
            aria-label="Help: what is Flowify?"
            className="shrink-0 w-7 h-7 rounded-full flex items-center justify-center text-xs font-semibold text-slate-300 border border-[#1e2d4a] hover:text-white hover:border-blue-600 hover:bg-blue-950/40 transition-colors"
          >
            ?
          </button>
        </div>
        <div className="mt-2.5"><ProviderBadge /></div>
      </div>

      {/* Body */}
      <div className="flex-1 overflow-y-auto px-4 py-4 flex flex-col gap-5">

        {/* Repository: a full form until a graph exists, then one line */}
        {showForm ? (
          <form onSubmit={(e) => { e.preventDefault(); if (!loading) { setEditingRepo(false); ingest(); } }} className="flex flex-col gap-3">
            <div>
              <label htmlFor="repo-input" className={`block ${SECTION} mb-2`}>
                {serverMode ? "GitHub or GitLab URL" : "Repository path or git URL"}
              </label>
              <input
                id="repo-input"
                name="repo"
                type={serverMode ? "url" : "text"}
                inputMode="url"
                autoComplete="off"
                spellCheck={false}
                aria-describedby={error ? "sidebar-error" : undefined}
                aria-invalid={error ? true : undefined}
                className="w-full bg-[#0f1629] border border-[#1e2d4a] rounded-lg px-3 py-2 text-sm text-slate-100 placeholder:text-slate-500 focus-visible:outline-none focus-visible:border-blue-500 focus-visible:ring-2 focus-visible:ring-blue-500/40 transition-colors"
                placeholder={serverMode ? "https://github.com/owner/repo…" : "/path/to/repo or https://github.com/owner/repo…"}
                value={repoPath}
                onChange={(e) => setRepoPath(e.target.value)}
              />
              {serverMode && (
                <p className="mt-1.5 text-xs text-slate-400 leading-snug">
                  Public repos only. Cloned, graphed, then deleted from the server; the graph itself expires after 24h.
                </p>
              )}
            </div>
            <div className="flex gap-2">
              <button
                type="submit"
                disabled={loading}
                className="flex-1 bg-blue-600 hover:bg-blue-500 disabled:opacity-40 disabled:cursor-not-allowed rounded-lg px-3 py-2 text-sm font-medium text-white transition-colors"
              >
                {loading ? "Analysing…" : graphId ? "Re-ingest" : "Analyse repository"}
              </button>
              {graphId && !loading && (
                <button type="button" onClick={() => setEditingRepo(false)} className="px-3 py-2 rounded-lg text-sm border border-[#1e2d4a] text-slate-300 hover:text-white hover:border-slate-500 transition-colors">
                  Cancel
                </button>
              )}
            </div>
            {error && (
              <div id="sidebar-error" role="alert" className="text-sm text-rose-300 bg-rose-950/40 border border-rose-900/60 rounded-lg p-2.5 break-words">
                {error}
              </div>
            )}
          </form>
        ) : (
          <div className="rounded-lg bg-[#0f1629] border border-[#1a2540] p-3">
            <div className="text-xs text-slate-400">Repository</div>
            <div translate="no" title={repoPath} className="font-mono text-sm text-white truncate">{repoName(repoPath)}</div>
            <div className="mt-2 flex gap-3 text-xs">
              <button onClick={() => setEditingRepo(true)} className="text-blue-300 hover:text-blue-200 underline-offset-2 hover:underline">Change</button>
              <button onClick={update} disabled={loading} title="Re-read only the files changed since the last ingest" className="text-blue-300 hover:text-blue-200 underline-offset-2 hover:underline disabled:opacity-40">
                {loading ? "Working…" : "Sync changes"}
              </button>
            </div>
          </div>
        )}

        {!showForm && error && (
          <div role="alert" className="text-sm text-rose-300 bg-rose-950/40 border border-rose-900/60 rounded-lg p-2.5 break-words">{error}</div>
        )}

        {graphId && canReview && (
          <div role="tablist" aria-label="Mode" className="grid grid-cols-2 gap-1 p-1 rounded-lg bg-[#0f1629] border border-[#1a2540]">
            {[["Review", true, "Rank what a change can break"], ["Explore", false, "The graph, search, trace and hotspots"]].map(([label, isReview, hint]) => (
              <button key={label} type="button" role="tab" aria-selected={reviewOpen === isReview} title={hint}
                onClick={() => (isReview ? openReview() : closeReview())}
                className={`py-1.5 rounded-md text-sm font-medium transition-colors ${reviewOpen === isReview ? "bg-blue-600 text-white" : "text-slate-300 hover:text-white"}`}>
                {label}
              </button>
            ))}
          </div>
        )}

        {/* Explore tools; the review screen has its own controls */}
        {graphId && !(canReview && reviewOpen) && (
          <>
            <FindFunction graphId={graphId} />
            <ViewSwitcher />
            <LaneRail />

            <section aria-labelledby="tools-title">
              <h3 id="tools-title" className={`${SECTION} mb-1`}>Analyse</h3>
              <div className="-mx-1 flex flex-col">
                <ToolRow icon={<SummaryIcon />} title="Repository summary" hint="What this codebase does, in plain English" onClick={openSummary} />
                <ToolRow icon={<InsightsIcon />} title="Hotspots and risks" hint="Central functions, cycles, unused code" onClick={openInsights} />
                <ToolRow icon={<TraceIcon />} title="Trace a path" hint="Follow a function's calls step by step" onClick={() => openTrace()} />
              </div>
            </section>

            <Disclosure title="Export">
              <div className="flex flex-col gap-1.5 text-sm">
                <button onClick={() => exportGraph("llm")} disabled={exporting} className="text-left text-slate-200 hover:text-white py-1 flex items-center gap-2"><CopyIcon /> <span aria-live="polite">{exportStatus === "copied" ? "Copied" : "Copy report for an AI chat"}</span></button>
                <button onClick={downloadReport} disabled={exporting} className="text-left text-slate-200 hover:text-white py-1 flex items-center gap-2"><DownloadIcon /> Download report (.md)</button>
                <button onClick={() => exportGraph("json")} disabled={exporting} className="text-left text-slate-200 hover:text-white py-1 flex items-center gap-2"><DownloadIcon /> Graph data (.json)</button>
                <button onClick={() => exportGraph("mermaid")} disabled={exporting} className="text-left text-slate-200 hover:text-white py-1 flex items-center gap-2"><CopyIcon /> Copy Mermaid diagram</button>
              </div>
            </Disclosure>
          </>
        )}

        {!(canReview && reviewOpen) && <Disclosure title="How to read the graph">
          <div className="flex flex-col gap-2 text-xs text-slate-300">
            <div className="flex items-center gap-2.5"><span className="w-6 h-4 rounded shrink-0 bg-indigo-900 border border-indigo-700/70" />Function or method</div>
            <div className="flex items-center gap-2.5"><span className="w-6 h-4 rounded shrink-0 bg-emerald-900 border border-emerald-700/70" />Class</div>
            <div className="flex items-center gap-2.5"><span className="w-6 h-4 rounded shrink-0 bg-blue-900 border border-blue-700/70" />File</div>
            <div className="border-t border-[#1a2540] pt-2 text-slate-400">Badges on a function</div>
            {[
              { icon: "🔌", label: "API endpoint" },
              { icon: "🗄️", label: "Database access" },
              { icon: "📤", label: "Emits an event" },
              { icon: "📥", label: "Consumes an event" },
            ].map(({ icon, label }) => (
              <div key={label} className="flex items-center gap-2.5"><span aria-hidden="true" className="w-6 text-center">{icon}</span>{label}</div>
            ))}
            <div className="border-t border-[#1a2540] pt-2 text-slate-400">Lines</div>
            <LegendItem color="#3b82f6">Calls</LegendItem>
            <LegendItem color="#8b5cf6">Calls into the database layer</LegendItem>
            <LegendItem color="#10b981">Calls into the API layer</LegendItem>
            <LegendItem color="#f59e0b">Emits an event</LegendItem>
            <LegendItem color="#1e3a5a" dashed>Contains</LegendItem>
            <p className="border-t border-[#1a2540] pt-2 text-slate-400 leading-snug">Bigger node = more central. Amber dots = importance (more dots, more depended on).</p>
          </div>
        </Disclosure>}
      </div>

      {/* Footer — local mode only; the backend rejects POST /shutdown in server mode */}
      {!serverMode && (
        <div className="px-4 py-2.5 border-t border-[#1a2540] shrink-0">
          <ShutdownButton />
        </div>
      )}

      {serverStopped && <StoppedOverlay />}
    </aside>
  );
}
