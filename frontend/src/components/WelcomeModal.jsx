import React, { useEffect } from "react";
import { useFlowStore } from "../store.js";

// Bumping this key re-shows the modal to everyone once, if the content ever
// changes enough to be worth re-surfacing — plain "seen" flags don't do that.
const SEEN_KEY = "flowify_onboarding_seen_v1";

const CloseIcon = () => (
  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
    <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
  </svg>
);

function Step({ n, children }) {
  return (
    <li className="flex gap-2.5">
      <span className="shrink-0 w-5 h-5 rounded-full bg-blue-900/40 border border-blue-700/50 text-blue-300 text-[10px] flex items-center justify-center font-mono mt-0.5">
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
    if (!localStorage.getItem(SEEN_KEY)) {
      openHelp();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const dismiss = () => {
    localStorage.setItem(SEEN_KEY, "1");
    closeHelp();
  };

  useEffect(() => {
    const onKey = (e) => e.key === "Escape" && dismiss();
    if (helpOpen) window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [helpOpen]);

  if (!helpOpen) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm p-6"
      onClick={dismiss}
    >
      <div
        className="w-full max-w-md flex flex-col bg-[#0a0f1a] border border-[#1a2540] rounded-2xl shadow-2xl overflow-hidden"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-center justify-between px-5 py-3.5 border-b border-[#1a2540] shrink-0">
          <div className="flex items-center gap-2">
            <span className="text-sm">👋</span>
            <span className="text-sm font-semibold text-white">Welcome to Flowify</span>
          </div>
          <button
            onClick={dismiss}
            className="w-7 h-7 rounded flex items-center justify-center text-slate-500 hover:text-slate-200 hover:bg-slate-800 transition-colors"
          >
            <CloseIcon />
          </button>
        </div>

        {/* Body */}
        <div className="px-5 py-4 flex flex-col gap-4">
          <p className="text-sm text-slate-300 leading-relaxed">
            Flowify turns a codebase into an explorable, queryable call graph — no reading required.
          </p>

          <div>
            <div className="text-[10px] uppercase tracking-widest text-blue-400/80 font-semibold mb-2">
              How to use it
            </div>
            <ol className="flex flex-col gap-2">
              <Step n={1}>Paste a repo path or git URL in the sidebar, then <b>Ingest</b>.</Step>
              <Step n={2}>Click ▸ on a node to expand it, or jump straight to a depth (Modules / Files / Functions).</Step>
              <Step n={3}>Ask a question in the bar at the bottom — the answer cites the exact functions it's grounded in.</Step>
            </ol>
          </div>

          <div>
            <div className="text-[10px] uppercase tracking-widest text-blue-400/80 font-semibold mb-2">
              How to read it
            </div>
            <ul className="flex flex-col gap-1.5 text-sm text-slate-300 leading-snug">
              <li>Node size/glow = importance — bigger and brighter is more central.</li>
              <li>🔌 🗄️ 📤 📥 badges = API, database, event-emit, event-consume activity.</li>
              <li>Edge color = relationship type — full key in the sidebar's <b>Legend</b>.</li>
            </ul>
          </div>

          <button
            onClick={dismiss}
            className="mt-1 w-full py-2 rounded-lg bg-blue-600 hover:bg-blue-500 text-white text-sm font-medium transition-colors"
          >
            Got it
          </button>
        </div>
      </div>
    </div>
  );
}
