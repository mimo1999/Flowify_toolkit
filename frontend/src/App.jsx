import React, { useEffect } from "react";
import { ReactFlowProvider } from "reactflow";
import GraphView from "./components/GraphView.jsx";
import Sidebar from "./components/Sidebar.jsx";
import QueryPanel from "./components/QueryPanel.jsx";
import QueryResults from "./components/QueryResults.jsx";
import ImpactPanel from "./components/ImpactPanel.jsx";
import SummaryPanel from "./components/SummaryPanel.jsx";
import InsightsPanel from "./components/InsightsPanel.jsx";
import { useFlowStore } from "./store.js";

export default function App() {
  const loadInitial = useFlowStore((s) => s.loadInitial);
  const loadDepthView = useFlowStore((s) => s.loadDepthView);
  const runQuery = useFlowStore((s) => s.runQuery);

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
        if (nodeId) setTimeout(() => useFlowStore.getState().selectNode(nodeId), 800);
        if (q) setTimeout(() => useFlowStore.getState().runQuery(q), 1500);
      });
    }
  }, []);

  return (
    <div className="flex h-screen w-screen overflow-hidden bg-[#07090f]">
      <Sidebar />
      <div className="flex flex-col flex-1 min-w-0">
        {/* Graph canvas */}
        <div className="flex-1 min-h-0 relative">
          <ReactFlowProvider>
            <GraphView />
          </ReactFlowProvider>
          {/* Query results panel overlays the graph on the right */}
          <QueryResults />
          {/* Change impact panel overlays the graph on the bottom-left */}
          <ImpactPanel />
        </div>
        {/* Query bar at bottom */}
        <QueryPanel />
      </div>
      {/* Repository summary modal */}
      <SummaryPanel />
      {/* Graph insights modal */}
      <InsightsPanel />
    </div>
  );
}
