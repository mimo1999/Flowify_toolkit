import React, { useEffect } from "react";
import { useFlowStore } from "../store.js";
import { useDialog } from "./useDialog.js";

// Bumping this key re-shows the modal to everyone once, if the content ever
// changes enough to be worth re-surfacing — plain "seen" flags don't do that.
const SEEN_KEY = "flowify_onboarding_seen_v1";

const CloseIcon = () => (
  <svg aria-hidden="true" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
    <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
  </svg>
);

function Step({ n, children }) {
  return (
    <li className="flex gap-2.5">
      <span className="shrink-0 w-5 h-5 rounded-full bg-blue-900/40 border border-blue-700/50 text-blue-300 text-[11px] flex items-center justify-center font-mono mt-0.5">
        {n}
      </span>
      <span className="text-sm text-slate-300 leading-snug">{children}</span>
    </li>
  );
}

export default function WelcomeModal() {
  const helpOpen = useFlowStore((s) => s.helpOpen);
  const openHelp = useFlowStore((s) => s.openHelp);
  const closeHelp = useFlowStore((s) => s.closeHelp);

  // First-ever visit: open once, then remember so a refresh doesn't re-show it.
  useEffect(() => {
    let seen = false;
    try { seen = !!localStorage.getItem(SEEN_KEY); } catch { /* storage blocked: show once per load */ }
    if (!seen) openHelp();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const dismiss = () => {
    try { localStorage.setItem(SEEN_KEY, "1"); } catch { /* storage blocked */ }
    closeHelp();
  };

  const dialogRef = useDialog(helpOpen, dismiss);

  if (!helpOpen) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm p-6 scroll-contain"
      onClick={dismiss}
    >
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="welcome-title"
        tabIndex={-1}
        className="w-full max-w-md flex flex-col bg-[#0a0f1a] border border-[#1a2540] rounded-2xl shadow-2xl overflow-hidden"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-center justify-between px-5 py-3.5 border-b border-[#1a2540] shrink-0">
          <div className="flex items-center gap-2">
            <span aria-hidden="true" className="text-sm">👋</span>
            <h2 id="welcome-title" className="text-sm font-semibold text-white">Welcome to Flowify</h2>
          </div>
          <button
            onClick={dismiss}
            aria-label="Close Welcome Dialog"
            className="w-7 h-7 rounded flex items-center justify-center text-slate-500 hover:text-slate-200 hover:bg-slate-800 transition-colors"
          >
            <CloseIcon />
          </button>
        </div>

        {/* Body */}
        <div className="px-5 py-4 flex flex-col gap-4">
          <p className="text-sm text-slate-300 leading-relaxed">
            Flowify tells you where to look in a large change: it keeps a call graph of the whole codebase and ranks every changed function by how far a mistake in it can travel.
          </p>

          <div>
            <h3 className="text-[11px] uppercase tracking-widest text-blue-400/80 font-semibold mb-2">
              How to Use It
            </h3>
            <ol className="flex flex-col gap-2">
              <Step n={1}>Paste a repo path or git URL and choose <b>Analyse repository</b>.</Step>
              <Step n={2}>In <b>Review</b>, pick a branch from the queue, or check your own working copy before you ask for review.</Step>
              <Step n={3}>Work through <b>Must check</b> first: static checks, the diff and the callers it did not update. <b>Explore</b> has the graph, search and trace.</Step>
            </ol>
          </div>

          <div>
            <h3 className="text-[11px] uppercase tracking-widest text-blue-400/80 font-semibold mb-2">
              How to Read It
            </h3>
            <ul className="flex flex-col gap-1.5 text-sm text-slate-300 leading-snug">
              <li>Node size/glow = importance — bigger and brighter is more central.</li>
              <li><span aria-hidden="true">🔌 🗄️ 📤 📥</span> badges = API, database, event-emit, event-consume activity.</li>
              <li>Edge color = relationship type — full key under <b>How to read the graph</b> in the sidebar.</li>
            </ul>
          </div>

          <button
            onClick={dismiss}
            className="mt-1 w-full py-2 rounded-lg bg-blue-600 hover:bg-blue-500 text-white text-sm font-medium transition-colors"
          >
            Start Exploring
          </button>
        </div>
      </div>
    </div>
  );
}
