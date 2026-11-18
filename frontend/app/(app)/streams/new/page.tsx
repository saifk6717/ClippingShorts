"use client";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronDown, ChevronUp, Plus, Trash2 } from "lucide-react";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { toast } from "sonner";
import { PageHeader } from "@/components/common";
import { DetectionSettingsForm, type DetectionSettings } from "@/components/stream-settings";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Input, NativeSelect } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { ApiError, api, post } from "@/lib/api";
import type { Stream } from "@/lib/types";
import { guessPlatform, normalizeKey, validateStreamUrl } from "@/lib/validate";

interface Row { key: number; url: string; platform: string; creator_name: string; enabled: boolean; settings: DetectionSettings; open: boolean; error?: string }
const MAX_ROWS = 10;
let seq = 0;
const blank = (): Row => ({ key: ++seq, url: "", platform: "twitch", creator_name: "", enabled: true, settings: {}, open: false });

export default function AddStreams() {
  const [rows, setRows] = useState<Row[]>([blank(), blank()]);
  const [startNow, setStartNow] = useState(true);
  const router = useRouter();
  const qc = useQueryClient();
  const existing = useQuery({ queryKey: ["streams"], queryFn: () => api<{ streams: Stream[] }>("/api/streams") });
  const cfg = useQuery({ queryKey: ["config"], queryFn: () => api<{ synthetic: boolean }>("/api/system/config") });
  const existingKeys = new Set((existing.data?.streams ?? []).map((s) => normalizeKey(s.url)));
  const room = Math.max(0, MAX_ROWS - (existing.data?.streams.length ?? 0));

  const update = (key: number, patch: Partial<Row>) => setRows((rs) => rs.map((r) => (r.key === key ? { ...r, ...patch, error: undefined } : r)));

  function check(): boolean {
    const seen = new Map<string, number>();
    let ok = true;
    const filled = rows.filter((r) => r.url.trim());
    const next = rows.map((r) => {
      if (!r.url.trim()) return { ...r, error: undefined };
      let error = validateStreamUrl(r.url, r.platform) ?? undefined;
      const k = normalizeKey(r.url);
      if (!error && seen.has(k)) error = "Same link as another row.";
      if (!error && existingKeys.has(k)) error = "This stream is already added.";
      seen.set(k, r.key);
      if (error) ok = false;
      return { ...r, error };
    });
    setRows(next);
    if (!filled.length) { toast.error("Add at least one stream link."); return false; }
    if (filled.length > room) { toast.error(`Only ${room} more stream(s) fit (10 max).`); return false; }
    return ok;
  }

  const submit = useMutation({
    mutationFn: () => post<{ created: Stream[]; errors: { index: number; error: string }[] }>("/api/streams", {
      start: startNow,
      streams: rows.filter((r) => r.url.trim()).map((r) => ({ url: r.url.trim(), platform: r.platform, creator_name: r.creator_name, enabled: r.enabled, settings: r.settings })),
    }),
    onSuccess: (res) => {
      qc.invalidateQueries({ queryKey: ["streams"] });
      if (res.errors.length) {
        const filled = rows.filter((r) => r.url.trim());
        const errByKey = new Map(res.errors.map((e) => [filled[e.index]?.key, e.error]));
        setRows((rs) => rs.filter((r) => errByKey.has(r.key)).map((r) => ({ ...r, error: errByKey.get(r.key) })));
        toast.warning(`${res.created.length} added, ${res.errors.length} need attention.`);
      } else {
        toast.success(`${res.created.length} stream(s) added${startNow ? " and starting" : ""}.`);
        router.push("/streams");
      }
    },
    onError: (e) => {
      const detail = e instanceof ApiError ? (e.detail as { errors?: { index: number; error: string }[] }) : undefined;
      if (detail?.errors) {
        const filled = rows.filter((r) => r.url.trim());
        const errByKey = new Map(detail.errors.map((x) => [filled[x.index]?.key, x.error]));
        setRows((rs) => rs.map((r) => ({ ...r, error: errByKey.get(r.key) })));
      }
      toast.error(e.message);
    },
  });

  return (
    <div className="mx-auto max-w-4xl space-y-4">
      <PageHeader title="Add live streams" description="Add 2 to 10 Twitch or YouTube Live links. Each runs in its own isolated worker." />
      {rows.map((r, i) => (
        <Card key={r.key} className={r.error ? "border-destructive/60" : ""}>
          <CardContent className="space-y-3 p-4">
            <div className="grid gap-3 md:grid-cols-[1fr_160px_200px_auto]">
              <div className="space-y-1">
                <Label>Stream {i + 1} link</Label>
                <Input placeholder="https://www.twitch.tv/channel  or  https://www.youtube.com/@channel/live" value={r.url}
                  onChange={(e) => { const g = guessPlatform(e.target.value); update(r.key, { url: e.target.value, ...(g ? { platform: g } : {}) }); }} />
              </div>
              <div className="space-y-1">
                <Label>Platform</Label>
                <NativeSelect value={r.platform} onChange={(e) => update(r.key, { platform: e.target.value })}>
                  <option value="twitch">Twitch</option>
                  <option value="youtube">YouTube Live</option>
                  {cfg.data?.synthetic && <option value="synthetic">Synthetic (test)</option>}
                </NativeSelect>
              </div>
              <div className="space-y-1">
                <Label>Creator name (optional)</Label>
                <Input value={r.creator_name} onChange={(e) => update(r.key, { creator_name: e.target.value })} />
              </div>
              <div className="flex items-end gap-3 pb-1.5">
                <div className="flex items-center gap-2"><Switch checked={r.enabled} onCheckedChange={(v) => update(r.key, { enabled: v })} /><span className="text-xs text-muted-foreground">Enabled</span></div>
                <Button variant="ghost" size="icon" disabled={rows.length <= 1} onClick={() => setRows((rs) => rs.filter((x) => x.key !== r.key))} aria-label="Remove"><Trash2 /></Button>
              </div>
            </div>
            {r.error && <p className="text-sm text-destructive">{r.error}</p>}
            <button className="flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground" onClick={() => update(r.key, { open: !r.open })}>
              {r.open ? <ChevronUp className="h-3.5 w-3.5" /> : <ChevronDown className="h-3.5 w-3.5" />} Custom detection settings {Object.keys(r.settings).length ? `(${Object.keys(r.settings).length} set)` : ""}
            </button>
            {r.open && <DetectionSettingsForm value={r.settings} onChange={(s) => update(r.key, { settings: s })} />}
          </CardContent>
        </Card>
      ))}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <Button variant="outline" disabled={rows.length >= Math.min(MAX_ROWS, room)} onClick={() => setRows((rs) => [...rs, blank()])}>
          <Plus /> Add another ({rows.length}/{Math.min(MAX_ROWS, room)})
        </Button>
        <div className="flex items-center gap-4">
          <label className="flex items-center gap-2 text-sm"><Checkbox checked={startNow} onCheckedChange={(v) => setStartNow(v === true)} /> Start monitoring now</label>
          <Button disabled={submit.isPending} onClick={() => check() && submit.mutate()}>{submit.isPending ? "Adding…" : "Add streams"}</Button>
        </div>
      </div>
      <p className="text-xs text-muted-foreground">{existing.data?.streams.length ?? 0} of 10 configured. Links are checked for the right platform and duplicates here and again on the server; whether a channel is live is checked when its worker starts.</p>
    </div>
  );
}
