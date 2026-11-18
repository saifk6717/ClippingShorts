"use client";
import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { Meter, PageHeader, PlatformBadge, Sparkline, Stat, StatusBadge } from "@/components/common";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { api } from "@/lib/api";
import { useLive } from "@/lib/events";
import type { Stream } from "@/lib/types";
import { fmtAgo, fmtMB } from "@/lib/utils";

interface Dash {
  streams_total: number; streams_active: number; streams_disconnected: number; highlights: number; candidates: number;
  clips_pending_review: number; clips_ready: number; clips_rendered: number; clips_uploaded: number; renders_active: number;
  ai_cost_today_usd: number; ai_calls_today: number;
  resources: { cpu_pct?: number; mem_pct?: number; mem_total_mb?: number; mem_available_mb?: number; disk?: { pct: number; free_gb: number; total_gb: number }; level?: string; reasons?: string[]; load1?: number; cpu_count?: number };
  storage: { dirs?: Record<string, number>; total_mb?: number; limit_gb?: number };
  health: { ok: boolean; redis: boolean; database: boolean; workers: Record<string, { ok: boolean; inflight?: Record<string, number>; children?: number; rss_mb?: number }> };
  latency: Record<string, { n: number; median?: number; p90?: number }>;
  limits: Record<string, number>;
  activity: { t: number; stream_id: string; text: string; level: string }[];
}

