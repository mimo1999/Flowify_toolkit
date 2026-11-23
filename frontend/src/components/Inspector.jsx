import React, { useEffect, useState } from "react";
import { useFlowStore } from "../store.js";

const fmt = new Intl.NumberFormat();

export const CloseIcon = ({ size = 14 }) => (
  <svg aria-hidden="true" width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
    <line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>
  </svg>
);

// Plain-language risk: the label alone says nothing about *why*, so the verdict
// sentence below it always states the number it is based on.
export const RISK = {
  critical: { color: "#f87171", label: "Critical" },
  high:     { color: "#fb923c", label: "High" },
  medium:   { color: "#fbbf24", label: "Medium" },
  low:      { color: "#34d399", label: "Low" },
};

const SEMANTIC_LABEL = {
  EXPOSES_API: "API endpoint", USES_DB: "Database access",
  EMITS_EVENT: "Emits events", CONSUMES_EVENT: "Consumes events",
};

const MARKER_COLORS = {
  HACK: "text-orange-300", FIXME: "text-rose-300", BUG: "text-rose-300", XXX: "text-rose-300",
  WHY: "text-sky-300", TODO: "text-amber-300", NOTE: "text-slate-300",
  WARNING: "text-orange-300", OPTIMIZE: "text-emerald-300",
};

const SOURCE_LABELS = { ast: "AST", static_analysis: "Static", regex: "Regex", llm: "LLM", heuristic: "Heuristic", user: "User" };

const shortPath = (p) => (p || "").split("/").slice(-2).join("/");

function ProvenanceBadge({ provenance }) {
  if (!provenance) return null;
  const conf = Math.round((provenance.confidence ?? 1) * 100);
  return (
    <span
      title={provenance.reasoning || provenance.evidence || ""}
      className={`shrink-0 text-[11px] font-mono px-1.5 py-0.5 rounded border ${
        conf >= 85 ? "text-emerald-300 border-emerald-800/60 bg-emerald-950/40" : "text-amber-300 border-amber-800/60 bg-amber-950/40"
      }`}
    >
      {conf}% · {SOURCE_LABELS[provenance.source] || provenance.source}
    </span>
  );
}

// A caller/callee as one real button: click re-centres the canvas on it.
function FnRow({ node, onOpen, note }) {
  const sure = node.confidence != null && node.confidence < 0.9;
  return (
    <li>
      <button
        type="button"
        onClick={() => onOpen(node.id)}
        className="w-full text-left flex items-center gap-2 px-4 py-2 border-b border-[#141d33] hover:bg-[#111a30] focus-visible:outline focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-blue-500"
      >
        <span className="min-w-0 flex-1">
          <span translate="no" className="block font-mono text-[13px] text-slate-100 truncate">{node.name}</span>
          <span className="block text-xs text-slate-400 truncate">
            {shortPath(node.file_path)}{node.line ? `:${node.line}` : ""}
            {SEMANTIC_LABEL[node.semantic_kind] ? ` · ${SEMANTIC_LABEL[node.semantic_kind]}` : ""}
          </span>
        </span>
        {sure && (
          <span className="shrink-0 text-xs text-amber-300 tabular-nums" title={node.reason || "How sure the graph is that this call goes to this function"}>
            {Math.round(node.confidence * 100)}% sure
          </span>
        )}
        {note && <span className="shrink-0 text-xs text-amber-300">{note}</span>}
      </button>
    </li>
  );
}

function CopyLink({ graphId, nodeId }) {
  const [state, setState] = useState("");
  const copy = async () => {
    const url = `${window.location.origin}/?graph_id=${encodeURIComponent(graphId)}&node_id=${encodeURIComponent(nodeId)}`;
    try { await navigator.clipboard.writeText(url); setState("Copied"); } catch { setState("Copy failed"); }
    setTimeout(() => setState(""), 2000);
  };
  return (
    <button
      type="button"
      onClick={copy}
      title="Copy a link that opens this function (works for anyone on the same server)"
      className="shrink-0 px-3 py-2 rounded-lg text-sm border border-[#1e2d4a] text-slate-300 hover:text-white hover:border-slate-500 transition-colors"
    >
      <span aria-live="polite">{state || "Copy link"}</span>
    </button>
  );
}

