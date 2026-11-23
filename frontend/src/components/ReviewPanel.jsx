import React, { useEffect, useMemo, useRef, useState } from "react";
import { API, apiFetch, useFlowStore } from "../store.js";
import { CloseIcon, Code } from "./Inspector.jsx";
import QueuePanel, { reviewRecordKey, readRecord, writeRecord } from "./QueuePanel.jsx";

const fmt = new Intl.NumberFormat();
const plural = (n, one, many = `${one}s`) => `${fmt.format(n)} ${n === 1 ? one : many}`;
const cap = (s) => s.charAt(0).toUpperCase() + s.slice(1);

// Always a word next to the colour. "Low" is grey, not green: nothing here means "safe".
export const SEVERITY = {
  critical: { label: "Critical", color: "#f87171" },
  high: { label: "High", color: "#fb923c" },
  medium: { label: "Medium", color: "#fbbf24" },
  low: { label: "Low", color: "#94a3b8" },
};

const breaksOf = (c) => (c.findings || []).filter((f) => f.severity === "breaks");
// Must check: the change's top 3, high blast radius, or a static check proved something breaks
// (pr_impact.bucket; calibrated on the vLLM replay). AI notes never move a chunk.
const bucketOf = (c) => (c.top || c.band === "critical" || c.band === "high" || breaksOf(c).length ? "must" : c.band === "medium" ? "glance" : "skim");
const BUCKETS = [
  { id: "must", title: "Must check" },
  { id: "glance", title: "Worth a glance" },
  { id: "skim", title: "Skim" },
];
const ORDER = { must: 0, glance: 1, skim: 2 };
const noteKey = (c) => `${c.file}::${c.name}`;

function reachLine(c) {
  const imp = c.impact || {};
  const parts = [c.kind === "module"
    ? `imported by ${plural((c.importers?.length || 0) + (c.module_importers?.length || 0), "file")}`
    : c.upstream ? plural(c.upstream, "dependent") : "no dependents in the graph"];
  if (imp.endpoints?.count) parts.push(plural(imp.endpoints.count, "API endpoint"));
  if (imp.crosses) parts.push(plural(imp.crosses, "other service"));
  if (imp.tests?.count) parts.push(plural(imp.tests.count, "test"));
  return parts.join(" · ");
}

// Source of one function, fetched on demand (the store's nodeSource belongs to the inspector).
function useSource(graphId, id) {
  const [src, setSrc] = useState(null);
  useEffect(() => {
    setSrc(null);
    if (!graphId || !id) return undefined;
    let live = true;
    apiFetch(`${API}/node_source?graph_id=${graphId}&node_id=${encodeURIComponent(id)}`)
      .then((r) => (r.ok ? r.json() : { code: "" }))
      .catch(() => ({ code: "" }))
      .then((s) => { if (live) setSrc(s); });
    return () => { live = false; };
  }, [graphId, id]);
  return src;
}

// ── small parts ─────────────────────────────────────────────────────────────────────────────

const Svg = ({ children, className = "w-3.5 h-3.5" }) => (
  <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" className={className}>{children}</svg>
);
const BreakIcon = () => <Svg><circle cx="8" cy="8" r="6" /><path d="M4 12 12 4" /></Svg>;
const WarnIcon = () => <Svg><path d="M8 2.5 14 13H2z" /><path d="M8 7v2.5M8 11.2v.1" /></Svg>;
const FlagIcon = () => <Svg><path d="M3.5 14V2.5M3.5 3h8l-1.8 3 1.8 3h-8" /></Svg>;
const BackIcon = () => <Svg className="w-4 h-4"><path d="M10 3 5 8l5 5" /></Svg>;
const CheckIcon = () => <Svg className="w-3 h-3"><path d="m3.5 8.5 3 3 6-7" /></Svg>;

function Tick({ state }) {
  // done: checked at this content · stale: checked, then the code changed · flagged: needs changes
  const cls = { done: "border-slate-500 bg-slate-600 text-white", stale: "border-amber-400 text-amber-300", flagged: "border-rose-400 bg-rose-500/20 text-rose-300" }[state] || "border-slate-600";
  return (
    <span aria-hidden="true" className={`mt-0.5 shrink-0 w-4 h-4 rounded-full border flex items-center justify-center ${cls}`}>
      {state === "done" && <CheckIcon />}
      {state === "flagged" && <span className="w-1.5 h-1.5 rounded-full bg-rose-300" />}
    </span>
  );
}

function ScoreBadge({ score, band, size = "sm" }) {
  const s = SEVERITY[band] || SEVERITY.low;
  return (
    <span className="shrink-0 flex items-center gap-1.5 font-semibold" style={{ color: s.color }}>
      <span aria-hidden="true" className="w-2 h-2 rounded-full" style={{ background: s.color }} />
      {score != null && <span className={`tabular-nums ${size === "lg" ? "text-3xl leading-none" : "text-sm"}`}>{score}</span>}
      <span className="text-xs">{s.label}</span>
    </span>
  );
}

const rowBtn = "w-full text-left flex items-start gap-2.5 px-4 py-2.5 hover:bg-[#111a30] focus-visible:outline focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-blue-500";
const activeRow = "bg-[#13203b] shadow-[inset_3px_0_0_#3b82f6]";
const btn = "px-3 py-2 rounded-lg text-sm border border-[#1e2d4a] text-slate-200 hover:text-white hover:border-slate-500 disabled:opacity-40 transition-colors";
const primary = "px-3 py-2 rounded-lg text-sm font-medium bg-blue-600 hover:bg-blue-500 text-white disabled:opacity-40 transition-colors";

