import React, { useEffect } from "react";
import { useFlowStore } from "../store.js";
import { CloseIcon } from "./Inspector.jsx";

// A reviewer's progress on one branch, per browser: what they checked (by chunk content hash, so an
// edited chunk comes back), what they flagged, and the head they last looked at.
export const reviewRecordKey = (graphId, ref) => `flowify_review_${graphId}_${ref || "working-copy"}`;
const EMPTY = { checked: {}, flags: {}, head: null, total: 0 };
export function readRecord(key) {
  try { return { ...EMPTY, ...(JSON.parse(localStorage.getItem(key)) || {}) }; } catch { return { ...EMPTY }; }
}
export function writeRecord(key, rec) {
  try { localStorage.setItem(key, JSON.stringify(rec)); } catch { /* private window: keep it in memory */ }
}

const SEV = { critical: ["Critical", "#f87171"], high: ["High", "#fb923c"], medium: ["Medium", "#fbbf24"], low: ["Low", "#94a3b8"] };
const fmt = new Intl.NumberFormat();
const ago = (unix) => {
  const s = Math.max(0, Date.now() / 1000 - unix);
  if (s < 3600) return `${Math.max(1, Math.round(s / 60))} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
};
const mb = (b) => (b >= 1e9 ? `${(b / 1e9).toFixed(1)} GB` : `${Math.round(b / 1e6)} MB`);

function Badge({ worst }) {
  if (!worst) return <span className="w-24 shrink-0 text-xs text-slate-400">no scored change</span>;
  const [label, color] = SEV[worst.band] || SEV.low;
  return (
    <span className="w-24 shrink-0 flex items-center gap-1.5 font-semibold" style={{ color }}>
      <span aria-hidden="true" className="w-2 h-2 rounded-full" style={{ background: color }} />
      <span className="tabular-nums text-sm">{worst.score}</span><span className="text-xs">{label}</span>
    </span>
  );
}

function status(graphId, b) {
  const rec = readRecord(reviewRecordKey(graphId, b.ref));
  const seen = Object.keys(rec.checked).length + Object.keys(rec.flags).length;
  if (!rec.head && !seen) return { text: "not started", tone: "text-slate-400" };
  if (rec.head && !b.head.startsWith(rec.head.slice(0, 7)) && !rec.head.startsWith(b.head)) return { text: "new commits since you looked", tone: "text-amber-300" };
  if (rec.total && seen >= rec.total) return { text: "done", tone: "text-slate-300" };
  return { text: `in progress ${seen}/${rec.total || "?"}`, tone: "text-slate-300" };
}

export default function QueuePanel() {
  const graphId = useFlowStore((s) => s.graphId);
  const queue = useFlowStore((s) => s.queue);
  const loading = useFlowStore((s) => s.queueLoading);
  const error = useFlowStore((s) => s.queueError);
  const base = useFlowStore((s) => s.queueBase);
  const opening = useFlowStore((s) => s.openingRef);
  const load = useFlowStore((s) => s.loadQueue);
  const openBranch = useFlowStore((s) => s.openBranch);
  const openWorkingCopy = useFlowStore((s) => s.openWorkingCopy);
  const removeBranch = useFlowStore((s) => s.removeBranch);
  const close = useFlowStore((s) => s.closeReview);
  const [draft, setDraft] = React.useState(base);
  useEffect(() => { setDraft(base); }, [base]);
  useEffect(() => {
    const onKey = (e) => { if (e.key === "Escape" && !/^(INPUT|TEXTAREA)$/.test(e.target.tagName) && !document.querySelector('[role="dialog"]')) close(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [close]);

  const branches = queue?.branches || [];
  return (
    <section aria-label="Review queue" className="absolute inset-0 z-30 flex flex-col bg-[#0a0f1a]">
      <div className="flex items-center gap-3 px-4 py-3 border-b border-[#1a2540] shrink-0">
        <h2 className="text-base font-semibold text-white">Review queue</h2>
        <form onSubmit={(e) => { e.preventDefault(); load(draft); }} className="flex items-center gap-2 min-w-0">
          <label htmlFor="queue-base" className="text-xs text-slate-400 shrink-0">against</label>
          <input id="queue-base" value={draft} onChange={(e) => setDraft(e.target.value)} list="queue-bases" autoComplete="off" spellCheck={false} translate="no"
            className="w-32 min-w-0 bg-[#0f1629] border border-[#1e2d4a] rounded-lg px-2.5 py-1.5 text-sm font-mono text-slate-100 focus-visible:outline-none focus-visible:border-blue-500 focus-visible:ring-2 focus-visible:ring-blue-500/40" />
          <datalist id="queue-bases"><option value="main" /><option value="origin/main" /><option value="master" /></datalist>
          <button type="submit" disabled={loading} className="px-3 py-1.5 rounded-lg text-sm border border-[#1e2d4a] text-slate-200 hover:text-white hover:border-slate-500 disabled:opacity-40">{loading ? "Loading…" : "Refresh"}</button>
        </form>
        <button type="button" onClick={close} aria-label="Close review, back to the graph"
          className="ml-auto w-8 h-8 shrink-0 rounded-lg flex items-center justify-center text-slate-400 hover:text-white hover:bg-slate-800"><CloseIcon /></button>
      </div>

      <div className="flex-1 min-h-0 overflow-y-auto scroll-contain">
        <div className="max-w-4xl">
          <button type="button" onClick={openWorkingCopy}
            className="w-full text-left flex flex-wrap items-center gap-x-4 gap-y-1 px-4 py-3 border-b border-[#1a2540] hover:bg-[#111a30] focus-visible:outline focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-blue-500">
            <span className="min-w-0 flex-1">
              <span className="block text-sm font-semibold text-white">My working copy</span>
              <span className="block text-xs text-slate-400">Your own change, uncommitted edits included. Check it before you ask for review.</span>
            </span>
            <span className="text-sm text-blue-300">Check my change</span>
          </button>

          {error && <div role="alert" className="m-4 text-sm text-rose-300 bg-rose-950/40 border border-rose-900/60 rounded-lg p-2.5 break-words">{error}</div>}
          {loading && !queue && <p role="status" className="px-4 py-6 text-sm text-slate-400">Listing branches and scoring them…</p>}
          {queue && branches.length === 0 && (
            <div className="px-4 py-6 text-sm text-slate-300">
              <p>No branch has commits that <span translate="no" className="font-mono">{queue.base}</span> does not, from the last {queue.days} days.</p>
              {queue.days < 365 && (
                <button type="button" onClick={() => load(queue.base, 365)} className="mt-2 text-sm text-blue-300 hover:text-blue-200 underline underline-offset-2">Include branches from the last year</button>
              )}
            </div>
          )}
          {branches.length > 0 && (
            <>
              <h3 className="px-4 pt-4 pb-1 text-xs font-semibold text-slate-400">Branches ahead of <span translate="no" className="font-mono">{queue.base}</span>, riskiest change first</h3>
              <ul>
                {branches.map((b) => {
                  const st = status(graphId, b);
                  return (
                    <li key={b.ref} className="flex flex-wrap items-start gap-x-4 gap-y-1 px-4 py-3 border-b border-[#131c31]">
                      <Badge worst={b.worst} />
                      <span className="min-w-0 flex-1 basis-56">
                        <span translate="no" className="block font-mono text-[13px] text-white truncate">{b.ref}</span>
                        <span className="block text-xs text-slate-400 truncate">
                          {b.worst ? <>worst: <span translate="no" className="font-mono text-slate-300">{b.worst.name}</span> · </> : null}
                          {fmt.format(b.chunks)} changed function{b.chunks === 1 ? "" : "s"}{b.new_functions ? ` + ${b.new_functions} new` : ""} · <span className="tabular-nums">+{fmt.format(b.added)} −{fmt.format(b.removed)}</span>
                        </span>
                        <span className="block text-xs text-slate-400 truncate">{b.author} · {ago(b.when)} · <span className={st.tone}>{st.text}</span></span>
                      </span>
                      <span className="flex items-center gap-2 shrink-0">
                        <button type="button" onClick={() => openBranch(b.ref)} disabled={!!opening}
                          className="px-3 py-1.5 rounded-lg text-sm font-medium bg-blue-600 hover:bg-blue-500 text-white disabled:opacity-40">
                          {opening === b.ref ? "Opening…" : b.opened ? "Review" : "Open"}
                        </button>
                        {b.opened && (
                          <button type="button" onClick={() => removeBranch(b.ref)} aria-label={`Remove the checkout of ${b.ref}`}
                            className="text-xs text-slate-400 hover:text-white underline underline-offset-2">Remove</button>
                        )}
                      </span>
                    </li>
                  );
                })}
              </ul>
            </>
          )}
          {opening && <p role="status" className="px-4 py-3 text-sm text-slate-300">Checking out <span translate="no" className="font-mono">{opening}</span> and reading it. On a large repository this takes a few minutes the first time, seconds after that.</p>}
        </div>
      </div>

      {queue && (
        <p className="px-4 py-2 border-t border-[#1a2540] text-xs text-slate-400 shrink-0">
          Scores here are estimates from the loaded graph; opening a branch scores it from its own code.
          Opened branches use {mb(queue.disk_bytes)} (at most {queue.max_worktrees}, oldest removed first).
        </p>
      )}
    </section>
  );
}
