"use client";
import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, Clapperboard, Lock, Trash2, X } from "lucide-react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";
import { toast } from "sonner";
import { PageHeader, PlatformBadge, StatusBadge } from "@/components/common";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Input, NativeSelect } from "@/components/ui/input";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { api, post } from "@/lib/api";
import type { Clip, Paged } from "@/lib/types";
import { fmtAgo, fmtDuration } from "@/lib/utils";

const CATEGORIES = ["funny", "fail", "clutch", "rage", "shock", "hype", "wholesome", "take", "story", "other"];

export default function ClipsPage() {
  return <Suspense><Library /></Suspense>;
}

function Library() {
  const sp = useSearchParams();
  const router = useRouter();
  const qc = useQueryClient();
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const get = (k: string, d = "") => sp.get(k) ?? d;
  const setParam = (k: string, v: string) => {
    const p = new URLSearchParams(sp.toString());
    if (v) p.set(k, v); else p.delete(k);
    if (k !== "page") p.delete("page");
    router.replace(`/clips?${p.toString()}`);
  };
  const params = new URLSearchParams();
  for (const k of ["platform", "stream_id", "creator", "category", "state", "q", "page"]) if (get(k)) params.set(k, get(k));
  if (get("from")) params.set("date_from", String(new Date(get("from")).getTime() / 1000));
  if (get("to")) params.set("date_to", String(new Date(get("to")).getTime() / 1000 + 86400));
  params.set("page_size", "24");
  const { data, isFetching } = useQuery({
    queryKey: ["clips", params.toString()], queryFn: () => api<Paged<Clip>>(`/api/clips?${params}`), placeholderData: keepPreviousData, refetchInterval: 10000,
  });
  const batch = useMutation({
    mutationFn: (action: string) => post<{ done: string[]; errors: { id: string; error: string }[] }>("/api/clips/batch", { ids: [...selected], action }),
    onSuccess: (r, action) => {
      toast.success(`${action}: ${r.done.length} clip(s)${r.errors.length ? `, ${r.errors.length} failed` : ""}`);
      setSelected(new Set());
      qc.invalidateQueries({ queryKey: ["clips"] });
    },
    onError: (e: Error) => toast.error(e.message),
  });
  const items = data?.items ?? [];
  const toggle = (id: string) => setSelected((s) => { const n = new Set(s); if (n.has(id)) n.delete(id); else n.add(id); return n; });

  return (
    <div className="space-y-4">
      <PageHeader title="Highlights library" description="Saved source clips. Open one to trim, crop and caption it, then render and publish." />
      <div className="flex flex-wrap items-center gap-2">
        <Tabs value={get("state", "all")} onValueChange={(v) => setParam("state", v === "all" ? "" : v)}>
          <TabsList>
            <TabsTrigger value="all">All</TabsTrigger>
            <TabsTrigger value="pending">Pending review</TabsTrigger>
            <TabsTrigger value="rendered">Rendered</TabsTrigger>
            <TabsTrigger value="uploaded">Uploaded</TabsTrigger>
          </TabsList>
        </Tabs>
        <NativeSelect className="w-36" value={get("platform")} onChange={(e) => setParam("platform", e.target.value)}>
          <option value="">All platforms</option><option value="twitch">Twitch</option><option value="youtube">YouTube Live</option><option value="synthetic">Synthetic</option>
        </NativeSelect>
        <NativeSelect className="w-36" value={get("category")} onChange={(e) => setParam("category", e.target.value)}>
          <option value="">All types</option>{CATEGORIES.map((c) => <option key={c} value={c}>{c}</option>)}
        </NativeSelect>
        <Input className="w-40" placeholder="Streamer" defaultValue={get("creator")} onKeyDown={(e) => e.key === "Enter" && setParam("creator", (e.target as HTMLInputElement).value)} />
        <Input className="w-40" type="date" value={get("from")} onChange={(e) => setParam("from", e.target.value)} title="From date" />
        <Input className="w-40" type="date" value={get("to")} onChange={(e) => setParam("to", e.target.value)} title="To date" />
        {get("stream_id") && <Badge variant="secondary" className="cursor-pointer" onClick={() => setParam("stream_id", "")}>one stream <X className="h-3 w-3" /></Badge>}
        {isFetching && <span className="text-xs text-muted-foreground">updating…</span>}
      </div>

      {selected.size > 0 && (
        <div className="sticky top-14 z-20 flex flex-wrap items-center gap-2 rounded-lg border bg-card p-2 md:top-2">
          <span className="px-2 text-sm">{selected.size} selected</span>
          <Button size="sm" variant="outline" onClick={() => batch.mutate("approve")}><Check /> Approve</Button>
          <Button size="sm" variant="outline" onClick={() => batch.mutate("reject")}><X /> Reject</Button>
          <Button size="sm" variant="outline" onClick={() => batch.mutate("render")}><Clapperboard /> Render default Short</Button>
          <Button size="sm" variant="outline" onClick={() => batch.mutate("keep")}><Lock /> Keep forever</Button>
          <Button size="sm" variant="destructive" onClick={() => confirm(`Delete ${selected.size} clip(s) and all their versions?`) && batch.mutate("delete")}><Trash2 /> Delete</Button>
          <Button size="sm" variant="ghost" onClick={() => setSelected(new Set())}>Clear</Button>
        </div>
      )}

      {items.length === 0 && !isFetching && <Card className="p-6 text-sm text-muted-foreground">No clips match. Highlights appear here as soon as a stream produces one.</Card>}
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3 2xl:grid-cols-4">
        {items.map((c) => (
          <Card key={c.id} className={`overflow-hidden ${selected.has(c.id) ? "ring-2 ring-primary" : ""}`}>
            <div className="relative aspect-video bg-muted">
              {c.thumbnail_url ? (
                // eslint-disable-next-line @next/next/no-img-element
                <img src={c.thumbnail_url} alt="" loading="lazy" className="h-full w-full object-cover" />
              ) : <div className="flex h-full items-center justify-center text-xs text-muted-foreground">{c.status}</div>}
              <div className="absolute left-2 top-2"><Checkbox checked={selected.has(c.id)} onCheckedChange={() => toggle(c.id)} className="bg-background/80" /></div>
              <div className="absolute bottom-2 right-2 rounded bg-black/70 px-1.5 text-xs">{fmtDuration(c.duration)}</div>
              {c.confidence > 0 && <div className="absolute right-2 top-2 rounded bg-black/70 px-1.5 text-xs">AI {c.confidence}%</div>}
            </div>
            <Link href={`/clips/${c.id}`} className="block space-y-1.5 p-3 hover:bg-accent/30">
              <div className="line-clamp-2 text-sm font-medium">{c.title || "Untitled highlight"}</div>
              <div className="flex flex-wrap items-center gap-1.5 text-xs text-muted-foreground">
                <PlatformBadge platform={c.platform} /><span>{c.creator_name}</span><span>· {fmtAgo(c.detected_at)}</span>
              </div>
              <div className="flex flex-wrap gap-1.5">
                <Badge variant="secondary">{c.category}</Badge>
                <StatusBadge status={c.review_status} />
                {c.render_status !== "none" && <span className="text-xs text-muted-foreground">render:</span>}{c.render_status !== "none" && <StatusBadge status={c.render_status} />}
                {c.upload_status !== "none" && <span className="text-xs text-muted-foreground">upload:</span>}{c.upload_status !== "none" && <StatusBadge status={c.upload_status} />}
              </div>
            </Link>
          </Card>
        ))}
      </div>
      {data && data.pages > 1 && (
        <div className="flex items-center justify-center gap-2">
          <Button size="sm" variant="outline" disabled={data.page <= 1} onClick={() => setParam("page", String(data.page - 1))}>Previous</Button>
          <span className="text-sm text-muted-foreground">Page {data.page} of {data.pages} · {data.total} clips</span>
          <Button size="sm" variant="outline" disabled={data.page >= data.pages} onClick={() => setParam("page", String(data.page + 1))}>Next</Button>
        </div>
      )}
    </div>
  );
}
