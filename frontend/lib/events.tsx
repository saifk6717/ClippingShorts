"use client";
// One Server-Sent Events connection per tab. Worker events refresh the matching queries; the 3-second snapshot
// feeds the live stream panels. If the connection drops, the browser reconnects by itself.
import { useQueryClient } from "@tanstack/react-query";
import { createContext, useContext, useEffect, useState } from "react";

export interface Snapshot {
  streams: Record<string, { status: string; desired: string; chat: { msgs_per_sec?: number; unique_60s?: number }; audio: { db?: number; excitement?: number }; t?: number; worker?: { rss_mb?: number; cpu_pct?: number } }>;
  resources: { cpu_pct?: number; mem_pct?: number; disk?: { pct: number }; level?: string; reasons?: string[] };
}
export interface LiveEvent {
  type: string;
  t: number;
  [k: string]: unknown;
}

const Ctx = createContext<{ snapshot: Snapshot | null; events: LiveEvent[]; connected: boolean }>({ snapshot: null, events: [], connected: false });

export function LiveProvider({ children }: { children: React.ReactNode }) {
  const qc = useQueryClient();
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [events, setEvents] = useState<LiveEvent[]>([]);
  const [connected, setConnected] = useState(false);

  useEffect(() => {
    const es = new EventSource("/api/events");
    es.onopen = () => setConnected(true);
    es.onerror = () => setConnected(false);
    es.addEventListener("snapshot", (e) => setSnapshot(JSON.parse((e as MessageEvent).data)));
    const onEvent = (e: MessageEvent) => {
      const ev = JSON.parse(e.data) as LiveEvent;
      setEvents((prev) => [ev, ...prev].slice(0, 60));
      if (ev.type === "edit" || ev.type === "upload") {
        qc.invalidateQueries({ queryKey: ["clip"] });
        qc.invalidateQueries({ queryKey: ["jobs"] });
      }
      if (ev.type === "clip" || ev.type === "highlight") {
        qc.invalidateQueries({ queryKey: ["clips"] });
        qc.invalidateQueries({ queryKey: ["dashboard"] });
      }
      if (ev.type === "stream") qc.invalidateQueries({ queryKey: ["streams"] });
    };
    for (const t of ["edit", "upload", "clip", "highlight", "stream", "spike", "activity"]) {
      es.addEventListener(t, onEvent as EventListener);
    }
    return () => es.close();
  }, [qc]);

  return <Ctx.Provider value={{ snapshot, events, connected }}>{children}</Ctx.Provider>;
}

export const useLive = () => useContext(Ctx);
