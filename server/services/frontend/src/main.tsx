import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter, Route, Routes } from "react-router-dom";
import Shell from "./components/Shell";
import { BASE } from "./lib/api";
import Help from "./pages/Help";
import Login from "./pages/Login";
import Cards from "./pages/Cards";
import Schedule from "./pages/Schedule";
import Walls from "./pages/Walls";
import SettingsPage from "./pages/Settings";
import Shows from "./pages/Shows";
import "./globals.css";

// Live state arrives over the websocket; queries do not poll on a timer.
const qc = new QueryClient({ defaultOptions: { queries: { staleTime: 5_000, refetchInterval: false } } });

function useRealtime() {
  React.useEffect(() => {
    const url = new URL(BASE + "/ws", location.href);
    url.protocol = url.protocol.replace("http", "ws");
    let ws: WebSocket | undefined;
    try {
      ws = new WebSocket(url.toString());
      ws.onmessage = ev => {
        try {
          const m = JSON.parse(ev.data);
          // A card's state changing can move a wall's status chip, so both
          // caches are invalidated. The server still emits the pre-v0.11.0
          // event name; there is no value in a flag day for a string.
          if (m.type === "panel_update" || m.type === "card_update") {
            qc.invalidateQueries({ queryKey: ["cards"] });
            qc.invalidateQueries({ queryKey: ["walls"] });
            qc.invalidateQueries({ queryKey: ["wall-layout"] });
          }
          if (m.type === "show_update") qc.invalidateQueries({ queryKey: ["shows"] });
        } catch {}
      };
    } catch {}
    return () => ws?.close();
  }, []);
}

function App() {
  useRealtime();
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route path="*" element={
        <Shell>
          <Routes>
            <Route path="/" element={<Walls />} />
            <Route path="/cards" element={<Cards />} />
            <Route path="/shows" element={<Shows />} />
            <Route path="/schedule" element={<Schedule />} />
            <Route path="/settings" element={<SettingsPage />} />
            <Route path="/help" element={<Help />} />
          </Routes>
        </Shell>} />
    </Routes>
  );
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <QueryClientProvider client={qc}>
      <BrowserRouter basename={BASE}><App /></BrowserRouter>
    </QueryClientProvider>
  </React.StrictMode>
);