function ChunkItem({ c, active, state, onPick }) {
  const brk = breaksOf(c).length;
  const checks = (c.findings || []).length - brk;
  const top = [...c.factors].sort((a, b) => b.points - a.points).slice(0, 2).map((f) => f.name);
  return (
    <li>
      <button type="button" onClick={() => onPick(c.id)} aria-current={active ? "true" : undefined} className={`${rowBtn} ${active ? activeRow : ""}`}>
        <Tick state={state} />
        <span className="min-w-0 flex-1">
          <span className="flex items-baseline gap-2">
            <span translate="no" className={`min-w-0 flex-1 font-mono text-[13px] truncate ${state === "done" ? "text-slate-400 line-through" : "text-white"}`}>{c.name}</span>
            <ScoreBadge score={c.score} band={c.band} />
          </span>
          <span translate="no" className="block text-xs text-slate-400 truncate">{c.file}:{c.line}{c.kind === "module" ? " · module-level" : ""}</span>
          <span className="flex items-center gap-1.5 text-xs text-slate-300 min-w-0">
            {brk > 0 && <span className="shrink-0 flex items-center gap-1 text-rose-300"><BreakIcon />breaks {plural(brk, "caller", "callers")}</span>}
            {!brk && checks > 0 && <span className="shrink-0 flex items-center gap-1 text-amber-300"><WarnIcon />{plural(checks, "check")}</span>}
            <span className="truncate">{(brk || checks) ? "· " : ""}{top.join(" · ") || "no factors"}</span>
          </span>
          {state === "stale" && <span className="block text-xs text-amber-300">changed since you checked</span>}
          {c.confidence.level === "low" && <span className="block text-xs text-amber-300/90">low confidence</span>}
        </span>
        <span className="sr-only">{state === "done" ? "checked" : state === "stale" ? "changed since you checked" : state === "flagged" ? "needs changes" : ""}</span>
      </button>
    </li>
  );
}

function Section({ title, children, aside }) {
  return (
    <section className="px-4 py-3 border-b border-[#1a2540]">
      <div className="flex items-baseline gap-2">
        <h3 className="text-sm font-semibold text-white">{title}</h3>
        {aside && <span className="text-xs text-slate-400">{aside}</span>}
      </div>
      {children}
    </section>
  );
}

function Fold({ title, children }) {
  return (
    <details className="group border-b border-[#1a2540]">
      <summary className="px-4 py-3 text-sm font-semibold text-white cursor-pointer select-none list-none flex items-center gap-2 hover:bg-[#0e1526] focus-visible:outline focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-blue-500">
        <Svg className="w-3.5 h-3.5 text-slate-400 transition-transform group-open:rotate-90"><path d="m6 3 5 5-5 5" /></Svg>
        {title}
      </summary>
      <div className="px-4 pb-3">{children}</div>
    </details>
  );
}

function EvidenceLine({ e }) {
  return (
    <div className="mt-1 rounded-md bg-[#0d1424] border border-[#16213a] px-2.5 py-1.5 font-mono text-xs leading-relaxed min-w-0">
      <span translate="no" className="text-slate-400">{e.file}:{e.line}</span>
      <span translate="no" className="block text-slate-100 whitespace-pre-wrap break-words">{e.code}</span>
    </div>
  );
}

function Findings({ c }) {
  const f = c.findings || [];
  if (!f.length) return null;
  return (
    <Section title="Static checks" aside={`${plural(f.length, "finding")} · from the code, no AI`}>
      <ul className="mt-2 flex flex-col gap-3">
        {[...f].sort((a, b) => (a.severity === "breaks" ? -1 : 1) - (b.severity === "breaks" ? -1 : 1)).map((x, i) => (
          <li key={i}>
            <p className={`flex items-start gap-1.5 text-sm leading-snug ${x.severity === "breaks" ? "text-rose-200" : "text-amber-100"}`}>
              <span className={`mt-0.5 shrink-0 ${x.severity === "breaks" ? "text-rose-300" : "text-amber-300"}`}>{x.severity === "breaks" ? <BreakIcon /> : <WarnIcon />}</span>
              <span><span className="font-semibold">{x.severity === "breaks" ? "Breaks: " : "Check: "}</span><span translate="no">{x.message.replaceAll("`", "")}</span></span>
            </p>
            {x.evidence.slice(0, 2).map((e, j) => <EvidenceLine key={j} e={e} />)}
          </li>
        ))}
      </ul>
    </Section>
  );
}

function AiNotes({ c, author }) {
  const res = useFlowStore((s) => s.aiResults[c.id]);
  const loading = useFlowStore((s) => s.aiLoading);
  const available = useFlowStore((s) => s.review?.ai_available);
  const runAI = useFlowStore((s) => s.runAI);
  if (!available && !res) return null;
  const status = {
    refused: "Claude declined to review this change.",
    error: res?.error === "AuthenticationError" ? "Anthropic rejected the API key. Check ANTHROPIC_API_KEY." : "The request to Claude failed. Try again.",
    invalid: "Claude's answer could not be read. Try again.",
  };
  return (
    <Section title="Claude's notes" aside="experimental · each note quotes the code it is about">
      {!res && (
        <button type="button" onClick={() => runAI([c.id])} disabled={loading} className={`${btn} mt-2`}>
          {loading ? "Asking Claude…" : "Ask Claude about this change"}
        </button>
      )}
      {res && status[res.status] && <p className="mt-1 text-sm text-amber-300">{status[res.status]}</p>}
      {res?.status === "ok" && res.notes.length === 0 && (
        <p className="mt-1 text-sm text-slate-300">No notes. Claude found nothing it could point at in this change and its callers.</p>
      )}
      {res?.notes?.length > 0 && (
        <ul className="mt-2 flex flex-col gap-3">
          {res.notes.map((n, i) => (
            <li key={i}>
              <p className="text-sm text-slate-100 leading-snug"><span className="font-semibold">{n.kind === "likely_bug" ? "Likely bug: " : "Question: "}</span>{n.claim}</p>
              {n.evidence.slice(0, 2).map((e, j) => <EvidenceLine key={j} e={e} />)}
              {author && n.fix && <p className="mt-1 text-xs text-slate-300"><span className="text-slate-400">Possible fix:</span> {n.fix}</p>}
            </li>
          ))}
        </ul>
      )}
      {res?.dropped > 0 && <p className="mt-2 text-xs text-slate-400">{plural(res.dropped, "note")} dropped: the quoted code could not be found where Claude said.</p>}
    </Section>
  );
}

