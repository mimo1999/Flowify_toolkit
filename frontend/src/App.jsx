import React, { useEffect } from "react";
import { ReactFlowProvider } from "reactflow";
import GraphView from "./components/GraphView.jsx";
import Sidebar from "./components/Sidebar.jsx";
import QueryPanel from "./components/QueryPanel.jsx";
import QueryResults from "./components/QueryResults.jsx";
import Inspector from "./components/Inspector.jsx";
import ReviewPanel from "./components/ReviewPanel.jsx";
import TracePanel from "./components/TracePanel.jsx";
import SummaryPanel from "./components/SummaryPanel.jsx";
import InsightsPanel from "./components/InsightsPanel.jsx";
import WelcomeModal from "./components/WelcomeModal.jsx";
import { useFlowStore } from "./store.js";

export default function App() {
  const loadInitial = useFlowStore((s) => s.loadInitial);
  const loadDepthView = useFlowStore((s) => s.loadDepthView);
  const runQuery = useFlowStore((s) => s.runQuery);
  const reviewOpen = useFlowStore((s) => s.reviewOpen);

  // Support ?graph_id=<id>&repo_path=<path>&depth=<1|2|3>&node_id=<id> URL params for direct loading
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const gid = params.get("graph_id");
    const rp = params.get("repo_path");
    const depth = params.get("depth");
    const nodeId = params.get("node_id");
    if (gid) {
      useFlowStore.setState({ graphId: gid, repoPath: rp || gid });
      const q = params.get("query");
      const doLoad = depth
        ? () => loadDepthView(parseInt(depth, 10))
        : () => loadInitial();
      doLoad().then?.(() => {
        // Only proceed with follow-on actions if the load actually succeeded
        if (useFlowStore.getState().error) return;
        if (nodeId) setTimeout(() => useFlowStore.getState().revealFunction(nodeId), 800);
        if (q) setTimeout(() => useFlowStore.getState().runQuery(q), 1500);
      });
    }
  }, []);

  // Width of whichever right-hand panel is showing (widths match each panel's own class).
  const dock = useFlowStore((s) => {
    const fn = s.selectedId && !s.selectedId.startsWith("file::") && !s.selectedId.startsWith("mod_");
    if (fn && (s.impactData || s.impactLoading)) return 420;
    if (s.reviewOpen) return 420;
    if (s.traceOpen) return 360;
    return s.explanation ? 340 : 0;
  });

  return (
    <div className="flex h-screen w-screen overflow-hidden bg-[#07090f]">
      <Sidebar />
      <div className="flex flex-col flex-1 min-w-0">
        {/* Graph canvas */}
        <div className="flex-1 min-h-0 relative">
          {/* The canvas ends where the open right-hand panel begins, so nothing is hidden under it */}
          <div className="absolute inset-y-0 left-0" style={{ right: dock }}>
            <ReactFlowProvider>
              <GraphView dock={dock} />
            </ReactFlowProvider>
          </div>
          {/* Query results panel overlays the graph on the right */}
          <QueryResults />
          {/* Dry-run trace picker + step list, same side of the graph */}
          <TracePanel />
          <ReviewPanel />
          {/* Selected-function inspector docks right and sits above the other right-hand panels */}
          <Inspector />
        </div>
        {/* Query bar at bottom: an Explore tool, hidden while reviewing */}
        {!reviewOpen && <QueryPanel />}
      </div>
      {/* Repository summary modal */}
      <SummaryPanel />
      {/* Graph insights modal */}
      <InsightsPanel />
      {/* First-visit onboarding modal (reopenable via the sidebar's "?" button) */}
      <WelcomeModal />
    </div>
  );
}