// `hunks` = [[firstLine, lastLine], ...] changed in the diff under review, if any.
// `removed` = [{at, lines}] text the diff deleted, shown above line `at` (the "before" side).
// `callLine` = the one line to point at (a call site).
// `context` = show only lines within this many of a changed/removed/call line (a diff-style excerpt).
export function Code({ source, hunks, removed, callLine, context }) {
  if (!source) return <div role="status" className="px-4 py-6 text-sm text-slate-400">Loading source…</div>;
  if (!source.code) return <div className="px-4 py-6 text-sm text-slate-400">No source text was stored for this node.</div>;
  const start = source.start_line || 1;
  const lines = source.code.split(/\r?\n/);
  const changed = (n) => !!hunks?.some(([a, b]) => n >= a && n <= b);
  const last = start + lines.length - 1;
  const spots = [...(hunks || []), ...(removed || []).map((r) => [r.at, r.at]), ...(callLine ? [[callLine, callLine]] : [])];
  const near = (n) => context == null || !spots.length || n === start || spots.some(([a, b]) => n >= a - context && n <= b + context);
  // deleted text sits above the line that took its place; a deletion past the end goes last
  const gone = (n) => (removed || []).filter((r) => r.at === n || (n === last + 1 && r.at > last)).flatMap((r) => r.lines).map((l, k) => (
    <div key={`gone-${n}-${k}`} className="flex min-w-max bg-rose-500/10 shadow-[inset_3px_0_0_#fb7185]">
      <span aria-hidden="true" className="select-none shrink-0 w-12 pr-3 text-right text-rose-300">−</span>
      <span className="sr-only">removed: </span>
      <code className="whitespace-pre pr-4 text-rose-200/80">{l || " "}</code>
    </div>
  ));
  return (
    <div>
      {hunks?.length > 0 && (
        <p className="px-4 pt-3 text-xs text-amber-300">Lines changed in the diff under review are marked.</p>
      )}
      {source.summary && !source.summary.startsWith("(stub)") && (
        <p className="px-4 pt-3 text-sm text-slate-300 leading-snug">{source.summary}</p>
      )}
      <pre translate="no" tabIndex={0} aria-label="Function source" className="m-0 py-3 overflow-x-auto text-[12px] leading-[1.6] font-mono text-slate-200 focus-visible:outline focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-blue-500">
        {lines.map((l, i) => {
          const hit = changed(start + i);
          if (!near(start + i)) {
            // one marker per skipped run
            return near(start + i - 1) ? <div key={i} aria-hidden="true" className="pl-12 text-slate-500 select-none">⋯</div> : null;
          }
          const call = callLine === start + i;
          const tone = call ? "bg-sky-400/15 shadow-[inset_3px_0_0_#38bdf8]" : hit ? "bg-amber-400/10 shadow-[inset_3px_0_0_#fbbf24]" : "hover:bg-[#111a30]";
          return (
            <React.Fragment key={i}>
              {gone(start + i)}
              <div className={`flex min-w-max ${tone}`}>
                <span aria-hidden="true" className={`select-none shrink-0 w-12 pr-3 text-right tabular-nums ${call ? "text-sky-300" : hit ? "text-amber-300" : "text-slate-500"}`}>{start + i}</span>
                {call ? <span className="sr-only">the call: </span> : hit && <span className="sr-only">changed: </span>}
                <code className="whitespace-pre pr-4">{l || " "}</code>
              </div>
            </React.Fragment>
          );
        })}
        {gone(last + 1)}
      </pre>
      {source.truncated && <div className="px-4 pb-3 text-xs text-slate-400">Showing the first {lines.length} lines.</div>}
    </div>
  );
}

