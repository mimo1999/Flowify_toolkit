import React from "react";
import { useFlowStore } from "../store.js";

const RISK_CONFIG = {
  critical: { color: "#ef4444", bg: "bg-red-950/40",    border: "border-red-800/50",    label: "CRITICAL" },
  high:     { color: "#f97316", bg: "bg-orange-950/40", border: "border-orange-800/50", label: "HIGH" },
  medium:   { color: "#f59e0b", bg: "bg-amber-950/40",  border: "border-amber-800/50",  label: "MEDIUM" },
  low:      { color: "#10b981", bg: "bg-emerald-950/40",border: "border-emerald-800/50",label: "LOW" },
};

const SEMANTIC_ICONS = {
  EXPOSES_API: "🔌", USES_DB: "🗄️", EMITS_EVENT: "📤",
  CONSUMES_EVENT: "📥", CALLS: "⚙️",
};

export const CloseIcon = ({ size = 13 }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
    <line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>
  </svg>
);

function CallerRow({ node }) {
  const icon = SEMANTIC_ICONS[node.semantic_kind] || "⚙️";
  return (
    <div className="flex items-center gap-1.5 py-1 border-b border-[#1a2540] last:border-0">
      <span className="text-[11px]">{icon}</span>
      <div className="min-w-0">
        <div className="font-mono text-[11px] text-white/80 truncate">{node.name}</div>
        <div className="text-[9px] text-slate-600 truncate">{node.file_path?.split("/").slice(-2).join("/")}</div>
      </div>
    </div>
  );
}

const MARKER_COLORS = {
  HACK: "text-orange-300", FIXME: "text-rose-300", BUG: "text-rose-300",
  WHY: "text-sky-300", TODO: "text-amber-300", NOTE: "text-slate-300",
  WARNING: "text-orange-300", XXX: "text-rose-300", OPTIMIZE: "text-emerald-300",
};

const SOURCE_LABELS = {
  ast: "AST", static_analysis: "Static", regex: "Regex",
  llm: "LLM", heuristic: "Heuristic", user: "User",
};

// Compact "97% · AST" trust badge derived from edge/document provenance
function ProvenanceBadge({ provenance }) {
  if (!provenance) return null;
  const conf = Math.round((provenance.confidence ?? 1) * 100);
  const solid = conf >= 85;
  return (
    <span
      title={provenance.reasoning || provenance.evidence || ""}
      className={`shrink-0 text-[9px] font-mono px-1.5 py-0.5 rounded border ${
        solid
          ? "text-emerald-400 border-emerald-800/50 bg-emerald-950/40"
          : "text-amber-400 border-amber-800/50 bg-amber-950/40"
      }`}
    >
      {conf}% · {SOURCE_LABELS[provenance.source] || provenance.source}
    </span>
  );
}

