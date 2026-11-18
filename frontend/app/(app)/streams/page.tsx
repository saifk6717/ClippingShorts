"use client";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Film, Pause, Play, RotateCw, Scissors, Settings2, Trash2 } from "lucide-react";
import Link from "next/link";
import { useState } from "react";
import { toast } from "sonner";
import { PageHeader, PlatformBadge, Sparkline, StatusBadge } from "@/components/common";
import { DetectionSettingsForm, type DetectionSettings } from "@/components/stream-settings";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { del, patch, post, api } from "@/lib/api";
import { useLive } from "@/lib/events";
import type { Stream } from "@/lib/types";
import { fmtAgo, fmtDuration, fmtMB } from "@/lib/utils";

export default function Monitoring() {
  const qc = useQueryClient();
  const { data, isLoading } = useQuery({ queryKey: ["streams"], queryFn: () => api<{ streams: Stream[] }>("/api/streams"), refetchInterval: 2500 });
  const [editing, setEditing] = useState<Stream | null>(null);
  const act = useMutation({
    mutationFn: ({ path, method }: { path: string; method?: "DELETE" }) => (method === "DELETE" ? del(path) : post(path)),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["streams"] }),
    onError: (e: Error) => toast.error(e.message),
  });
  const streams = data?.streams ?? [];

  return (
    <div className="space-y-4">
      <PageHeader title="Stream monitoring" description="Each stream runs in its own worker process; one failing never affects the others.">
        <Button size="sm" variant="outline" onClick={() => act.mutate({ path: "/api/streams/start-all" })}><Play /> Start all</Button>
        <Button size="sm" variant="outline" onClick={() => act.mutate({ path: "/api/streams/stop-all" })}><Pause /> Stop all</Button>
        <Button size="sm" asChild><Link href="/streams/new">Add streams</Link></Button>
      </PageHeader>
      {isLoading && <p className="text-sm text-muted-foreground">Loading…</p>}
      {!isLoading && streams.length === 0 && <Card><CardContent className="p-6 text-sm text-muted-foreground">No streams configured. <Link href="/streams/new" className="text-primary">Add your first streams</Link>.</CardContent></Card>}
      <div className="grid gap-4 xl:grid-cols-2">
        {streams.map((s) => <StreamCard key={s.id} s={s} onAct={(path, method) => act.mutate({ path, method })} onEdit={() => setEditing(s)} />)}
      </div>
      {editing && <SettingsDialog stream={editing} onClose={() => setEditing(null)} />}
    </div>
  );
}

