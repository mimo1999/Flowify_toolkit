import React from "react";
import { ReactFlowProvider } from "reactflow";
import GraphView from "./components/GraphView.jsx";
import Sidebar from "./components/Sidebar.jsx";
import QueryPanel from "./components/QueryPanel.jsx";

export default function App() {
  return (
    <div className="flex h-screen w-screen">
      <Sidebar />
      <main className="flex-1 flex flex-col">
        <div className="flex-1 min-h-0">
          <ReactFlowProvider>
            <GraphView />
          </ReactFlowProvider>
        </div>
        <QueryPanel />
      </main>
    </div>
  );
}