function Callers({ c, graphId }) {
  const list = c.not_updated || [];
  const [pick, setPick] = useState(0);
  useEffect(() => { setPick(0); }, [c.id]);
  const cur = list[pick];
  const src = useSource(graphId, cur?.id);
  if (!list.length) return null;
  const tests = list.filter((p) => p.is_test).length;
  return (
    <section className="border-b border-[#1a2540]">
      <div className="px-4 pt-3">
        <h3 className="text-sm font-semibold text-white">Callers not in this change <span className="font-normal text-slate-400">({list.length}{tests ? `, ${tests} test${tests > 1 ? "s" : ""}` : ""})</span></h3>
        <p className="text-xs text-slate-400">Each calls the changed code but was not edited with it. Does it still work?</p>
        <div className="mt-2 flex flex-wrap gap-1.5">
          {list.slice(0, 12).map((p, i) => (
            <button key={p.id} type="button" translate="no" onClick={() => setPick(i)} aria-pressed={i === pick}
              className={`font-mono text-xs px-2 py-1 rounded border ${i === pick ? "border-blue-500 text-white" : "border-[#1e2d4a] text-slate-300 hover:text-white"}`}>
              {p.name}{p.is_test ? " (test)" : ""}{!p.file_in_diff ? " ·" : ""}
            </button>
          ))}
          {list.length > 12 && <span className="text-xs text-slate-400 self-center">+{list.length - 12} more</span>}
        </div>
        {cur && (
          <p className="mt-2 text-xs text-slate-400">
            <span translate="no" className="font-mono">{cur.file}{cur.line ? `:${cur.line}` : ""}</span>
            {!cur.file_in_diff && " · file not touched by this change"} · linked because {cur.rule || "a direct call in the graph"}
            {cur.confidence != null && <> ({Math.round(cur.confidence * 100)}% sure)</>}
          </p>
        )}
      </div>
      {cur && <Code source={src} callLine={cur.line} context={4} />}
    </section>
  );
}

function Factors({ c }) {
  return (
    <>
      <ul className="flex flex-col gap-2">
        {c.factors.map((f) => (
          <li key={f.name}>
            <div className="flex items-center gap-2 text-xs">
              <span className="w-28 shrink-0 text-slate-300">{cap(f.name)}</span>
              <span aria-hidden="true" className="flex-1 h-1.5 rounded-full bg-[#16213a] overflow-hidden">
                <span className="block h-full rounded-full bg-blue-500/70" style={{ width: `${Math.min(100, Math.round((f.points / (f.max || f.points)) * 100))}%` }} />
              </span>
              <span className="w-16 shrink-0 text-right tabular-nums whitespace-nowrap text-slate-200">+{f.points}<span className="text-slate-400">/{f.max}</span></span>
            </div>
            <p className="pl-[7.5rem] pr-16 text-xs text-slate-400 leading-snug break-words">{f.why}</p>
          </li>
        ))}
      </ul>
      <p className="mt-3 text-xs text-slate-400 leading-snug">The score ranks blast radius: how far a mistake here can travel. It is not a bug probability.</p>
      {c.confidence.level !== "high" && (
        <p className="mt-1 text-xs text-amber-300 leading-snug">
          Confidence: {c.confidence.level}. {c.confidence.notes.join("; ")}{c.confidence.notes.length ? "." : ""} Incomplete evidence raises a floor, it never lowers the score.
        </p>
      )}
    </>
  );
}

function Reach({ c }) {
  const imp = c.impact;
  if (!imp) return null;
  const more = (g) => (g.count > g.examples.length ? ` +${fmt.format(g.count - g.examples.length)} more` : "");
  const line = (label, g, show = (x) => x) => g.count > 0 && (
    <p className="text-xs text-slate-400 leading-snug">
      <span className="text-slate-300">{label}</span> {fmt.format(g.count)}:{" "}
      <span translate="no" className="font-mono text-slate-300">{g.examples.map(show).join(", ")}</span>{more(g)}
    </p>
  );
  return (
    <>
      {imp.services.length === 0 ? (
        <p className="text-xs text-slate-400 leading-snug">Nothing in the graph depends on it. That is what the graph could link, not proof that nothing does.</p>
      ) : (
        <>
          <p className="text-xs text-slate-400">
            Lives in <span translate="no" className="font-mono text-slate-300">{imp.own_service}</span>
            {imp.crosses > 0 ? `; ${plural(imp.crosses, "other service")} depend on it.` : "; no other service depends on it."}
          </p>
          <ul className="mt-2 flex flex-col gap-1.5">
            {imp.services.slice(0, 6).map((s) => (
              <li key={s.name} className="text-xs min-w-0">
                <span className="flex items-baseline gap-2 min-w-0">
                  <span translate="no" className="font-mono text-[13px] text-white truncate">{s.name}</span>
                  {s.name === imp.own_service && <span className="shrink-0 text-[11px] text-slate-400 border border-[#1e2d4a] rounded px-1.5">this change</span>}
                  {s.owner && <span translate="no" className="shrink-0 text-slate-400">{s.owner}</span>}
                  <span className="ml-auto shrink-0 tabular-nums text-slate-200">{plural(s.dependents, c.kind === "module" ? "file" : "dependent")}</span>
                </span>
                <span translate="no" className="block font-mono text-slate-400 truncate">{s.examples.join(", ")}</span>
              </li>
            ))}
            {imp.services.length > 6 && <li className="text-xs text-slate-400">+{imp.services.length - 6} more services</li>}
          </ul>
        </>
      )}
      <div className="mt-2 flex flex-col gap-1">
        {line("API endpoints", imp.endpoints, (e) => e.name)}
        {line("Database access", imp.db)}
        {line("Events", imp.events)}
        {line("Tests", imp.tests)}
      </div>
    </>
  );
}