export default function ImpactPanel() {
  const impact        = useFlowStore((s) => s.impactData);
  const impactLoading = useFlowStore((s) => s.impactLoading);
  const nodeRefs      = useFlowStore((s) => s.nodeRefs);
  const selectedId    = useFlowStore((s) => s.selectedId);
  const clearSelection = useFlowStore((s) => s.clearSelection);

  // Only show for function nodes
  if (!selectedId || selectedId.startsWith("file::") || selectedId.startsWith("mod_")) return null;
  if (!impact && !impactLoading) return null;

  const risk = impact ? (RISK_CONFIG[impact.risk_level] || RISK_CONFIG.low) : null;

  return (
    <div className="absolute bottom-0 left-0 w-[300px] z-10 flex flex-col bg-[#0a0f1a]/96 backdrop-blur-md border-t border-r border-[#1a2540] rounded-tr-xl shadow-[4px_-4px_32px_rgba(0,0,0,0.5)] slide-in-right max-h-[420px]">

      {/* Header */}
      <div className="flex items-center justify-between px-4 py-3 border-b border-[#1a2540] shrink-0">
        <div className="text-xs font-semibold text-white">Change Impact</div>
        <button
          onClick={clearSelection}
          className="w-6 h-6 rounded flex items-center justify-center text-slate-500 hover:text-slate-200 hover:bg-slate-800 transition-colors"
        >
          <CloseIcon />
        </button>
      </div>

      {impactLoading && (
        <div className="flex items-center justify-center py-8 text-slate-500 text-xs">
          Analysing impact…
        </div>
      )}

      {impact && !impactLoading && (
        <div className="flex-1 overflow-y-auto">
          {/* Node name + risk */}
          <div className="px-4 py-3 border-b border-[#1a2540]">
            <div className="flex items-start justify-between gap-2">
              <div>
                <div className="flex items-center gap-1.5">
                  <span className="text-sm">{SEMANTIC_ICONS[impact.semantic_kind] || "⚙️"}</span>
                  <span className="font-mono text-sm text-white">{impact.node_name}()</span>
                </div>
                <div className="text-[10px] text-slate-500 mt-0.5 truncate">{impact.file_path?.split("/").slice(-2).join("/")}</div>
              </div>
              {risk && (
                <span
                  className={`shrink-0 text-[9px] font-bold px-2 py-0.5 rounded-full border ${risk.bg} ${risk.border}`}
                  style={{ color: risk.color }}
                >
                  {risk.label} RISK
                </span>
              )}
            </div>
          </div>

          {/* Stats row */}
          <div className="grid grid-cols-3 border-b border-[#1a2540]">
            {[
              { label: "Callers",  value: impact.caller_count },
              { label: "DB ops",   value: impact.db_interactions?.length || 0 },
              { label: "Modules",  value: impact.affected_modules?.length || 0 },
            ].map(({ label, value }) => (
              <div key={label} className="flex flex-col items-center py-2.5 px-1 border-r border-[#1a2540] last:border-0">
                <span className="text-base font-bold text-white">{value}</span>
                <span className="text-[9px] text-slate-500 mt-0.5">{label}</span>
              </div>
            ))}
          </div>

          {/* DB interactions */}
          {impact.db_interactions?.length > 0 && (
            <div className="px-4 py-2.5 border-b border-[#1a2540]">
              <div className="text-[9px] uppercase tracking-widest text-slate-600 font-semibold mb-1.5">DB Operations</div>
              {impact.db_interactions.slice(0, 4).map((fn) => (
                <div key={fn} className="flex items-center gap-1.5 text-[11px] text-purple-300 py-0.5">
                  <span>🗄️</span><span className="font-mono truncate">{fn}()</span>
                </div>
              ))}
            </div>
          )}

          {/* Affected modules */}
          {impact.affected_modules?.length > 0 && (
            <div className="px-4 py-2.5 border-b border-[#1a2540]">
              <div className="text-[9px] uppercase tracking-widest text-slate-600 font-semibold mb-1.5">Affected Modules</div>
              {impact.affected_modules.slice(0, 4).map((mod) => (
                <div key={mod} className="flex items-center gap-1.5 text-[11px] text-blue-300 py-0.5">
                  <span>📦</span><span className="truncate">{mod}</span>
                </div>
              ))}
            </div>
          )}

          {/* Documentation references (knowledge layer) */}
          {nodeRefs?.referenced_in?.length > 0 && (
            <div className="px-4 py-2.5 border-b border-[#1a2540]">
              <div className="text-[9px] uppercase tracking-widest text-slate-600 font-semibold mb-1.5">
                Referenced in docs
              </div>
              {nodeRefs.referenced_in.slice(0, 4).map((ref, i) => (
                <div key={i} className="flex items-center gap-1.5 py-0.5 min-w-0">
                  <span className="text-[11px]">📄</span>
                  <span className="text-[11px] text-cyan-300 truncate" title={ref.doc_path}>
                    {ref.document}
                    {ref.section ? <span className="text-slate-500"> › {ref.section}</span> : null}
                  </span>
                  <ProvenanceBadge provenance={ref.provenance} />
                </div>
              ))}
            </div>
          )}

          {/* Developer rationale (TODO/FIXME/HACK/WHY comments in this node) */}
          {nodeRefs?.rationale?.length > 0 && (
            <div className="px-4 py-2.5 border-b border-[#1a2540]">
              <div className="text-[9px] uppercase tracking-widest text-slate-600 font-semibold mb-1.5">
                Developer rationale
              </div>
              {nodeRefs.rationale.slice(0, 3).map((r) => (
                <div key={r.id} className="py-0.5">
                  <span className={`text-[9px] font-bold mr-1.5 ${MARKER_COLORS[r.marker] || "text-slate-300"}`}>
                    {r.marker}
                  </span>
                  <span className="text-[10px] text-slate-400">{r.text}</span>
                </div>
              ))}
            </div>
          )}

          {/* Callers */}
          {impact.callers?.length > 0 && (
            <div className="px-4 py-2.5">
              <div className="text-[9px] uppercase tracking-widest text-slate-600 font-semibold mb-1.5">
                Called by ({impact.caller_count})
              </div>
              {impact.callers.slice(0, 5).map((n) => (
                <CallerRow key={n.id} node={n} />
              ))}
              {impact.caller_count > 5 && (
                <div className="text-[10px] text-slate-600 mt-1">+{impact.caller_count - 5} more</div>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
