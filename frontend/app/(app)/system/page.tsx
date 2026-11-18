"use client";
import { useQuery } from "@tanstack/react-query";
import { Meter, PageHeader } from "@/components/common";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { TBody, TD, TH, THead, TR, Table } from "@/components/ui/table";
import { api } from "@/lib/api";
import { fmtMB } from "@/lib/utils";

interface Storage { dirs: Record<string, number>; total_mb: number; limit_gb: number; disk_free_gb: number; disk_total_gb: number; retention_days: number; t: number }
interface Usage { rows: { service: string; model: string; purpose: string; calls: number; units_in: number; units_out: number; avg_latency_ms: number; est_cost_usd: number; failures: number }[];
  today: Record<string, number>; pricing_note: string }
interface Resources { cpu_pct: number; mem_pct: number; mem_total_mb: number; mem_available_mb: number; disk: { pct: number; free_gb: number }; level: string; reasons: string[];
  workers: Record<string, { ok: boolean; container?: { used_mb?: number; limit_mb?: number; pct?: number } }>;
  streams: Record<string, { rss_mb: number; cpu_pct: number; procs: number } | null>; limits: Record<string, number> }

export default function SystemPage() {
  const storage = useQuery({ queryKey: ["storage"], queryFn: () => api<Storage>("/api/system/storage"), refetchInterval: 30000 });
  const usage = useQuery({ queryKey: ["usage"], queryFn: () => api<Usage>("/api/system/usage?days=1"), refetchInterval: 30000 });
  const res = useQuery({ queryKey: ["resources"], queryFn: () => api<Resources>("/api/system/resources"), refetchInterval: 3000 });
  const cfg = useQuery({ queryKey: ["config"], queryFn: () => api<Record<string, string | number | boolean>>("/api/system/config") });
  const ch = useQuery({ queryKey: ["yt-channel"], queryFn: () => api<{ configured: boolean; title?: string; id?: string; error?: string }>("/api/youtube/channel") });
  const r = res.data;
  return (
    <div className="space-y-4">
      <PageHeader title="System" description="Resources, storage, AI usage and configuration." />
      <div className="grid gap-4 lg:grid-cols-3">
        <Card><CardHeader><CardTitle>Machine <Badge variant={r?.level === "normal" ? "success" : r?.level === "critical" ? "destructive" : "warning"}>{r?.level ?? "…"}</Badge></CardTitle></CardHeader>
          <CardContent className="space-y-3">
            <Meter label="CPU" pct={r?.cpu_pct} /><Meter label="RAM" pct={r?.mem_pct} detail={r ? `${fmtMB(r.mem_available_mb)} available of ${fmtMB(r.mem_total_mb)}` : undefined} /><Meter label="Disk" pct={r?.disk?.pct} detail={r ? `${r.disk.free_gb} GB free` : undefined} />
            {r?.reasons?.length ? <p className="text-xs text-warning">{r.reasons.join("; ")}</p> : null}
          </CardContent></Card>
        <Card><CardHeader><CardTitle>Containers</CardTitle></CardHeader><CardContent className="space-y-3">
          {Object.entries(r?.workers ?? {}).map(([name, w]) => (
            <Meter key={name} label={`${name} worker ${w.ok ? "" : "(down)"}`} pct={w.container?.pct} detail={w.container?.used_mb != null ? `${fmtMB(w.container.used_mb)} of ${w.container.limit_mb ? fmtMB(w.container.limit_mb) : "no limit"}` : undefined} />
          ))}
          <div className="text-xs text-muted-foreground">Limits: {Object.entries(r?.limits ?? {}).map(([k, v]) => `${k} ${v}`).join(" · ")}</div>
        </CardContent></Card>
        <Card><CardHeader><CardTitle>Per-stream workers</CardTitle></CardHeader><CardContent>
          <Table><THead><TR><TH>Stream</TH><TH>RAM</TH><TH>CPU</TH><TH>Procs</TH></TR></THead><TBody>
            {Object.entries(r?.streams ?? {}).filter(([, v]) => v).map(([id, v]) => <TR key={id}><TD className="font-mono text-xs">{id.slice(0, 8)}</TD><TD>{fmtMB(v!.rss_mb)}</TD><TD>{v!.cpu_pct}%</TD><TD>{v!.procs}</TD></TR>)}
          </TBody></Table>
          {!Object.values(r?.streams ?? {}).some(Boolean) && <p className="text-sm text-muted-foreground">No stream workers running.</p>}
        </CardContent></Card>
      </div>
      <div className="grid gap-4 lg:grid-cols-2">
        <Card><CardHeader><CardTitle className="flex items-center justify-between">Storage <Button size="sm" variant="ghost" onClick={() => api("/api/system/storage?refresh=true").then(() => storage.refetch())}>Measure now</Button></CardTitle></CardHeader>
          <CardContent className="space-y-2">
            {storage.data && <Meter label={`Used ${fmtMB(storage.data.total_mb)} of ${storage.data.limit_gb} GB limit`} pct={(storage.data.total_mb / 1024 / storage.data.limit_gb) * 100} detail={`${storage.data.disk_free_gb} GB free on disk · source clips kept ${storage.data.retention_days} days`} />}
            <Table><TBody>{Object.entries(storage.data?.dirs ?? {}).map(([k, v]) => <TR key={k}><TD className="text-muted-foreground">{k.replace("_", " ")}</TD><TD className="text-right tabular-nums">{fmtMB(v)}</TD></TR>)}</TBody></Table>
          </CardContent></Card>
        <Card><CardHeader><CardTitle>AI & API usage (24 h)</CardTitle></CardHeader><CardContent className="space-y-2">
          <Table><THead><TR><TH>Service</TH><TH>Purpose</TH><TH>Calls</TH><TH>In / out</TH><TH>Avg ms</TH><TH>Est. $</TH></TR></THead><TBody>
            {(usage.data?.rows ?? []).map((u, i) => <TR key={i}><TD>{u.service}<div className="text-[11px] text-muted-foreground">{u.model}</div></TD><TD>{u.purpose}</TD><TD>{u.calls}{u.failures ? <span className="text-destructive"> ({u.failures} failed)</span> : null}</TD>
              <TD className="text-xs tabular-nums">{u.units_in} / {u.units_out}</TD><TD>{u.avg_latency_ms}</TD><TD>{u.est_cost_usd}</TD></TR>)}
          </TBody></Table>
          {usage.data && <p className="text-xs text-muted-foreground">Speech today {usage.data.today.speech_minutes} / {usage.data.today.speech_budget_minutes} min · YouTube chat quota {usage.data.today.youtube_chat_units} / {usage.data.today.youtube_chat_budget} units · Gemini budget {usage.data.today.gemini_request_budget} requests/day. {usage.data.pricing_note}</p>}
        </CardContent></Card>
      </div>
      <Card><CardHeader><CardTitle>Configuration</CardTitle></CardHeader><CardContent>
        <div className="mb-3 text-sm">YouTube: {ch.data?.configured ? (ch.data.title ? <Badge variant="success">{ch.data.title} · {ch.data.id}</Badge> : <span className="text-destructive">{ch.data.error}</span>) : <Badge variant="secondary">not configured</Badge>}</div>
        <div className="grid gap-x-6 gap-y-1 text-sm sm:grid-cols-2 lg:grid-cols-3">
          {Object.entries(cfg.data ?? {}).map(([k, v]) => <div key={k} className="flex justify-between gap-2 border-b py-1"><span className="text-muted-foreground">{k.replaceAll("_", " ")}</span><span className="tabular-nums">{String(v)}</span></div>)}
        </div>
      </CardContent></Card>
    </div>
  );
}