// Author mode: run the tests that reach a chunk, or write why it is fine without one.
function AuthorCheck({ c, note, draft, setDraft }) {
  const run = useFlowStore((s) => s.testRuns[c.id]);
  const runTests = useFlowStore((s) => s.runChunkTests);
  const ids = c.impact?.tests?.run || [];
  const outcomes = run?.outcomes ? Object.entries(run.outcomes) : [];
  const stale = note && note.hash !== c.hash;
  return (
    <Section title="Before you ask for review" aside={bucketOf(c) === "must" ? "required for must-check changes" : "optional"}>
      <div className="mt-2 flex flex-wrap items-center gap-2">
        <button type="button" onClick={() => runTests(c.id)} disabled={!ids.length || run?.running} className={btn}>
          {run?.running ? "Running tests…" : ids.length ? `Run the ${plural(ids.length, "test")} that reach${ids.length === 1 ? "es" : ""} this` : "No pytest test reaches this"}
        </button>
        {run?.seconds != null && <span className="text-xs text-slate-400">{run.seconds} s · at {String(run.head).slice(0, 7)}</span>}
      </div>
      {run?.error && <p className="mt-1 text-sm text-rose-300">{run.error}</p>}
      {outcomes.length > 0 && (
        <ul className="mt-2 flex flex-col gap-0.5 text-xs font-mono">
          {outcomes.map(([id, o]) => (
            <li key={id} className="flex gap-2 min-w-0">
              <span className={`shrink-0 w-14 ${o === "passed" ? "text-slate-300" : o === "failed" ? "text-rose-300" : "text-amber-300"}`}>{o}</span>
              <span translate="no" className="truncate text-slate-400">{id}</span>
            </li>
          ))}
        </ul>
      )}
      {run?.output && <details className="mt-1"><summary className="text-xs text-blue-300 cursor-pointer">pytest output</summary><pre className="mt-1 text-[11px] text-slate-400 whitespace-pre-wrap">{run.output}</pre></details>}
      <label htmlFor={`note-${c.id}`} className="mt-3 block text-xs font-semibold text-slate-400">Note for the reviewer</label>
      <textarea id={`note-${c.id}`} value={draft ?? note?.note ?? ""} onChange={(e) => setDraft(e.target.value)} rows={2}
        placeholder="Why this is safe without a test, or what you checked by hand"
        className="mt-1 w-full bg-[#0f1629] border border-[#1e2d4a] rounded-lg px-3 py-2 text-sm text-slate-100 placeholder:text-slate-500 focus-visible:outline-none focus-visible:border-blue-500 focus-visible:ring-2 focus-visible:ring-blue-500/40" />
      {stale && <p className="text-xs text-amber-300">Your saved note was written before the code last changed.</p>}
    </Section>
  );
}

// What the author recorded for this chunk, as the reviewer sees it. Self-reported, and marked stale.
function AuthorSays({ note, c }) {
  if (!note) return null;
  const stale = note.hash !== c.hash;
  const passed = note.tests.filter((t) => t.outcome === "passed").length;
  return (
    <Section title="The author says" aside={`self-reported${stale ? " · written before the code last changed" : ""}`}>
      {note.note && <p className={`mt-1 text-sm leading-snug ${stale ? "text-slate-400" : "text-slate-200"}`}>{note.note}</p>}
      {note.tests.length > 0 && (
        <p className="mt-1 text-xs text-slate-400">Ran {plural(note.tests.length, "reaching test")}: {passed} passed at {String(note.head).slice(0, 7)}.</p>
      )}
    </Section>
  );
}