export default function Dashboard() {
  const { data: d } = useQuery({ queryKey: ["dashboard"], queryFn: () => api<Dash>("/api/dashboard"), refetchInterval: 5000 });
  const { data: streams } = useQuery({ queryKey: ["streams"], queryFn: () => api<{ streams: Stream[] }>("/api/streams"), refetchInterval: 4000 });
  const { snapshot, events } = useLive();
  const res = snapshot?.resources?.cpu_pct != null ? { ...d?.resources, ...snapshot.resources } : d?.resources;
  const activity = [
    ...events.filter((e) => e.type === "activity").map((e) => ({ t: e.t, text: String(e.text), level: String(e.level), stream_id: String(e.stream_id) })),
    ...(d?.activity ?? []),
  ].filter((a, i, arr) => arr.findIndex((b) => b.t === a.t && b.text === a.text) === i).slice(0, 40);
  const names = Object.fromEntries((streams?.streams ?? []).map((s) => [s.id, s.creator_name || s.channel]));

  return (
    <div className="space-y-5">
      <PageHeader title="Dashboard" description="Live overview of every stream, highlight and job.">
        <Button asChild size="sm"><Link href="/streams/new">Add streams</Link></Button>
      </PageHeader>

      {res?.level && res.level !== "normal" && (
        <div className={`rounded-lg border p-3 text-sm ${res.level === "critical" ? "border-destructive/50 bg-destructive/10 text-destructive" : "border-warning/50 bg-warning/10 text-warning"}`}>
          Resource pressure ({res.level}): {res.reasons?.join("; ")}. New streams and renders wait; live ingestion continues.
        </div>
      )}

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4 xl:grid-cols-7">
        <Stat label="Configured streams" value={d?.streams_total ?? "–"} hint={`max ${d?.limits?.max_streams ?? "–"}`} />
        <Stat label="Active" value={d?.streams_active ?? "–"} tone="success" />
        <Stat label="Disconnected" value={d?.streams_disconnected ?? "–"} tone={d?.streams_disconnected ? "warning" : undefined} />
        <Stat label="Highlights detected" value={d?.highlights ?? "–"} hint={`${d?.candidates ?? 0} candidates judged`} />
        <Stat label="Awaiting review" value={d?.clips_pending_review ?? "–"} />
        <Stat label="Rendered" value={d?.clips_rendered ?? "–"} hint={d?.renders_active ? `${d.renders_active} in progress` : undefined} />
        <Stat label="Uploaded" value={d?.clips_uploaded ?? "–"} />
      </div>

      <div className="grid gap-4 lg:grid-cols-3">
        <Card>
          <CardHeader><CardTitle>System resources</CardTitle></CardHeader>
          <CardContent className="space-y-3">
            <Meter label="CPU" pct={res?.cpu_pct} detail={res?.load1 != null ? `load ${res.load1} on ${res.cpu_count} cores` : undefined} />
            <Meter label="RAM" pct={res?.mem_pct} detail={d?.resources?.mem_available_mb != null ? `${fmtMB(d.resources.mem_available_mb)} free of ${fmtMB(d.resources.mem_total_mb)}` : undefined} />
            <Meter label="Disk" pct={res?.disk?.pct} detail={d?.resources?.disk ? `${d.resources.disk.free_gb} GB free` : undefined} />
            <div className="text-xs text-muted-foreground">
              Storage used: {fmtMB(d?.storage?.total_mb)} of {d?.storage?.limit_gb ?? "–"} GB limit · FFmpeg {d?.limits?.ffmpeg_in_use ?? 0}/{d?.limits?.ffmpeg ?? "–"}
            </div>
          </CardContent>
        </Card>
        <Card>
          <CardHeader><CardTitle>Health</CardTitle></CardHeader>
          <CardContent className="space-y-2 text-sm">
            <HealthRow label="Redis" ok={d?.health?.redis} />
            <HealthRow label="Database" ok={d?.health?.database} />
            <HealthRow label="Stream worker" ok={d?.health?.workers?.stream?.ok} extra={d?.health?.workers?.stream?.ok ? `${d.health.workers.stream.children ?? 0} streams · ${fmtMB(d.health.workers.stream.rss_mb)}` : undefined} />
            <HealthRow label="AI worker" ok={d?.health?.workers?.ai?.ok} extra={d?.health?.workers?.ai?.inflight ? Object.entries(d.health.workers.ai.inflight).map(([k, v]) => `${k.split("_")[0]} ${v}`).join(" · ") : undefined} />
            <HealthRow label="Renderer" ok={d?.health?.workers?.renderer?.ok} />
            <div className="pt-1 text-xs text-muted-foreground">AI today: {d?.ai_calls_today ?? 0} calls · est. ${d?.ai_cost_today_usd?.toFixed(3) ?? "0"}</div>
          </CardContent>
        </Card>
        <Card>
          <CardHeader><CardTitle>Measured latency (median / p90)</CardTitle></CardHeader>
          <CardContent className="space-y-1.5 text-sm">
            {[["transcription_latency", "Transcription"], ["ai_latency", "AI judging"], ["extract_latency", "Clip extraction"], ["render_latency", "Rendering"], ["upload_latency", "Upload"]].map(([k, label]) => (
              <div key={k} className="flex justify-between"><span className="text-muted-foreground">{label}</span>
                <span className="tabular-nums">{d?.latency?.[k]?.n ? `${d.latency[k].median}s / ${d.latency[k].p90}s (n=${d.latency[k].n})` : "no data yet"}</span></div>
            ))}
          </CardContent>
        </Card>
      </div>

      <div className="grid gap-4 lg:grid-cols-5">
        <Card className="lg:col-span-3">
          <CardHeader><CardTitle>Stream activity</CardTitle></CardHeader>
          <CardContent className="space-y-2">
            {(streams?.streams ?? []).length === 0 && <p className="text-sm text-muted-foreground">No streams yet. <Link className="text-primary" href="/streams/new">Add some</Link>.</p>}
            {(streams?.streams ?? []).map((s) => {
              const snap = snapshot?.streams?.[s.id];
              const levels = s.live?.audio?.levels ?? [];
              return (
                <Link key={s.id} href="/streams" className="grid grid-cols-[1fr_auto] items-center gap-3 rounded-md border p-2 hover:bg-accent/40 sm:grid-cols-[minmax(0,1.4fr)_100px_90px_minmax(0,1fr)]">
                  <div className="min-w-0">
                    <div className="flex items-center gap-2"><span className="truncate text-sm font-medium">{s.creator_name || s.channel}</span><PlatformBadge platform={s.platform} /></div>
                    <div className="truncate text-xs text-muted-foreground">{s.live?.title || s.url}</div>
                  </div>
                  <StatusBadge status={snap?.status || s.status} />
                  <div className="hidden text-xs tabular-nums text-muted-foreground sm:block">{(snap?.chat?.msgs_per_sec ?? s.live?.chat?.msgs_per_sec ?? 0).toFixed(1)} msg/s<br />{snap?.chat?.unique_60s ?? s.live?.chat?.unique_60s ?? 0} chatters</div>
                  <Sparkline values={levels} min={-60} max={0} className="hidden sm:block" />
                </Link>
              );
            })}
          </CardContent>
        </Card>
        <Card className="lg:col-span-2">
          <CardHeader><CardTitle>Activity</CardTitle></CardHeader>
          <CardContent className="max-h-[420px] space-y-1.5 overflow-y-auto">
            {activity.length === 0 && <p className="text-sm text-muted-foreground">Nothing yet.</p>}
            {activity.map((a, i) => (
              <div key={i} className="flex gap-2 text-xs">
                <span className="w-14 shrink-0 text-muted-foreground">{fmtAgo(a.t)}</span>
                <span className={a.level === "error" ? "text-destructive" : a.level === "warn" ? "text-warning" : ""}>
                  {a.stream_id && names[a.stream_id] ? <Badge variant="secondary" className="mr-1">{names[a.stream_id]}</Badge> : null}{a.text}
                </span>
              </div>
            ))}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}

function HealthRow({ label, ok, extra }: { label: string; ok?: boolean; extra?: string }) {
  return (
    <div className="flex items-center justify-between gap-2">
      <span className="text-muted-foreground">{label}</span>
      <span className="flex items-center gap-2 text-xs">
        {extra && <span className="text-muted-foreground">{extra}</span>}
        <Badge variant={ok == null ? "secondary" : ok ? "success" : "destructive"}>{ok == null ? "unknown" : ok ? "ok" : "down"}</Badge>
      </span>
    </div>
  );
}
