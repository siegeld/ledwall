import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter, Route, Routes } from "react-router-dom";
import Shell from "./components/Shell";
import { BASE } from "./lib/api";
import Help from "./pages/Help";
import Login from "./pages/Login";
import Cards from "./pages/Cards";
import Firmware from "./pages/Firmware";
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
    let closed = false;
    // Reconnect when the socket drops (an API restart, a proxy timeout) --
    // otherwise the page silently stops updating until it is reloaded.
    const connect = () => {
      try {
        ws = new WebSocket(url.toString());
        ws.onclose = () => { if (!closed) setTimeout(connect, 5000); };
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
      } catch { if (!closed) setTimeout(connect, 5000); }
    };
    connect();
    return () => { closed = true; ws?.close(); };
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
            <Route path="/updates" element={<Firmware />} />
            {/* The page was called "firmware" before it also pushed bitstreams.
                Kept so existing links keep working. */}
            <Route path="/firmware" element={<Firmware />} />
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