function ChunkEvidence({ c, graphId, author, state, flag, onToggle, onFlag, onGraph, notes, draft, setDraft }) {
  const fnSrc = useSource(graphId, c.kind === "chunk" ? c.id : null);
  const source = c.kind === "module" ? c.excerpt : fnSrc;
  const [flagOpen, setFlagOpen] = useState(false);
  const [flagText, setFlagText] = useState("");
  const [copied, setCopied] = useState("");
  const flagRef = useRef(null);
  useEffect(() => { setFlagOpen(false); setFlagText(flag?.note || ""); }, [c.id]);  // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    const open = () => setFlagOpen(true);  // the input autofocuses as it mounts
    window.addEventListener("flowify:flag", open);
    return () => window.removeEventListener("flowify:flag", open);
  }, []);
  const [whole, setWhole] = useState(false);
  useEffect(() => { setWhole(false); }, [c.id]);
  const copy = async () => {
    const top = c.findings?.[0];
    const text = `\`${c.name}\` (${c.file}:${c.line}): risk ${c.score}/100, ${c.band}. ${reachLine(c)}.${top ? ` ${top.message}.` : ""}`;
    try { await navigator.clipboard.writeText(text); setCopied("Copied"); } catch { setCopied("Copy failed"); }
    setTimeout(() => setCopied(""), 2000);
  };
  const authorNote = notes?.chunks?.[noteKey(c)];

  return (
    <div className="flex-1 min-w-0 flex flex-col">
      <div className="px-4 py-3 border-b border-[#1a2540] shrink-0">
        <div className="flex items-start gap-4">
          <ScoreBadge score={c.score} band={c.band} size="lg" />
          <div className="min-w-0">
            <h2 translate="no" className="font-mono text-base text-white break-words">{c.name}</h2>
            <p translate="no" className="text-xs text-slate-400 font-mono break-all">{c.file}:{c.line}{c.kind === "module" ? " · module-level edit" : ""}</p>
            <p className="text-xs text-slate-300 mt-0.5">{reachLine(c)}</p>
          </div>
        </div>
        <div className="mt-3 flex flex-wrap gap-2">
          <button type="button" onClick={onToggle} aria-pressed={state === "done"} className={primary}>
            {state === "done" ? "Checked · undo" : state === "stale" ? "Check again" : "Mark checked"}
          </button>
          {!author && (
            <button type="button" onClick={() => setFlagOpen((v) => !v)} aria-expanded={flagOpen}
              className={`${btn} flex items-center gap-1.5 ${flag ? "border-rose-400/60 text-rose-200" : ""}`}>
              <FlagIcon />{flag ? "Needs changes" : "Flag: needs changes"}
            </button>
          )}
          <button type="button" onClick={copy} className={btn}><span aria-live="polite">{copied || "Copy summary"}</span></button>
          {onGraph && c.kind === "chunk" && <button type="button" onClick={() => onGraph(c.id)} className={btn}>Show in graph</button>}
        </div>
        {flagOpen && (
          <form className="mt-2 flex gap-2" onSubmit={(e) => { e.preventDefault(); onFlag(flagText.trim()); setFlagOpen(false); }}>
            <label htmlFor="flag-note" className="sr-only">What needs to change</label>
            <input id="flag-note" ref={flagRef} autoFocus value={flagText} onChange={(e) => setFlagText(e.target.value)} placeholder="What needs to change (optional)"
              onKeyDown={(e) => { if (e.key === "Escape") { e.stopPropagation(); setFlagOpen(false); } }}
              className="min-w-0 flex-1 bg-[#0f1629] border border-[#1e2d4a] rounded-lg px-3 py-2 text-sm text-slate-100 placeholder:text-slate-500 focus-visible:outline-none focus-visible:border-blue-500 focus-visible:ring-2 focus-visible:ring-blue-500/40" />
            <button type="submit" className={primary}>Save flag</button>
            {flag && <button type="button" onClick={() => { onFlag(null); setFlagOpen(false); }} className={btn}>Clear</button>}
          </form>
        )}
        {!flagOpen && flag?.note && <p className="mt-2 text-sm text-rose-200"><span className="text-rose-300">Your flag:</span> {flag.note}</p>}
      </div>
      <div className="flex-1 min-h-0 overflow-y-auto scroll-contain">
        {author ? <AuthorCheck c={c} note={authorNote} draft={draft} setDraft={setDraft} /> : <AuthorSays note={authorNote} c={c} />}
        <Findings c={c} />
        <AiNotes c={c} author={author} />
        <section className="border-b border-[#1a2540]">
          <header className="px-4 pt-3 flex items-baseline gap-2 min-w-0">
            <h3 className="text-sm font-semibold text-white shrink-0">What changed</h3>
            <span className="min-w-0 flex-1 text-xs text-slate-400">removed lines in red, added in amber</span>
            {c.kind === "chunk" && (
              <button type="button" onClick={() => setWhole((v) => !v)} aria-pressed={whole} className="shrink-0 text-xs text-blue-300 hover:text-blue-200 underline underline-offset-2">
                {whole ? "Show excerpt" : "Show whole function"}
              </button>
            )}
          </header>
          <Code source={source} hunks={c.hunks} removed={c.removed} context={whole ? undefined : 4} />
        </section>
        <Callers c={c} graphId={graphId} />
        <Fold title="What it can reach"><Reach c={c} /></Fold>
        <Fold title={`Why ${c.score}`}><Factors c={c} /></Fold>
      </div>
    </div>
  );
}

// ── finish ──────────────────────────────────────────────────────────────────────────────────

