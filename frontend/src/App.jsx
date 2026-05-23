import React from "react";
import { ReactFlowProvider } from "reactflow";
import GraphView from "./components/GraphView.jsx";
import Sidebar from "./components/Sidebar.jsx";
import QueryPanel from "./components/QueryPanel.jsx";
import QueryResults from "./components/QueryResults.jsx";

export default function App() {
  return (
    <div className="flex h-screen w-screen overflow-hidden bg-[#07090f]">
      <Sidebar />
      <div className="flex flex-col flex-1 min-w-0">
        {/* Graph canvas */}
        <div className="flex-1 min-h-0 relative">
          <ReactFlowProvider>
            <GraphView />
          </ReactFlowProvider>
          {/* Results panel overlays the graph on the right */}
          <QueryResults />
        </div>
        {/* Query bar at bottom */}
        <QueryPanel />
      </div>
    </div>
  );
}