function StreamCard({ s, onAct, onEdit }: { s: Stream; onAct: (path: string, method?: "DELETE") => void; onEdit: () => void }) {
  const { snapshot } = useLive();
  const live = s.live || {};
  const status = snapshot?.streams?.[s.id]?.status || s.status;
  const running = s.desired_state === "running";
  const tr = live.transcription;
  const spike = live.detector?.last_spike;
  const w = live.worker;
  return (
    <Card>
      <CardContent className="space-y-3 p-4">
        <div className="flex flex-wrap items-start justify-between gap-2">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-medium">{s.creator_name || s.channel}</span>
              <PlatformBadge platform={s.platform} />
              <StatusBadge status={status} />
              {!s.enabled && <Badge variant="secondary">disabled</Badge>}
            </div>
            <a href={s.url} target="_blank" rel="noreferrer noopener" className="block truncate text-xs text-muted-foreground hover:text-foreground">{s.url}</a>
            {live.title && <div className="truncate text-xs">{live.title}</div>}
          </div>
          <div className="flex flex-wrap gap-1">
            {running ? <Button size="sm" variant="outline" onClick={() => onAct(`/api/streams/${s.id}/stop`)}><Pause /> Stop</Button>
              : <Button size="sm" onClick={() => onAct(`/api/streams/${s.id}/start`)} disabled={!s.enabled}><Play /> Start</Button>}
            <Button size="sm" variant="ghost" title="Restart worker" onClick={() => onAct(`/api/streams/${s.id}/restart`)}><RotateCw /></Button>
            <Button size="sm" variant="ghost" title="Clip the last minute now" disabled={status !== "live"} onClick={() => onAct(`/api/streams/${s.id}/clip?seconds=60`)}><Scissors /></Button>
            <Button size="sm" variant="ghost" title="Detection settings" onClick={onEdit}><Settings2 /></Button>
            <Button size="sm" variant="ghost" title="View clips" asChild><Link href={`/clips?stream_id=${s.id}`}><Film /></Link></Button>
            <Button size="sm" variant="ghost" title="Delete stream" onClick={() => confirm("Delete this stream? Saved clips stay in the library.") && onAct(`/api/streams/${s.id}`, "DELETE")}><Trash2 /></Button>
          </div>
        </div>
        {(live.error || s.last_error) && status !== "live" && <p className="rounded-md bg-warning/10 p-2 text-xs text-warning">{live.error || s.last_error}</p>}
        <div className="grid grid-cols-2 gap-x-4 gap-y-2 text-xs sm:grid-cols-4">
          <Metric label="Monitored" value={live.started_at ? fmtDuration(Date.now() / 1000 - live.started_at) : "–"} />
          <Metric label="Chat msg/s" value={live.chat?.msgs_per_sec?.toFixed(1) ?? "–"} sub={live.chat?.connected === false ? (live.chat?.error || "chat offline") : `base ${live.chat?.baseline_msgs_per_sec ?? 0}`} />
          <Metric label="Chatters (60s)" value={live.chat?.unique_60s ?? "–"} sub={live.chat?.top_user_share ? `top user ${(live.chat.top_user_share * 100).toFixed(0)}%` : undefined} />
          <Metric label="Audio" value={live.audio?.db != null ? `${live.audio.db.toFixed(0)} dB` : "–"} sub={live.audio?.baseline_db != null ? `base ${live.audio.baseline_db} · z ${live.audio.z}` : "warming up"} />
          <Metric label="Transcription" value={tr ? (tr.ok ? `${tr.provider}` : "error") : "–"} sub={tr ? `lag ${tr.lag ?? 0}s · ${tr.latency ?? 0}s/chunk${tr.dropped ? ` · ${tr.dropped} dropped` : ""}` : undefined} warn={tr && !tr.ok} />
          <Metric label="Last spike" value={spike ? fmtAgo(spike.at) : "none"} sub={spike ? `score ${spike.score}` : undefined} />
          <Metric label="Highlights" value={live.detector?.candidates ?? 0} sub={`${live.detector?.open ?? 0} waiting · ${live.buffer?.seconds ?? 0}s buffered`} />
          <Metric label="Worker" value={w?.rss_mb != null ? fmtMB(w.rss_mb) : "–"} sub={w?.cpu_pct != null ? `CPU ${w.cpu_pct}% · ${w.procs} procs` : undefined} />
        </div>
        <div className="grid grid-cols-2 gap-3">
          <div><div className="mb-0.5 text-[11px] text-muted-foreground">Audio level (60 s)</div><Sparkline values={live.audio?.levels ?? []} min={-60} max={0} /></div>
          <div><div className="mb-0.5 text-[11px] text-muted-foreground">Highlight score (60 s)</div><Sparkline values={live.detector?.scores ?? []} min={0} max={100} color="hsl(var(--warning))" /></div>
        </div>
        {tr?.error && !tr.ok && <p className="text-xs text-destructive">Transcription: {tr.error}</p>}
      </CardContent>
    </Card>
  );
}

function Metric({ label, value, sub, warn }: { label: string; value: React.ReactNode; sub?: string; warn?: boolean }) {
  return (
    <div className="min-w-0">
      <div className="text-muted-foreground">{label}</div>
      <div className={`truncate text-sm font-medium tabular-nums ${warn ? "text-destructive" : ""}`}>{value}</div>
      {sub && <div className="truncate text-[11px] text-muted-foreground">{sub}</div>}
    </div>
  );
}

function SettingsDialog({ stream, onClose }: { stream: Stream; onClose: () => void }) {
  const qc = useQueryClient();
  const [value, setValue] = useState<DetectionSettings>(stream.settings || {});
  const save = useMutation({
    mutationFn: () => patch(`/api/streams/${stream.id}`, { settings: value }),
    onSuccess: () => { toast.success("Settings saved; the worker picks them up now."); qc.invalidateQueries({ queryKey: ["streams"] }); onClose(); },
    onError: (e: Error) => toast.error(e.message),
  });
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-3xl">
        <DialogHeader><DialogTitle>Detection settings · {stream.creator_name || stream.channel}</DialogTitle></DialogHeader>
        <DetectionSettingsForm value={value} onChange={setValue} />
        <DialogFooter>
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
          <Button onClick={() => save.mutate()} disabled={save.isPending}>Save</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
