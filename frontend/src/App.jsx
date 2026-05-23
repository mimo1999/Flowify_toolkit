import React, { useEffect, useState } from "react";
import { ReactFlowProvider } from "reactflow";
import GraphView from "./components/GraphView.jsx";
import Sidebar from "./components/Sidebar.jsx";
import QueryPanel from "./components/QueryPanel.jsx";

function StubBanner() {
  const [info, setInfo] = useState(null);

  useEffect(() => {
    fetch("/api/provider_info")
      .then((r) => r.json())
      .then(setInfo)
      .catch(() => {}); // silently ignore if backend is down
  }, []);

  if (!info || !info.is_stub) return null;

  return (
    <div className="flex items-center gap-2 px-4 py-1.5 bg-amber-950/70 border-b border-amber-800 text-amber-300 text-xs">
      <span className="text-amber-400">⚠</span>
      <span>
        <strong>Stub mode</strong> — AI explanations and queries are placeholders.
        Set <code className="bg-amber-900/50 px-1 rounded">ANTHROPIC_API_KEY</code>,{" "}
        <code className="bg-amber-900/50 px-1 rounded">BOB_API_KEY</code>, or{" "}
        <code className="bg-amber-900/50 px-1 rounded">OPENAI_API_KEY</code> to enable real AI responses.
      </span>
    </div>
  );
}

export default function App() {
  return (
    <div className="flex flex-col h-screen w-screen">
      <StubBanner />
      <div className="flex flex-1 min-h-0">
        <Sidebar />
        <main className="flex-1 flex flex-col min-w-0">
          <div className="flex-1 min-h-0">
            <ReactFlowProvider>
              <GraphView />
            </ReactFlowProvider>
          </div>
          <QueryPanel />
        </main>
      </div>
    </div>
  );
}
