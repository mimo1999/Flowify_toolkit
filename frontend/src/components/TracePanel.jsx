import React, { useEffect, useRef, useState } from "react";
import { useFlowStore } from "../store.js";
import { CloseIcon } from "./Inspector.jsx";
import { ctxLabel } from "./traceLayout.js";

const fmt = new Intl.NumberFormat();

const short = (p) => (p || "").split("/").slice(-2).join("/");

function asText(trace) {
  const lines = [`Dry run from ${trace.steps[0].name} (${trace.stats.steps} steps)`, ""];
  trace.steps.forEach((s) => {
    const ctx = s.via?.ctx ? `[${ctxLabel[s.via.ctx] || s.via.ctx}] ` : "";
    const note = s.cycle ? " (cycle)" : s.repeat_of !== undefined ? ` (see step ${s.repeat_of})` : "";
    const amb = s.unresolved_count ? ` — ${s.unresolved_count} ambiguous call(s): ${s.unresolved.map((u) => u.callee).join(", ")}` : "";
    lines.push(`${"  ".repeat(s.depth)}${s.index}. ${ctx}${s.name} (${s.file_path}${s.via?.line ? `:${s.via.line}` : ""})${note}${amb}`);
  });
  return lines.join("\n");
}

function Picker() {
  const starts = useFlowStore((s) => s.traceStarts);
  const loading = useFlowStore((s) => s.traceLoading);
  const search = useFlowStore((s) => s.searchTraceStarts);
  const run = useFlowStore((s) => s.runTrace);
  const runPath = useFlowStore((s) => s.runPath);
  const [q, setQ] = useState("");
  const [between, setBetween] = useState(false);
  const [from, setFrom] = useState(null);
  const [to, setTo] = useState(null);
  const pick = (s) => {
    if (!between) run(s.id);
    else if (!from) setFrom(s);
    else setTo(s);
  };

  return (
    <div className="flex-1 min-h-0 flex flex-col px-4 py-3 gap-3">
      <div role="group" aria-label="Trace type" className="flex gap-1.5">
        {[[false, "From a Start"], [true, "Between Two"]].map(([v, label]) => (
          <button key={label} type="button" aria-pressed={between === v} onClick={() => setBetween(v)}
            className={`flex-1 px-2 py-1 rounded-lg text-xs border ${between === v ? "border-blue-500/80 bg-blue-900/30 text-blue-300" : "border-[#1e2d4a] text-slate-400 hover:border-slate-500"}`}>
            {label}
          </button>
        ))}
      </div>
      <p className="text-xs text-slate-400 leading-relaxed">
        {between
          ? "Pick two functions and Flowify finds the shortest chain of calls from the first to the second."
          : "Pick where a request or command begins. Flowify walks everything it calls, in the order the code calls it, across files and modules."}
      </p>
      {between && (
        <div className="space-y-1.5">
          {[["From", from, setFrom], ["To", to, setTo]].map(([label, val, set]) => (
            <div key={label} className="flex items-center gap-2 text-xs">
              <span className="w-8 text-slate-500">{label}</span>
              {val ? (
                <button type="button" onClick={() => set(null)} aria-label={`Clear ${label}: ${val.name}`}
                  className="flex-1 min-w-0 text-left px-2 py-1 rounded-lg border border-blue-700/50 bg-blue-950/30 text-blue-200">
                  <span translate="no" className="font-mono truncate block">{val.name} ✕</span>
                </button>
              ) : <span className="flex-1 px-2 py-1 rounded-lg border border-dashed border-[#1e2d4a] text-slate-400">choose below</span>}
            </div>
          ))}
          <button type="button" disabled={!from || !to} onClick={() => runPath(from.id, to.id)}
            className="w-full px-3 py-1.5 rounded-lg text-xs font-medium bg-blue-600 text-white hover:bg-blue-500 disabled:opacity-40">
            Find Path
          </button>
        </div>
      )}
      <form onSubmit={(e) => { e.preventDefault(); search(q.trim()); }} className="flex gap-2">
        <input
          name="trace-search"
          aria-label="Search for a function to start from"
          autoComplete="off"
          spellCheck={false}
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="e.g. handle_request…"
          className="flex-1 min-w-0 bg-[#0f1629] border border-[#1e2d4a] rounded-lg px-3 py-1.5 text-sm text-slate-200 placeholder:text-slate-400 focus-visible:outline-none focus-visible:border-blue-500"
        />
        <button type="submit" className="px-3 rounded-lg text-xs font-medium border border-[#1e2d4a] text-slate-300 hover:border-slate-500">
          Search
        </button>
      </form>
      <h3 className="text-[11px] uppercase tracking-widest text-slate-500 font-semibold">
        {q.trim() && starts.length ? "Matches" : "Suggested Starts"}
      </h3>
      <div className="flex-1 min-h-0 overflow-y-auto scroll-contain" role="status" aria-live="polite">
        {loading && <div className="text-xs text-slate-500 py-6 text-center">Loading…</div>}
        {!loading && starts.length === 0 && (
          <div className="text-xs text-slate-500 py-6 text-center">No functions found. Try part of a function name.</div>
        )}
        <ul className="space-y-1.5">
          {starts.map((s) => (
            <li key={s.id}>
              <button
                type="button"
                onClick={() => pick(s)}
                className="w-full text-left rounded-lg border border-[#1a2540] bg-[#0f1629] hover:border-blue-600/60 px-3 py-2 transition-colors"
              >
                <div className="flex items-center gap-2">
                  <span translate="no" className="font-mono text-xs text-white truncate flex-1">{s.name}</span>
                  {s.reason && <span className="shrink-0 text-[11px] text-blue-300/80">{s.reason}</span>}
                </div>
                <div translate="no" className="text-[11px] text-slate-500 truncate">{short(s.file_path)}</div>
                <div className="text-[11px] text-slate-400 tabular-nums">
                  {s.reach !== undefined
                    ? `reaches ${fmt.format(s.reach)} functions`
                    : `${fmt.format(s.callers ?? 0)} callers · ${fmt.format(s.callees ?? 0)} callees`}
                  {s.module ? ` · ${s.module}` : ""}
                </div>
              </button>
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}

function Steps() {
  const trace = useFlowStore((s) => s.trace);
  const step = useFlowStore((s) => s.traceStep);
  const setStep = useFlowStore((s) => s.setTraceStep);
  const clear = useFlowStore((s) => s.clearTrace);
  const [copied, setCopied] = useState(false);
  const rowRefs = useRef([]);
  const n = trace.steps.length;

  useEffect(() => {
    if (step >= 0) rowRefs.current[step]?.scrollIntoView({ block: "nearest" });
  }, [step]);

  const go = (i) => setStep(Math.max(0, Math.min(n - 1, i)));
  const onKey = (e) => {
    if (e.key === "ArrowDown") { e.preventDefault(); go(step + 1); }
    else if (e.key === "ArrowUp") { e.preventDefault(); go(step - 1); }
  };
  const copy = () => navigator.clipboard.writeText(asText(trace)).then(() => {
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  });
  const { stats } = trace;
  const cur = step >= 0 ? trace.steps[step] : null;

  return (
    <div className="flex-1 min-h-0 flex flex-col">
      <div className="px-4 py-3 border-b border-[#1a2540] space-y-2">
        <div className="text-xs text-slate-300">
          From <span translate="no" className="font-mono text-white">{trace.steps[0].name}</span>
          {trace.target && <> to <span translate="no" className="font-mono text-white">{trace.steps[trace.steps.length - 1].id === trace.target ? trace.steps[trace.steps.length - 1].name : trace.target.split("::").pop()}</span></>}
        </div>
        {trace.stats.found === false && (
          <div role="status" className="text-[11px] text-amber-300/90">
            No call chain found in the resolved graph. A link may still exist through a call the graph could not resolve.
          </div>
        )}
        {trace.stats.alternatives > 1 && (
          <div className="text-[11px] text-slate-400">{trace.stats.alternatives} routes of this length exist; this is one of them.</div>
        )}
        <div className="text-[11px] text-slate-400 tabular-nums">
          {fmt.format(stats.steps)} steps · {fmt.format(stats.unique_nodes)} functions · {fmt.format(stats.modules.length)} modules crossed
          {stats.unresolved_calls > 0 && ` · ${fmt.format(stats.unresolved_calls)} ambiguous calls`}
        </div>
        {stats.truncated && (
          <div className="text-[11px] text-amber-300/90">Trace cut at the step limit — start from a deeper function to see more.</div>
        )}
        <div className="flex gap-1.5">
          <button type="button" onClick={() => go(step < 0 ? 0 : step - 1)} disabled={step === 0}
            className="flex-1 px-2 py-1.5 rounded-lg text-xs border border-[#1e2d4a] text-slate-300 hover:border-slate-500 disabled:opacity-30">
            Previous
          </button>
          <button type="button" onClick={() => go(step + 1)} disabled={step === n - 1}
            className="flex-1 px-2 py-1.5 rounded-lg text-xs font-medium bg-blue-600 text-white hover:bg-blue-500 disabled:opacity-30">
            {step < 0 ? "Start Stepping" : "Next Step"}
          </button>
          <button type="button" onClick={() => setStep(-1)} aria-pressed={step === -1}
            className={`px-2.5 py-1.5 rounded-lg text-xs border ${step === -1 ? "border-blue-500/80 bg-blue-900/30 text-blue-300" : "border-[#1e2d4a] text-slate-400 hover:border-slate-500"}`}>
            Show All
          </button>
        </div>
        <div className="flex gap-1.5">
          <button type="button" onClick={copy} className="flex-1 px-2 py-1 rounded-lg text-[11px] border border-[#1e2d4a] text-slate-400 hover:text-slate-200 hover:border-slate-500">
            <span aria-live="polite">{copied ? "Copied" : "Copy as Text"}</span>
          </button>
          <button type="button" onClick={clear} className="flex-1 px-2 py-1 rounded-lg text-[11px] border border-[#1e2d4a] text-slate-400 hover:text-slate-200 hover:border-slate-500">
            New Trace
          </button>
        </div>
        <div role="status" className="sr-only">
          {cur ? `Step ${cur.index} of ${n - 1}: ${cur.name}` : "Showing the whole trace"}
        </div>
      </div>

      <ol className="flex-1 overflow-y-auto scroll-contain py-1" onKeyDown={onKey}>
        {trace.steps.map((s) => {
          const ctx = s.via?.ctx;
          const active = s.index === step;
          return (
            <li key={s.index}>
              <button
                type="button"
                ref={(el) => { rowRefs.current[s.index] = el; }}
                onClick={() => setStep(s.index)}
                aria-current={active ? "step" : undefined}
                style={{ paddingLeft: 12 + s.depth * 14 }}
                className={`w-full text-left pr-3 py-1.5 border-l-2 transition-colors ${
                  active ? "bg-blue-900/30 border-blue-400" : step >= 0 && s.index > step ? "border-transparent opacity-45 hover:opacity-80" : "border-transparent hover:bg-[#111a30]"
                }`}
              >
                <div className="flex items-center gap-1.5 min-w-0">
                  <span className="shrink-0 text-[11px] text-slate-500 tabular-nums w-6 text-right">{s.index}</span>
                  {ctx && <span className="shrink-0 text-[11px] px-1.5 rounded border border-amber-800/50 text-amber-300 bg-amber-950/40">{ctxLabel[ctx] || ctx}</span>}
                  <span translate="no" className="font-mono text-xs text-white truncate">{s.name}</span>
                  {s.cycle && <span className="shrink-0 text-[11px] text-rose-300">cycle</span>}
                  {s.repeat_of !== undefined && <span className="shrink-0 text-[11px] text-slate-500">= step {s.repeat_of}</span>}
                  {s.via?.confidence != null && s.via.confidence < 0.9 && (
                    <span className="shrink-0 text-[11px] text-slate-400 tabular-nums" title={s.via.reason || "resolution confidence"}>
                      {Math.round(s.via.confidence * 100)}%
                    </span>
                  )}
                </div>
                <div className="pl-8 text-[11px] text-slate-500 truncate" translate="no">
                  {short(s.file_path)}{s.via?.line ? `:${s.via.line}` : ""}
                </div>
                {(s.enters_module || s.unresolved_count > 0 || s.hidden_calls > 0 || s.cut > 0) && (
                  <div className="pl-8 flex flex-wrap gap-x-2 text-[11px]">
                    {s.enters_module && <span className="text-sky-300" translate="no">↪ enters {s.module}</span>}
                    {s.unresolved_count > 0 && (
                      <span className="text-amber-300" title={s.unresolved.map((u) => `${u.callee} (${u.candidates} candidates)`).join(", ")}>
                        {s.unresolved_count} ambiguous
                      </span>
                    )}
                    {s.hidden_calls > 0 && <span className="text-slate-500">+{s.hidden_calls} more calls not shown</span>}
                    {s.cut > 0 && <span className="text-slate-500">{s.cut} calls beyond depth limit</span>}
                  </div>
                )}
              </button>
            </li>
          );
        })}
      </ol>
    </div>
  );
}

export default function TracePanel() {
  const open = useFlowStore((s) => s.traceOpen);
  const trace = useFlowStore((s) => s.trace);
  const close = useFlowStore((s) => s.closeTrace);
  const error = useFlowStore((s) => s.traceError);
  if (!open) return null;
  return (
    <aside aria-label="Trace a Path" className="absolute top-0 right-0 h-full w-[360px] max-w-full z-10 flex flex-col bg-[#0a0f1a]/96 backdrop-blur-md border-l border-[#1a2540] shadow-[-4px_0_32px_rgba(0,0,0,0.5)] slide-in-right">
      <div className="flex items-center justify-between px-4 py-3 border-b border-[#1a2540] shrink-0">
        <h2 className="text-xs font-semibold text-white">Trace a Path</h2>
        <button type="button" onClick={close} aria-label="Close Trace"
          className="w-7 h-7 rounded-lg flex items-center justify-center text-slate-500 hover:text-slate-200 hover:bg-slate-800 transition-colors">
          <CloseIcon />
        </button>
      </div>
      {error && <div role="alert" className="mx-4 mt-3 text-xs text-rose-400 bg-rose-950/40 border border-rose-900/60 rounded-lg p-2.5 break-words">{error}</div>}
      {trace ? <Steps /> : <Picker />}
    </aside>
  );
}