function Notes({ impact, refs }) {
  const docs = refs?.referenced_in || [];
  const notes = refs?.rationale || [];
  if (!docs.length && !notes.length && !impact.affected_modules?.length) {
    return <div className="px-4 py-6 text-sm text-slate-400">No docs or TODO/FIXME comments reference this function.</div>;
  }
  const H = ({ children }) => <h3 className="text-xs font-semibold text-slate-400 mb-2">{children}</h3>;
  return (
    <div className="px-4 py-3 flex flex-col gap-4">
      {impact.affected_modules?.length > 0 && (
        <section>
          <H>Areas that depend on this</H>
          <ul className="text-sm text-slate-200 flex flex-col gap-1">
            {impact.affected_modules.map((m) => <li key={m} className="truncate">{m}</li>)}
          </ul>
        </section>
      )}
      {notes.length > 0 && (
        <section>
          <H>Developer notes in the code</H>
          <ul className="flex flex-col gap-2">
            {notes.map((r) => (
              <li key={r.id} className="text-sm text-slate-300 leading-snug">
                <span className={`font-semibold mr-1.5 ${MARKER_COLORS[r.marker] || "text-slate-300"}`}>{r.marker}</span>{r.text}
              </li>
            ))}
          </ul>
        </section>
      )}
      {docs.length > 0 && (
        <section>
          <H>Mentioned in docs</H>
          <ul className="flex flex-col gap-2">
            {docs.map((d, i) => (
              <li key={i} className="flex items-center gap-2 min-w-0 text-sm" title={d.doc_path}>
                <span className="truncate text-cyan-300">
                  {d.document}{d.section ? <span className="text-slate-400"> › {d.section}</span> : null}
                </span>
                <ProvenanceBadge provenance={d.provenance} />
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}

export default function Inspector() {
  const impact = useFlowStore((s) => s.impactData);
  const loading = useFlowStore((s) => s.impactLoading);
  const refs = useFlowStore((s) => s.nodeRefs);
  const source = useFlowStore((s) => s.nodeSource);
  const selectedId = useFlowStore((s) => s.selectedId);
  const clearSelection = useFlowStore((s) => s.clearSelection);
  const openTrace = useFlowStore((s) => s.openTrace);
  const revealFunction = useFlowStore((s) => s.revealFunction);
  const graphId = useFlowStore((s) => s.graphId);
  // When this function is part of the change under review, mark its changed lines.
  const reviewOpen = useFlowStore((s) => s.reviewOpen);
  const reviewRow = useFlowStore((s) => (s.reviewOpen && s.review?.functions.find((f) => f.id === s.selectedId)) || null);
  const [tab, setTab] = useState("source");

  useEffect(() => { setTab("source"); }, [selectedId]);
  useEffect(() => {
    if (!selectedId) return undefined;
    const onKey = (e) => { if (e.key === "Escape" && !e.defaultPrevented && !document.querySelector('[role="dialog"]')) clearSelection(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [selectedId, clearSelection]);

  if (!selectedId || selectedId.startsWith("file::") || selectedId.startsWith("mod_")) return null;
  if (!impact && !loading) return null;

  const risk = impact ? RISK[impact.risk_level] || RISK.low : null;
  const callers = impact?.callers || [];
  const callees = impact?.callees || [];
  const noteCount = (refs?.rationale?.length || 0) + (refs?.referenced_in?.length || 0) + (impact?.affected_modules?.length || 0);
  const tabs = [
    { id: "source", label: "Source" },
    { id: "callers", label: "Callers", n: impact?.caller_count },
    { id: "calls", label: "Calls", n: callees.length },
    { id: "notes", label: "Notes", n: noteCount || undefined },
  ];
  const onTabKey = (e) => {
    const i = tabs.findIndex((t) => t.id === tab);
    const j = e.key === "ArrowRight" ? (i + 1) % tabs.length : e.key === "ArrowLeft" ? (i + tabs.length - 1) % tabs.length : -1;
    if (j < 0) return;
    e.preventDefault();
    setTab(tabs[j].id);
    document.getElementById(`insp-tab-${tabs[j].id}`)?.focus();
  };

  return (
    <aside aria-label="Function details" className="absolute top-0 right-0 h-full w-[420px] max-w-full z-20 flex flex-col bg-[#0a0f1a] border-l border-[#1a2540] shadow-[-4px_0_32px_rgba(0,0,0,0.5)] slide-in-right">
      {reviewOpen && (
        <button
          type="button"
          onClick={clearSelection}
          className="shrink-0 text-left px-4 py-2 text-sm text-blue-300 hover:text-blue-200 border-b border-[#1a2540]"
        >
          <span aria-hidden="true">← </span>Back to the review list
        </button>
      )}
      <div className="flex items-start gap-2 px-4 pt-4 pb-3 border-b border-[#1a2540] shrink-0">
        <div className="min-w-0 flex-1">
          <h2 translate="no" className="font-mono text-base font-semibold text-white truncate">{impact ? `${impact.node_name}()` : "Loading…"}</h2>
          {impact && <div translate="no" className="text-xs text-slate-400 truncate">{impact.file_path}{source?.start_line ? `:${source.start_line}` : ""}</div>}
        </div>
        <button
          onClick={clearSelection}
          aria-label="Close function details"
          className="shrink-0 w-8 h-8 rounded-lg flex items-center justify-center text-slate-400 hover:text-white hover:bg-slate-800 transition-colors"
        >
          <CloseIcon />
        </button>
      </div>

      {loading && <div role="status" className="px-4 py-8 text-sm text-slate-400">Analysing impact…</div>}

      {impact && !loading && (
        <>
          <div className="px-4 py-3 border-b border-[#1a2540] shrink-0 flex flex-col gap-3">
            <div>
              <div className="flex items-center gap-2">
                <span className="text-sm font-semibold" style={{ color: risk.color }}>{risk.label} change risk</span>
                {SEMANTIC_LABEL[impact.semantic_kind] && <span className="text-xs text-slate-300 border border-[#1e2d4a] rounded px-1.5 py-0.5">{SEMANTIC_LABEL[impact.semantic_kind]}</span>}
              </div>
              <p className="text-sm text-slate-300 leading-snug mt-1">
                {impact.upstream_count > 0
                  ? <>Changing this can affect <b className="text-white">{fmt.format(impact.upstream_count)}</b> {impact.upstream_count === 1 ? "function" : "functions"} ({fmt.format(impact.caller_count)} call it directly).</>
                  : impact.semantic_kind === "EXPOSES_API"
                    ? "Nothing in the repository calls this; it is reached from outside, as an API endpoint."
                    : "No function in the graph calls this."}
                {impact.possible_caller_count > 0 && <> <span className="text-amber-300">{fmt.format(impact.possible_caller_count)} more may call it but could not be confirmed, so this may be understated.</span></>}
                {impact.name_shared > 0 && <> <span className="text-amber-300">{fmt.format(impact.name_shared)} functions share this name, so calls made by name alone could not be checked.</span></>}
              </p>
              {reviewRow && !reviewRow.tested && (
                <p className="text-sm text-amber-300 leading-snug mt-1">No test reaches this function through its callers.</p>
              )}
              {impact.coverage != null && (
                <p className="text-xs text-slate-400 mt-1">The graph matched {Math.round(impact.coverage * 100)}% of calls to a single function.</p>
              )}
            </div>
            <div className="flex gap-2">
              <button
                type="button"
                onClick={() => openTrace(selectedId)}
                className="flex-1 px-3 py-2 rounded-lg text-sm font-medium bg-blue-600 hover:bg-blue-500 text-white transition-colors"
              >
                Trace what it calls
              </button>
              <CopyLink graphId={graphId} nodeId={selectedId} />
            </div>
          </div>

          <div role="tablist" aria-label="Function details" onKeyDown={onTabKey} className="flex border-b border-[#1a2540] shrink-0">
            {tabs.map((t) => (
              <button
                key={t.id}
                id={`insp-tab-${t.id}`}
                role="tab"
                aria-selected={tab === t.id}
                aria-controls="insp-panel"
                tabIndex={tab === t.id ? 0 : -1}
                onClick={() => setTab(t.id)}
                className={`flex-1 py-2.5 text-sm border-b-2 -mb-px transition-colors ${
                  tab === t.id ? "border-blue-500 text-white font-medium" : "border-transparent text-slate-400 hover:text-slate-200"
                }`}
              >
                {t.label}{t.n != null && <span className="ml-1 text-slate-400 tabular-nums">{fmt.format(t.n)}</span>}
              </button>
            ))}
          </div>

          <div id="insp-panel" role="tabpanel" aria-labelledby={`insp-tab-${tab}`} className="flex-1 min-h-0 overflow-y-auto scroll-contain">
            {tab === "source" && <Code source={source} hunks={reviewRow?.hunks} />}
            {tab === "callers" && (
              <>
                {callers.length === 0 && impact.possible_caller_count === 0 && <div className="px-4 py-6 text-sm text-slate-400">Nothing calls this function. It may be an entry point, or only called dynamically.</div>}
                <ul>{callers.map((n) => <FnRow key={n.id} node={n} onOpen={revealFunction} />)}</ul>
                {impact.caller_count > callers.length && <div className="px-4 py-2 text-xs text-slate-400">Showing {callers.length} of {fmt.format(impact.caller_count)} callers.</div>}
                {impact.possible_caller_count > 0 && (
                  <section>
                    <h3 className="px-4 pt-3 pb-1 text-sm font-semibold text-amber-300">Unconfirmed callers ({fmt.format(impact.possible_caller_count)})</h3>
                    <p className="px-4 pb-2 text-xs text-slate-400 leading-snug">These call something with this name, but the graph could not tell which function they meant.</p>
                    <ul>{impact.possible_callers.map((n) => <FnRow key={n.id} node={n} onOpen={revealFunction} note={n.candidates ? `${n.candidates} matches` : null} />)}</ul>
                  </section>
                )}
              </>
            )}
            {tab === "calls" && (
              callees.length === 0
                ? <div className="px-4 py-6 text-sm text-slate-400">This function calls nothing defined in the repository.</div>
                : <ul>{callees.map((n) => <FnRow key={n.id} node={n} onOpen={revealFunction} />)}</ul>
            )}
            {tab === "notes" && <Notes impact={impact} refs={refs} />}
          </div>
        </>
      )}
    </aside>
  );
}