function summaryMarkdown(target, chunks, rec) {
  const flagged = chunks.filter((c) => rec.flags[c.id]);
  const done = chunks.filter((c) => rec.checked[c.id] === c.hash);
  const must = chunks.filter((c) => bucketOf(c) === "must");
  const open = must.filter((c) => rec.checked[c.id] !== c.hash && !rec.flags[c.id]);
  const lines = [`### Review: ${target.label} vs ${target.base}`, "",
    `Checked ${done.length} of ${plural(chunks.length, "change")}${flagged.length ? `; ${flagged.length} need${flagged.length === 1 ? "s" : ""} changes` : ""}.`];
  if (flagged.length) {
    lines.push("", "**Needs changes**");
    for (const c of flagged) {
      const brk = breaksOf(c)[0];
      lines.push(`- \`${c.name}\` (${c.file}:${c.line})${rec.flags[c.id].note ? `: ${rec.flags[c.id].note}` : ""}${brk ? ` (static check: ${brk.message.replaceAll("`", "")})` : ""}`);
    }
  }
  if (open.length) lines.push("", `**Not reviewed yet** (must check): ${open.map((c) => `\`${c.name}\``).join(", ")}`);
  lines.push("", "<sub>Ranked by Flowify: blast radius from the static call graph, static contract checks. A short list is a place to start, never proof of safety.</sub>");
  return lines.join("\n");
}

function Finish({ target, chunks, rec, onClose }) {
  const text = summaryMarkdown(target, chunks, rec);
  const [copied, setCopied] = useState("");
  const done = chunks.filter((c) => rec.checked[c.id] === c.hash).length;
  const flagged = chunks.filter((c) => rec.flags[c.id]).length;
  const copy = async () => {
    try { await navigator.clipboard.writeText(text); setCopied("Copied"); } catch { setCopied("Copy failed"); }
    setTimeout(() => setCopied(""), 2000);
  };
  return (
    <div className="flex-1 min-w-0 flex flex-col">
      <div className="px-4 py-3 border-b border-[#1a2540] flex items-center gap-3">
        <h2 className="text-base font-semibold text-white">Finish the review</h2>
        <button type="button" onClick={onClose} className="ml-auto text-sm text-blue-300 hover:text-blue-200 underline underline-offset-2">Back to the change</button>
      </div>
      <div className="flex-1 min-h-0 overflow-y-auto scroll-contain px-4 py-4">
        <dl className="flex flex-wrap gap-x-8 gap-y-2 text-sm">
          <div><dt className="text-slate-400 text-xs">Checked</dt><dd className="text-white tabular-nums text-xl">{done}<span className="text-slate-400 text-sm"> / {chunks.length}</span></dd></div>
          <div><dt className="text-slate-400 text-xs">Needs changes</dt><dd className="text-rose-200 tabular-nums text-xl">{flagged}</dd></div>
          <div><dt className="text-slate-400 text-xs">Not looked at</dt><dd className="text-slate-200 tabular-nums text-xl">{chunks.length - done - flagged}</dd></div>
        </dl>
        <p className="mt-4 text-sm text-slate-300">Paste this into the pull request:</p>
        <pre className="mt-2 p-3 rounded-lg bg-[#0d1424] border border-[#16213a] text-xs text-slate-200 whitespace-pre-wrap break-words max-w-[90ch]">{text}</pre>
        <button type="button" onClick={copy} className={`${primary} mt-3`}><span aria-live="polite">{copied || "Copy review summary"}</span></button>
      </div>
    </div>
  );
}

// ── the screen ──────────────────────────────────────────────────────────────────────────────

function BranchReview() {
  const review = useFlowStore((s) => s.review);
  const loading = useFlowStore((s) => s.reviewLoading);
  const error = useFlowStore((s) => s.reviewError);
  const target = useFlowStore((s) => s.reviewTarget);
  const mainGraph = useFlowStore((s) => s.graphId);
  const close = useFlowStore((s) => s.closeReview);
  const back = useFlowStore((s) => s.backToQueue);
  const run = useFlowStore((s) => s.runReview);
  const reveal = useFlowStore((s) => s.revealFunction);
  const runAI = useFlowStore((s) => s.runAI);
  const aiLoading = useFlowStore((s) => s.aiLoading);
  const aiError = useFlowStore((s) => s.aiError);
  const aiUsage = useFlowStore((s) => s.aiUsage);
  const aiResults = useFlowStore((s) => s.aiResults);
  const testRuns = useFlowStore((s) => s.testRuns);
  const saveNotes = useFlowStore((s) => s.saveNotes);

  const recKey = reviewRecordKey(mainGraph, target.ref);
  const [rec, setRec] = useState(() => readRecord(recKey));
  useEffect(() => { setRec(readRecord(recKey)); }, [recKey]);
  const update = (fn) => setRec((r) => { const next = fn(r); writeRecord(recKey, next); return next; });

  const [sel, setSel] = useState(null);
  const [skimOpen, setSkimOpen] = useState(false);
  const [finishing, setFinishing] = useState(false);
  const [mobileEvidence, setMobileEvidence] = useState(false);
  const [onlyChanged, setOnlyChanged] = useState(false);
  const [drafts, setDrafts] = useState({});
  const [saveMsg, setSaveMsg] = useState("");
  const author = !!target.author;

  const chunks = useMemo(() => {
    if (!review) return [];
    return [...(review.functions || []).map((f) => ({ ...f, kind: "chunk" })), ...(review.module_chunks || []).map((m) => ({ ...m, kind: "module" }))]
      .sort((a, b) => ORDER[bucketOf(a)] - ORDER[bucketOf(b)] || b.score - a.score || a.name.localeCompare(b.name));
  }, [review]);
  const priorChecks = chunks.some((c) => rec.checked[c.id]);
  // A re-review: the change moved on since this reviewer last looked. Show what is new by default.
  useEffect(() => {
    setSel(null); setFinishing(false); setDrafts({});
    setOnlyChanged(!!review && !author && chunks.some((c) => rec.checked[c.id] && rec.checked[c.id] !== c.hash));
    if (review && !author) update((r) => ({ ...r, head: review.head, total: chunks.length }));
  }, [review]);  // eslint-disable-line react-hooks/exhaustive-deps

  const stateOf = (c) => (rec.flags[c.id] ? "flagged" : rec.checked[c.id] === c.hash ? "done" : rec.checked[c.id] ? "stale" : "");
  const visible = onlyChanged ? chunks.filter((c) => rec.checked[c.id] !== c.hash) : chunks;
  const groups = BUCKETS.map((b) => ({ ...b, items: visible.filter((c) => bucketOf(c) === b.id) }));
  const nav = groups.flatMap((g) => (g.id === "skim" && !skimOpen ? [] : g.items));
  const current = nav.find((c) => c.id === sel) || (sel && chunks.find((c) => c.id === sel)) || nav[0] || null;
  const doneCount = chunks.filter((c) => stateOf(c) === "done" || stateOf(c) === "flagged").length;

  const toggle = (c) => update((r) => {
    const checked = { ...r.checked };
    if (checked[c.id] === c.hash) delete checked[c.id]; else checked[c.id] = c.hash;
    return { ...r, checked };
  });
  const setFlag = (c, note) => update((r) => {
    const flags = { ...r.flags };
    if (note === null) delete flags[c.id]; else flags[c.id] = { hash: c.hash, note };
    return { ...r, flags };
  });
  const skimAll = (items) => update((r) => ({ ...r, checked: { ...r.checked, ...Object.fromEntries(items.map((c) => [c.id, c.hash])) } }));

  useEffect(() => {
    const onKey = (e) => {
      if (e.metaKey || e.ctrlKey || e.altKey || /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName)) return;
      const i = current ? nav.findIndex((c) => c.id === current.id) : -1;
      if (e.key === "j" && nav[i + 1]) setSel(nav[i + 1].id);
      else if (e.key === "k" && i > 0) setSel(nav[i - 1].id);
      else if (e.key === "x" && current) toggle(current);
      else if (e.key === "f" && current && !author) window.dispatchEvent(new Event("flowify:flag"));
      else if (e.key === "Escape" && !document.querySelector('[role="dialog"]')) { if (finishing) setFinishing(false); else close(); }
      else return;
      e.preventDefault();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  const must = chunks.filter((c) => bucketOf(c) === "must");
  const brk = chunks.reduce((n, c) => n + breaksOf(c).length, 0);
  const untested = review?.has_tests ? chunks.filter((c) => !c.tested).length : 0;
  const aiNotes = Object.values(aiResults).reduce((n, r) => n + (r.notes?.length || 0), 0);
  const aiAsked = must.filter((c) => aiResults[c.id]).length;
  const parts = (review?.parts || []).filter((p) => p.lines >= 30);
  const notes = review?.notes;

  // author readiness: every must-check change has passing reaching tests or a current note
  const ready = (c) => {
    const runR = testRuns[c.id];
    if (runR?.outcomes && Object.keys(runR.outcomes).length && Object.values(runR.outcomes).every((o) => o === "passed") && runR.hash === c.hash) return true;
    const n = notes?.chunks?.[noteKey(c)];
    return !!(n && n.hash === c.hash && (n.note || (n.tests.length && n.tests.every((t) => t.outcome === "passed"))));
  };
  const notReady = author ? must.filter((c) => !ready(c)) : [];
  const save = async () => {
    const chunksOut = { ...(notes?.chunks || {}) };
    for (const c of chunks) {
      const runR = testRuns[c.id];
      const draft = drafts[c.id];
      if (draft == null && !runR?.outcomes) continue;
      const prev = chunksOut[noteKey(c)];
      chunksOut[noteKey(c)] = {
        hash: c.hash, note: draft ?? prev?.note ?? "",
        tests: runR?.outcomes ? Object.entries(runR.outcomes).map(([id, outcome]) => ({ id, outcome })) : prev?.hash === c.hash ? prev.tests : [],
      };
    }
    try { await saveNotes(chunksOut); setDrafts({}); setSaveMsg("Saved to .flowify/review.json; commit it with your change."); }
    catch (e) { setSaveMsg(e.message); }
  };

  const pick = (id) => { setSel(id); setFinishing(false); setMobileEvidence(true); };
  const onGraph = target.graphId === mainGraph ? (id) => { close(); reveal(id); } : null;

  return (
    <section aria-label={`Review ${target.label}`} className="absolute inset-0 z-30 flex flex-col bg-[#0a0f1a]">
      {/* header */}
      <div className="flex items-center gap-2 px-3 md:px-4 py-2.5 border-b border-[#1a2540] shrink-0 min-w-0">
        <button type="button" onClick={back} aria-label="Back to the review queue" className="w-8 h-8 shrink-0 rounded-lg flex items-center justify-center text-slate-400 hover:text-white hover:bg-slate-800"><BackIcon /></button>
        <h2 className="min-w-0 text-base font-semibold text-white truncate">
          <span translate="no" className="font-mono">{target.label}</span>
          <span className="text-slate-400 font-normal"> vs <span translate="no" className="font-mono">{target.base}</span></span>
        </h2>
        {author && <span className="shrink-0 text-[11px] text-slate-300 border border-[#1e2d4a] rounded px-1.5 py-0.5">author</span>}
        <span className="ml-auto flex items-center gap-2 shrink-0">
          {chunks.length > 0 && !author && <span className="hidden sm:inline text-xs text-slate-400 tabular-nums">{doneCount} / {chunks.length} done</span>}
          <button type="button" onClick={() => run()} disabled={loading} className="text-xs text-blue-300 hover:text-blue-200 underline underline-offset-2 disabled:opacity-40">{loading ? "Checking…" : "Re-run"}</button>
          {!author && chunks.length > 0 && <button type="button" onClick={() => { setFinishing(true); setMobileEvidence(true); }} className={primary}>Finish</button>}
          {author && chunks.length > 0 && <button type="button" onClick={save} className={primary}>Save notes</button>}
          <button type="button" onClick={close} aria-label="Close review, back to the graph"
            className="w-8 h-8 rounded-lg flex items-center justify-center text-slate-400 hover:text-white hover:bg-slate-800 transition-colors"><CloseIcon /></button>
        </span>
      </div>

      {/* verdict strip: the 10-second triage */}
      {review && !loading && chunks.length > 0 && (
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1.5 px-4 py-2 border-b border-[#1a2540] text-sm shrink-0">
          <span className="text-white"><span className="font-semibold tabular-nums">{must.length}</span> must check</span>
          {brk > 0 && <span className="flex items-center gap-1 text-rose-300"><BreakIcon /><span className="tabular-nums">{brk}</span> break{brk === 1 ? "s" : ""} a caller</span>}
          {untested > 0 && <span className="text-slate-300"><span className="tabular-nums">{untested}</span> untested</span>}
          {parts.length >= 2 && <span className="text-slate-300" title={parts.map((p) => p.files.join(", ")).join("\n")}>{parts.length} independent parts: could be split</span>}
          {aiNotes > 0 && <span className="text-slate-300">Claude: {plural(aiNotes, "note")}</span>}
          <span className="ml-auto flex items-center gap-2">
            {review.ai_available && must.length > 0 && aiAsked < must.length && (
              <button type="button" onClick={() => runAI(must.slice(0, 8).map((c) => c.id))} disabled={aiLoading} className={btn}>
                {aiLoading ? "Asking Claude…" : `Ask Claude about the ${plural(Math.min(8, must.length), "must-check change")}`}
              </button>
            )}
            {!review.ai_available && <span className="text-xs text-slate-400">Set <span translate="no" className="font-mono">ANTHROPIC_API_KEY</span> to ask Claude</span>}
            {aiUsage && <span className="text-xs text-slate-400 tabular-nums">{fmt.format((aiUsage.input_tokens || 0) + (aiUsage.cache_read_input_tokens || 0))} in · {fmt.format(aiUsage.output_tokens || 0)} out tokens</span>}
          </span>
          {aiError && <p role="alert" className="basis-full text-xs text-rose-300">{aiError}</p>}
          {author && (
            <p className="basis-full text-xs">
              {notReady.length
                ? <span className="text-amber-300">Not ready: {plural(notReady.length, "must-check change")} still need passing tests or a note.</span>
                : <span className="text-slate-300">Ready for review: every must-check change has passing tests or a note.</span>}
              {saveMsg && <span className="text-slate-400"> {saveMsg}</span>}
            </p>
          )}
        </div>
      )}

      <div className="flex-1 min-h-0 flex">
        {/* worklist */}
        <div className={`${mobileEvidence ? "hidden md:flex" : "flex"} w-full md:w-[340px] shrink-0 flex-col border-r border-[#1a2540]`}>
          <div className="flex-1 min-h-0 overflow-y-auto scroll-contain">
            {error && <div role="alert" className="m-4 text-sm text-rose-300 bg-rose-950/40 border border-rose-900/60 rounded-lg p-2.5 break-words">{error}</div>}
            {loading && <div role="status" className="px-4 py-6 text-sm text-slate-400">Reading the diff and checking what it reaches…</div>}
            {review && !loading && (
              <>
                {priorChecks && !author && (
                  <label className="flex items-center gap-2 px-4 pt-3 text-xs text-slate-300 cursor-pointer">
                    <input type="checkbox" checked={onlyChanged} onChange={(e) => setOnlyChanged(e.target.checked)} className="accent-blue-500" />
                    Only what changed since I checked
                  </label>
                )}
                {groups.map((g) => g.items.length > 0 && (
                  <div key={g.id}>
                    <div className="flex items-baseline gap-2 px-4 pt-4 pb-1">
                      <h3 className="text-xs font-semibold text-slate-400">{g.title} ({g.items.length})</h3>
                      {g.id === "skim" && (
                        <span className="ml-auto flex gap-3">
                          <button type="button" onClick={() => setSkimOpen((v) => !v)} aria-expanded={skimOpen} className="text-xs text-blue-300 hover:text-blue-200 underline underline-offset-2">{skimOpen ? "Hide" : "Show"}</button>
                          {!author && <button type="button" onClick={() => skimAll(g.items)} className="text-xs text-blue-300 hover:text-blue-200 underline underline-offset-2">Mark all skimmed</button>}
                        </span>
                      )}
                    </div>
                    {(g.id !== "skim" || skimOpen) && (
                      <ul>{g.items.map((c) => <ChunkItem key={c.id} c={c} active={current?.id === c.id} state={stateOf(c)} onPick={pick} />)}</ul>
                    )}
                    {g.id === "skim" && !skimOpen && <p className="px-4 text-xs text-slate-400">Low blast radius and no static check found anything.</p>}
                  </div>
                ))}
                {onlyChanged && visible.length === 0 && <p className="px-4 py-5 text-sm text-slate-300">Nothing changed since you last checked.</p>}
                {chunks.length === 0 && (
                  <p className="px-4 py-5 text-sm text-slate-300 leading-relaxed">
                    No function bodies changed compared with <span translate="no" className="font-mono text-white">{review.base}</span>.
                    {review.files_changed > 0 && <> {plural(review.files_changed, "file")} changed, but not inside a function the graph knows.</>}
                    <span className="block mt-1 text-slate-400">That is what the graph could link, not proof that nothing changed that matters. Untracked new files are not included.</span>
                  </p>
                )}
                {((review.could_not_check || []).length > 0 || review.deleted.length > 0 || review.unmapped.length > 0 || review.services_config?.error) && (
                  <div className="mt-3 px-4 py-3 border-t border-[#1a2540] text-xs text-slate-400 leading-relaxed">
                    <h3 className="font-semibold text-amber-300 mb-1">Could not check</h3>
                    {(review.could_not_check || []).map((l) => (
                      <p key={l.id}><span translate="no" className="font-mono text-slate-300">{l.name}</span>: {fmt.format(l.shared_by)} functions share its name, so calls made by name alone are not attributed.</p>
                    ))}
                    {review.deleted.length > 0 && <p>Deleted: <span translate="no" className="font-mono text-slate-300">{review.deleted.slice(0, 5).join(", ")}</span>. Anything still calling into {review.deleted.length === 1 ? "it" : "them"} will break.</p>}
                    {review.unmapped.length > 0 && <p>Changed, but not code the graph tracks: <span translate="no">{review.unmapped.slice(0, 6).join(", ")}</span>{review.unmapped.length > 6 ? ` and ${review.unmapped.length - 6} more` : ""}.</p>}
                    {review.services_config?.error && <p><span translate="no" className="font-mono text-slate-300">{review.services_config.error}</span></p>}
                  </div>
                )}
              </>
            )}
          </div>
          <p className="hidden md:block px-4 py-2 border-t border-[#1a2540] text-xs text-slate-400 shrink-0">
            <kbd className="font-mono">j</kbd>/<kbd className="font-mono">k</kbd> move · <kbd className="font-mono">x</kbd> checked{!author && <> · <kbd className="font-mono">f</kbd> flag</>}
          </p>
        </div>

        {/* evidence */}
        <div className={`${mobileEvidence ? "flex" : "hidden md:flex"} flex-1 min-w-0 flex-col`}>
          <button type="button" onClick={() => setMobileEvidence(false)} className="md:hidden flex items-center gap-1 px-3 py-2 text-sm text-blue-300 border-b border-[#1a2540]"><BackIcon />All changes</button>
          {finishing && review
            ? <Finish target={target} chunks={chunks} rec={rec} onClose={() => setFinishing(false)} />
            : current && review && !loading
              ? <ChunkEvidence key={current.id} c={current} graphId={target.graphId} author={author} state={stateOf(current)} flag={rec.flags[current.id]}
                  onToggle={() => toggle(current)} onFlag={(n) => setFlag(current, n)} onGraph={onGraph} notes={notes}
                  draft={drafts[current.id]} setDraft={(v) => setDrafts((d) => ({ ...d, [current.id]: v }))} />
              : <div className="flex-1 flex items-center justify-center px-6 text-sm text-slate-400">{loading ? "" : "Pick a change on the left to see the code behind it."}</div>}
        </div>
      </div>
    </section>
  );
}

export default function ReviewPanel() {
  const open = useFlowStore((s) => s.reviewOpen);
  const target = useFlowStore((s) => s.reviewTarget);
  if (!open) return null;
  return target ? <BranchReview /> : <QueuePanel />;
}
