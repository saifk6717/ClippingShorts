"use client";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { toast } from "sonner";
import { PageHeader, StatusBadge } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Progress } from "@/components/ui/progress";
import { TBody, TD, TH, THead, TR, Table } from "@/components/ui/table";
import { api, post } from "@/lib/api";
import type { Edit, Highlight, Upload } from "@/lib/types";
import { fmtAgo } from "@/lib/utils";

interface Jobs {
  renders: Edit[]; uploads: Upload[]; highlights: Highlight[];
  queues: Record<string, { length: number; cap: number; dead_letters: number }>;
  dead_letters: Record<string, { id: string; data: Record<string, unknown>; attempts: number; error: string; t: number }[]>;
}

export default function JobsPage() {
  const qc = useQueryClient();
  const { data } = useQuery({ queryKey: ["jobs"], queryFn: () => api<Jobs>("/api/jobs"), refetchInterval: 3000 });
  const retry = async (path: string) => {
    try { await post(path); toast.success("Queued again."); qc.invalidateQueries({ queryKey: ["jobs"] }); } catch (e) { toast.error((e as Error).message); }
  };
  const dead = Object.entries(data?.dead_letters ?? {}).flatMap(([q, items]) => items.map((i) => ({ q, ...i })));
  return (
    <div className="space-y-4">
      <PageHeader title="Jobs" description="Bounded queues: when one is full, new work is refused (backpressure) instead of piling up in memory." />
      <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
        {Object.entries(data?.queues ?? {}).map(([name, q]) => (
          <Card key={name}><CardContent className="space-y-1.5 p-3">
            <div className="text-xs text-muted-foreground">{name.replace("_", " ")}</div>
            <div className="text-lg font-semibold tabular-nums">{q.length}<span className="text-xs text-muted-foreground"> / {q.cap}</span></div>
            <Progress value={(q.length / q.cap) * 100} className="h-1.5" />
            {q.dead_letters > 0 && <div className="text-xs text-destructive">{q.dead_letters} dead-lettered</div>}
          </CardContent></Card>
        ))}
      </div>
      {dead.length > 0 && (
        <Card><CardHeader><CardTitle>Dead letters (failed after all retries)</CardTitle></CardHeader><CardContent>
          <Table><THead><TR><TH>Queue</TH><TH>When</TH><TH>Attempts</TH><TH>Error</TH><TH /></TR></THead><TBody>
            {dead.map((d) => <TR key={d.q + d.id}><TD>{d.q}</TD><TD>{fmtAgo(d.t)}</TD><TD>{d.attempts}</TD><TD className="max-w-md truncate text-xs text-destructive" title={d.error}>{d.error}</TD>
              <TD><Button size="sm" variant="outline" onClick={() => retry(`/api/jobs/dead/${d.q}/${d.id}/retry`)}>Retry</Button></TD></TR>)}
          </TBody></Table>
        </CardContent></Card>
      )}
      <div className="grid gap-4 xl:grid-cols-2">
        <Card><CardHeader><CardTitle>Renders</CardTitle></CardHeader><CardContent>
          <Table><THead><TR><TH>Clip</TH><TH>Version</TH><TH>Status</TH><TH>Progress</TH><TH>Time</TH></TR></THead><TBody>
            {(data?.renders ?? []).map((e) => <TR key={e.id}>
              <TD><Link className="text-primary" href={`/clips/${e.clip_id}`}>{e.clip_id.slice(0, 8)}</Link></TD><TD>v{e.version} · {e.params?.aspect}</TD>
              <TD><StatusBadge status={e.status} /></TD><TD className="w-32">{["rendering", "queued"].includes(e.status) ? <Progress value={e.progress} /> : e.status === "failed" ? <span className="text-xs text-destructive" title={e.error}>error</span> : null}</TD>
              <TD className="text-xs text-muted-foreground">{e.render_seconds ? `${e.render_seconds}s` : fmtAgo(e.created_at)}</TD></TR>)}
          </TBody></Table>
        </CardContent></Card>
        <Card><CardHeader><CardTitle>Uploads</CardTitle></CardHeader><CardContent>
          <Table><THead><TR><TH>Title</TH><TH>Status</TH><TH>Video</TH><TH /></TR></THead><TBody>
            {(data?.uploads ?? []).map((u) => <TR key={u.id}>
              <TD className="max-w-[220px] truncate">{u.title}</TD><TD><StatusBadge status={u.status} />{u.status === "uploading" && <Progress value={u.progress} className="mt-1 h-1.5" />}</TD>
              <TD>{u.youtube_video_id ? <a className="text-primary" href={`https://youtu.be/${u.youtube_video_id}`} target="_blank" rel="noreferrer noopener">{u.youtube_video_id}</a> : <span className="text-xs text-destructive">{u.error}</span>}</TD>
              <TD>{u.status === "failed" && <Button size="sm" variant="outline" onClick={() => retry(`/api/uploads/${u.id}/retry`)}>Retry</Button>}</TD></TR>)}
          </TBody></Table>
        </CardContent></Card>
      </div>
      <Card><CardHeader><CardTitle>AI decisions (latest candidates)</CardTitle></CardHeader><CardContent>
        <Table><THead><TR><TH>When</TH><TH>Trigger</TH><TH>Local</TH><TH>Words</TH><TH>Reaction</TH><TH>Result</TH><TH>Reason</TH></TR></THead><TBody>
          {(data?.highlights ?? []).map((h) => <TR key={h.id}>
            <TD className="whitespace-nowrap text-xs">{fmtAgo(h.detected_at)}</TD><TD className="max-w-[180px] truncate text-xs">{h.trigger}</TD>
            <TD className="tabular-nums">{h.local_score?.toFixed(0)}</TD><TD className="tabular-nums">{h.transcript_score}</TD><TD className="tabular-nums">{h.reaction_score}</TD>
            <TD><StatusBadge status={h.status} /></TD><TD className="max-w-md truncate text-xs text-muted-foreground" title={h.reason}>{h.reason}</TD></TR>)}
        </TBody></Table>
      </CardContent></Card>
    </div>
  );
}
