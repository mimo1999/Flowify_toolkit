import React, { useEffect, useRef, useState } from "react";
import mermaid from "mermaid";
import { useFlowStore } from "../store.js";

mermaid.initialize({
  startOnLoad: false,
  theme: "dark",
  securityLevel: "loose",
  flowchart: { curve: "basis", useMaxWidth: true },
  themeVariables: {
    background: "#0a0f1a",
    primaryColor: "#0f1629",
    primaryBorderColor: "#1e3a5a",
    primaryTextColor: "#e2e8f0",
    lineColor: "#3b82f6",
    fontSize: "13px",
  },
});

const CloseIcon = () => (
  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
    <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
  </svg>
);
const RefreshIcon = () => (
  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
    <polyline points="23,4 23,10 17,10" /><polyline points="1,20 1,14 7,14" />
    <path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15" />
  </svg>
);

// ── Rendered Mermaid diagram ──────────────────────────────────────────────────
let _mid = 0;
function Mermaid({ code }) {
  const ref = useRef(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let cancelled = false;
    if (!code) return;
    setFailed(false);
    const id = `flowify-mermaid-${_mid++}`;
    mermaid
      .render(id, code)
      .then(({ svg }) => { if (!cancelled && ref.current) ref.current.innerHTML = svg; })
      .catch(() => { if (!cancelled) setFailed(true); });
    return () => { cancelled = true; };
  }, [code]);

  if (failed) {
    return (
      <pre className="text-[11px] text-slate-400 bg-[#0b1220] border border-[#1a2540] rounded-lg p-3 overflow-x-auto whitespace-pre">
        {code}
      </pre>
    );
  }
  return <div ref={ref} className="flex justify-center overflow-x-auto [&_svg]:max-w-full" />;
}

// ── Section heading ───────────────────────────────────────────────────────────
function Section({ title, children }) {
  return (
    <div>
      <div className="text-[10px] uppercase tracking-widest text-blue-400/80 font-semibold mb-1.5">{title}</div>
      {children}
    </div>
  );
}

function Chips({ items, color }) {
  if (!items || items.length === 0) return <div className="text-slate-600 text-xs italic">—</div>;
  return (
    <div className="flex flex-wrap gap-1.5">
      {items.map((t) => (
        <span key={t} className={`text-[11px] px-2 py-0.5 rounded-full border ${color}`}>{t}</span>
      ))}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
export default function SummaryPanel() {
  const open      = useFlowStore((s) => s.summaryOpen);
  const close     = useFlowStore((s) => s.closeSummary);
  const summary   = useFlowStore((s) => s.flowSummary);
  const loading   = useFlowStore((s) => s.flowSummaryLoading);
  const error     = useFlowStore((s) => s.flowSummaryError);
  const fetchSummary = useFlowStore((s) => s.fetchFlowSummary);

  useEffect(() => {
    const onKey = (e) => e.key === "Escape" && close();
    if (open) window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, close]);

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-40 flex items-center justify-center bg-black/60 backdrop-blur-sm p-6"
      onClick={close}
    >
      <div
        className="w-full max-w-2xl max-h-[88vh] flex flex-col bg-[#0a0f1a] border border-[#1a2540] rounded-2xl shadow-2xl overflow-hidden"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-center justify-between px-5 py-3.5 border-b border-[#1a2540] shrink-0">
          <div className="flex items-center gap-2">
            <span className="text-sm">📄</span>
            <span className="text-sm font-semibold text-white">Repository Summary</span>
            {summary?.fallback_used && (
              <span className="text-[9px] px-1.5 py-0.5 rounded-full border border-amber-800/50 bg-amber-950/40 text-amber-400">
                heuristic
              </span>
            )}
          </div>
          <div className="flex items-center gap-1">
            <button
              onClick={() => fetchSummary(true)}
              disabled={loading}
              title="Regenerate summary"
              className="flex items-center gap-1 text-[10px] px-2 py-1 rounded text-slate-400 hover:text-slate-200 hover:bg-slate-800 disabled:opacity-40 transition-colors"
            >
              <RefreshIcon /> Regenerate
            </button>
            <button
              onClick={close}
              className="w-7 h-7 rounded flex items-center justify-center text-slate-500 hover:text-slate-200 hover:bg-slate-800 transition-colors"
            >
              <CloseIcon />
            </button>
          </div>
        </div>

        {/* Body */}
        <div className="flex-1 overflow-y-auto px-5 py-4 flex flex-col gap-5">
          {loading && (
            <div className="flex items-center justify-center gap-2 py-12 text-slate-500 text-sm">
              <svg className="animate-spin" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3">
                <path d="M12 2v4M12 18v4M4.93 4.93l2.83 2.83M16.24 16.24l2.83 2.83M2 12h4M18 12h4" />
              </svg>
              Generating summary…
            </div>
          )}

          {!loading && error && (
            <div className="text-xs text-rose-400 bg-rose-950/40 border border-rose-900/60 rounded-lg p-3 break-words">
              {error}
            </div>
          )}

          {!loading && !error && summary && (
            <>
              {summary.what && (
                <Section title="What it does">
                  <p className="text-sm text-slate-300 leading-relaxed">{summary.what}</p>
                </Section>
              )}

              {summary.usecase && (
                <Section title="Use case">
                  <p className="text-sm text-slate-300 leading-relaxed">{summary.usecase}</p>
                </Section>
              )}

              {summary.architecture_mermaid && (
                <Section title="Architecture">
                  <div className="rounded-lg bg-[#0b1220] border border-[#1a2540] p-3">
                    <Mermaid code={summary.architecture_mermaid} />
                  </div>
                </Section>
              )}

              {summary.flows?.length > 0 && (
                <Section title={`Flows (${summary.flows.length})`}>
                  <ol className="flex flex-col gap-2">
                    {summary.flows.map((f) => (
                      <li key={f.step} className="flex gap-2.5">
                        <span className="shrink-0 w-5 h-5 rounded-full bg-blue-900/40 border border-blue-700/50 text-blue-300 text-[10px] flex items-center justify-center font-mono mt-0.5">
                          {f.step}
                        </span>
                        <div className="min-w-0">
                          <span className="text-xs font-medium text-blue-300">{f.module}</span>
                          {f.description && (
                            <span className="text-xs text-slate-400"> — {f.description}</span>
                          )}
                        </div>
                      </li>
                    ))}
                  </ol>
                </Section>
              )}

              <div className="grid grid-cols-2 gap-5">
                <Section title="Tech stack">
                  <Chips items={summary.tech_stack} color="border-emerald-800/50 bg-emerald-950/30 text-emerald-300" />
                </Section>
                <Section title="Techniques">
                  <Chips items={summary.techniques} color="border-indigo-800/50 bg-indigo-950/30 text-indigo-300" />
                </Section>
              </div>
            </>
          )}

          {!loading && !error && !summary && (
            <div className="text-slate-500 text-sm text-center py-12">No summary available.</div>
          )}
        </div>
      </div>
    </div>
  );
}
