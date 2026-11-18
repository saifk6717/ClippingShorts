"use client";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, Check, Clapperboard, Download, Pause, Play, Repeat, Save, Trash2, Upload as UploadIcon, X } from "lucide-react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useEffect, useMemo, useRef, useState } from "react";
import { toast } from "sonner";
import { PlatformBadge, StatusBadge } from "@/components/common";
import { CaptionsEditor } from "@/components/editor/captions-editor";
import { Preview } from "@/components/editor/preview";
import { Timeline } from "@/components/editor/timeline";
import { UploadDialog } from "@/components/editor/upload-dialog";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input, NativeSelect, Textarea } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Progress } from "@/components/ui/progress";
import { Slider } from "@/components/ui/slider";
import { Switch } from "@/components/ui/switch";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { api, del, patch, post } from "@/lib/api";
import type { Clip, Edit, EditParams, Word } from "@/lib/types";
import { fmtAgo, fmtTime } from "@/lib/utils";

export default function EditorPage() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const qc = useQueryClient();
  const clipQ = useQuery({ queryKey: ["clip", id], queryFn: () => api<Clip>(`/api/clips/${id}`), refetchInterval: (q) => (q.state.data?.edits?.some((e) => ["queued", "rendering", "deferred"].includes(e.status)) ? 2000 : 15000) });
  const wordsQ = useQuery({ queryKey: ["words", id], queryFn: () => api<{ words: Word[]; status: string }>(`/api/clips/${id}/words`) });
  const waveQ = useQuery({ queryKey: ["wave", id], queryFn: () => api<{ per_second: number; peaks: number[] }>(`/api/clips/${id}/waveform`), staleTime: Infinity });
  const clip = clipQ.data;
  const [p, setP] = useState<EditParams | null>(null);
  const [time, setTime] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [loop, setLoop] = useState(true);
  const [uploadFor, setUploadFor] = useState<Edit | null>(null);
  const [watch, setWatch] = useState<Edit | null>(null);
  const video = useRef<HTMLVideoElement>(null);

  useEffect(() => {
    if (clip && !p) {
      const latest = clip.edits?.[0]?.params;
      setP(structuredClone(latest ?? clip.default_params!) as EditParams);
    }
  }, [clip, p]);

  const words: Word[] = useMemo(() => p?.captions.words ?? wordsQ.data?.words ?? [], [p?.captions.words, wordsQ.data]);

  // preview volume follows the edit (the browser can't amplify above 100%; the render can)
  useEffect(() => {
    if (!video.current || !p) return;
    video.current.muted = p.audio.mute;
    video.current.volume = Math.min(1, Math.pow(10, (p.audio.volume_db || 0) / 20));
  }, [p?.audio]);

  // loop playback inside the selection
  useEffect(() => {
    const v = video.current;
    if (!v || !p || !playing) return;
    if (time >= p.end) {
      if (loop) v.currentTime = p.start;
      else { v.pause(); setPlaying(false); }
    }
  }, [time, p, loop, playing]);

  const set = <K extends keyof EditParams>(k: K, v: EditParams[K]) => setP((x) => (x ? { ...x, [k]: v } : x));
  const setSub = <K extends "captions" | "hook" | "audio">(k: K, patchObj: Partial<EditParams[K]>) => setP((x) => (x ? { ...x, [k]: { ...x[k], ...patchObj } } : x));

  const render = useMutation({
    mutationFn: () => post<Edit>(`/api/clips/${id}/render`, { params: p }),
    onSuccess: (e) => { toast.success(`Version ${e.version} queued for rendering.`); qc.invalidateQueries({ queryKey: ["clip", id] }); },
    onError: (e: Error) => toast.error(e.message),
  });
  const saveDraft = useMutation({
    mutationFn: () => post<Edit>(`/api/clips/${id}/edit`, p),
    onSuccess: (e) => { toast.success(`Saved as draft version ${e.version}.`); qc.invalidateQueries({ queryKey: ["clip", id] }); },
    onError: (e: Error) => toast.error(e.message),
  });
  const review = useMutation({
    mutationFn: (status: string) => patch(`/api/clips/${id}`, { review_status: status }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["clip", id] }),
  });

  if (clipQ.isLoading || !clip || !p) return <div className="text-sm text-muted-foreground">Loading…</div>;
  if (clipQ.isError) return <div className="text-sm text-destructive">This clip could not be loaded.</div>;
  const dur = clip.duration;
  const seek = (t: number) => { if (video.current) video.current.currentTime = t; setTime(t); };
  const toggle = () => {
    const v = video.current;
    if (!v) return;
    if (v.paused) { if (v.currentTime < p.start || v.currentTime >= p.end) v.currentTime = p.start; v.play(); setPlaying(true); }
    else { v.pause(); setPlaying(false); }
  };

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex min-w-0 items-center gap-3">
          <Button variant="ghost" size="icon" onClick={() => router.back()}><ArrowLeft /></Button>
          <div className="min-w-0">
            <h1 className="truncate text-lg font-semibold">{clip.title || "Untitled highlight"}</h1>
            <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
              <PlatformBadge platform={clip.platform} /> {clip.creator_name} · detected {fmtAgo(clip.detected_at)} · {clip.category}
              {clip.confidence > 0 && <Badge variant="secondary">AI {clip.confidence}%</Badge>}
              <StatusBadge status={clip.review_status} />
              <Badge variant="outline">captions: {wordsQ.data?.status ?? clip.words_status}</Badge>
            </div>
          </div>
        </div>
        <div className="flex gap-2">
          <Button size="sm" variant="outline" onClick={() => review.mutate("approved")}><Check /> Approve</Button>
          <Button size="sm" variant="outline" onClick={() => review.mutate("rejected")}><X /> Reject</Button>
          <Button size="sm" variant="ghost" onClick={async () => { if (confirm("Delete this clip and every version?")) { await del(`/api/clips/${id}`); router.push("/clips"); } }}><Trash2 /></Button>
        </div>
      </div>

      <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_380px]">
        <div className="space-y-3">
          <Preview ref={video} src={clip.preview_url!} width={clip.width} height={clip.height} params={p} words={words} time={time}
            onTime={setTime} onCrop={(x, y) => setP((v) => (v ? { ...v, crop_x: x, crop_y: y, smart_crop: false } : v))} />
          <div className="flex flex-wrap items-center gap-2">
            <Button size="sm" onClick={toggle}>{playing ? <Pause /> : <Play />}{playing ? "Pause" : "Play selection"}</Button>
            <Button size="sm" variant={loop ? "secondary" : "ghost"} onClick={() => setLoop(!loop)} title="Loop the selection"><Repeat /></Button>
            <span className="font-mono text-xs tabular-nums text-muted-foreground">{fmtTime(time)} / {fmtTime(dur)}</span>
            <div className="ml-auto flex flex-wrap items-center gap-2 text-xs">
              <Button size="sm" variant="outline" onClick={() => set("start", Math.min(time, p.end - 1))}>Set start</Button>
              <Input className="h-8 w-20" type="number" step="0.1" min={0} value={p.start.toFixed(1)} onChange={(e) => set("start", Math.max(0, Math.min(Number(e.target.value), p.end - 1)))} />
              <span className="text-muted-foreground">→</span>
              <Input className="h-8 w-20" type="number" step="0.1" max={dur} value={p.end.toFixed(1)} onChange={(e) => set("end", Math.min(dur, Math.max(Number(e.target.value), p.start + 1)))} />
              <Button size="sm" variant="outline" onClick={() => set("end", Math.max(time, p.start + 1))}>Set end</Button>
              <Badge variant={p.end - p.start > 60 ? "warning" : "secondary"}>{(p.end - p.start).toFixed(1)}s{p.end - p.start > 60 ? " (Shorts ≤ 60s recommended)" : ""}</Badge>
            </div>
          </div>
          <Timeline duration={dur} start={p.start} end={p.end} current={time} sprite={clip.sprite} spriteUrl={clip.sprite_url}
            peaks={waveQ.data?.peaks ?? []} perSecond={waveQ.data?.per_second ?? 10}
            onChange={(s, e) => setP((v) => (v ? { ...v, start: +s.toFixed(2), end: +e.toFixed(2) } : v))} onSeek={seek} />
          {clip.suggested_end > 0 && (
            <button className="text-xs text-muted-foreground hover:text-foreground" onClick={() => setP((v) => (v ? { ...v, start: clip.suggested_start, end: clip.suggested_end } : v))}>
              Reset to the AI's suggested range ({fmtTime(clip.suggested_start)}–{fmtTime(clip.suggested_end)})
            </button>
          )}
          {clip.reason && <p className="text-xs text-muted-foreground">Why it was picked: {clip.reason}</p>}
        </div>

        <div className="space-y-4">
          <Card>
            <CardContent className="p-3">
              <Tabs defaultValue="format">
                <TabsList className="w-full justify-between">
                  <TabsTrigger value="format">Format</TabsTrigger>
                  <TabsTrigger value="captions">Captions</TabsTrigger>
                  <TabsTrigger value="text">Title</TabsTrigger>
                  <TabsTrigger value="audio">Audio</TabsTrigger>
                </TabsList>
                <TabsContent value="format" className="space-y-3">
                  <div className="space-y-1"><Label>Aspect ratio</Label>
                    <div className="grid grid-cols-3 gap-2">
                      {(["9:16", "16:9", "1:1"] as const).map((a) => (
                        <Button key={a} size="sm" variant={p.aspect === a ? "default" : "outline"} onClick={() => set("aspect", a)}>{a === "9:16" ? "9:16 Short" : a === "16:9" ? "16:9 Wide" : "1:1 Square"}</Button>
                      ))}
                    </div>
                  </div>
                  <div className="space-y-1"><Label>Fit</Label>
                    <NativeSelect value={p.fit} onChange={(e) => set("fit", e.target.value as EditParams["fit"])}>
                      <option value="crop">Crop to fill (drag the box on the video)</option>
                      <option value="blur">Whole frame on a blurred background</option>
                    </NativeSelect>
                  </div>
                  {p.fit === "crop" && (
                    <>
                      <div className="space-y-1"><Label>Zoom {p.zoom.toFixed(2)}×</Label><Slider min={1} max={3} step={0.05} value={p.zoom} onChange={(v) => set("zoom", v)} /></div>
                      <div className="flex items-center justify-between"><Label>Smart crop (follow the action)</Label>
                        <Switch checked={p.smart_crop && p.crop_x == null} onCheckedChange={(v) => setP((x) => (x ? { ...x, smart_crop: v, crop_x: v ? null : 0.5 } : x))} /></div>
                    </>
                  )}
                  <div className="space-y-1"><Label>Quality</Label>
                    <NativeSelect value={p.quality} onChange={(e) => set("quality", e.target.value as EditParams["quality"])}>
                      <option value="high">High (1080p)</option><option value="fast">Fast draft (720p)</option>
                    </NativeSelect>
                  </div>
                </TabsContent>
                <TabsContent value="captions" className="space-y-3">
                  <div className="flex items-center justify-between"><Label>Burn in captions</Label><Switch checked={p.captions.enabled} onCheckedChange={(v) => setSub("captions", { enabled: v })} /></div>
                  <div className="grid grid-cols-2 gap-2">
                    <div className="space-y-1"><Label>Style</Label>
                      <NativeSelect value={p.captions.style} onChange={(e) => setSub("captions", { style: e.target.value as EditParams["captions"]["style"] })}>
                        <option value="bounce">Bounce</option><option value="hype">Hype</option><option value="clean">Clean</option>
                      </NativeSelect>
                    </div>
                    <div className="space-y-1"><Label>Size {Math.round(p.captions.font_scale * 100)}%</Label><Slider min={0.6} max={1.5} step={0.05} value={p.captions.font_scale} onChange={(v) => setSub("captions", { font_scale: v })} /></div>
                  </div>
                  <div className="space-y-1"><Label>Vertical position</Label><Slider min={0.1} max={0.92} step={0.01} value={p.captions.position} onChange={(v) => setSub("captions", { position: v })} /></div>
                  <CaptionsEditor words={words} start={p.start} end={p.end} per={3} current={time} onSeek={seek}
                    onChange={(w) => setSub("captions", { words: w })} onReset={() => setSub("captions", { words: null })} />
                </TabsContent>
                <TabsContent value="text" className="space-y-3">
                  <div className="flex items-center justify-between"><Label>Hook text on video</Label><Switch checked={p.hook.enabled} onCheckedChange={(v) => setSub("hook", { enabled: v })} /></div>
                  <Input maxLength={60} value={p.hook.text} onChange={(e) => setSub("hook", { text: e.target.value })} placeholder="SHORT, PUNCHY HOOK" />
                  <div className="space-y-1"><Label>Hook position</Label><Slider min={0.05} max={0.9} step={0.01} value={p.hook.position} onChange={(v) => setSub("hook", { position: v })} /></div>
                  <div className="space-y-1"><Label>Title</Label><Input maxLength={100} value={p.title} onChange={(e) => set("title", e.target.value)} /></div>
                  <div className="space-y-1"><Label>Description</Label><Textarea rows={3} value={p.description} onChange={(e) => set("description", e.target.value)} /></div>
                  <div className="space-y-1"><Label>Hashtags</Label><Input value={p.hashtags.join(" ")} onChange={(e) => set("hashtags", e.target.value.split(/[\s,]+/).map((t) => t.replace(/^#/, "")).filter(Boolean))} /></div>
                </TabsContent>
                <TabsContent value="audio" className="space-y-3">
                  <div className="space-y-1"><Label>Volume {p.audio.volume_db > 0 ? "+" : ""}{p.audio.volume_db.toFixed(1)} dB</Label><Slider min={-20} max={12} step={0.5} value={p.audio.volume_db} onChange={(v) => setSub("audio", { volume_db: v })} /></div>
                  <div className="flex items-center justify-between"><Label>Normalize loudness (−14 LUFS)</Label><Switch checked={p.audio.normalize} onCheckedChange={(v) => setSub("audio", { normalize: v })} /></div>
                  <div className="flex items-center justify-between"><Label>Mute</Label><Switch checked={p.audio.mute} onCheckedChange={(v) => setSub("audio", { mute: v })} /></div>
                  <p className="text-xs text-muted-foreground">The preview can’t play louder than 100%; boosts and normalisation are applied in the render.</p>
                </TabsContent>
              </Tabs>
            </CardContent>
          </Card>
          <div className="flex gap-2">
            <Button className="flex-1" onClick={() => render.mutate()} disabled={render.isPending}><Clapperboard /> Export / render</Button>
            <Button variant="outline" onClick={() => saveDraft.mutate()} disabled={saveDraft.isPending} title="Save without rendering"><Save /></Button>
          </div>

          <Card>
            <CardHeader><CardTitle>Versions</CardTitle></CardHeader>
            <CardContent className="space-y-2">
              {(clip.edits ?? []).length === 0 && <p className="text-sm text-muted-foreground">Nothing rendered yet. The source clip is never changed; every export is a new version.</p>}
              {(clip.edits ?? []).map((e) => {
                const ups = (clip.uploads ?? []).filter((u) => u.edit_id === e.id);
                return (
                  <div key={e.id} className="space-y-1.5 rounded-md border p-2">
                    <div className="flex items-center justify-between gap-2">
                      <div className="flex items-center gap-2 text-sm"><span className="font-medium">v{e.version}</span><StatusBadge status={e.status} /><span className="text-xs text-muted-foreground">{e.params.aspect} · {(e.params.end - e.params.start).toFixed(0)}s</span></div>
                      <div className="flex gap-1">
                        <Button size="sm" variant="ghost" onClick={() => setP(structuredClone(e.params))} title="Load these settings">Load</Button>
                        {e.status === "done" && <Button size="sm" variant="ghost" onClick={() => setWatch(e)} title="Watch"><Play /></Button>}
                        {e.status === "done" && <Button size="sm" variant="ghost" asChild title="Download"><a href={`${e.file_url}?download=1`}><Download /></a></Button>}
                        {e.status === "done" && <Button size="sm" variant="ghost" onClick={() => setUploadFor(e)} title="Upload to YouTube"><UploadIcon /></Button>}
                      </div>
                    </div>
                    {["queued", "rendering", "deferred"].includes(e.status) && <Progress value={e.progress} />}
                    {e.status === "deferred" && <p className="text-xs text-warning">{e.error}</p>}
                    {e.status === "failed" && <p className="text-xs text-destructive">{e.error}</p>}
                    {e.status === "done" && <p className="text-xs text-muted-foreground">rendered in {e.render_seconds}s</p>}
                    {ups.map((u) => (
                      <div key={u.id} className="flex items-center gap-2 text-xs">
                        <span className="text-muted-foreground">YouTube</span><StatusBadge status={u.status} />
                        {u.status === "uploading" && <Progress value={u.progress} className="h-1.5 w-24" />}
                        {u.youtube_video_id && <a className="text-primary" target="_blank" rel="noreferrer noopener" href={`https://youtu.be/${u.youtube_video_id}`}>{u.youtube_video_id}</a>}
                        {u.status === "failed" && <><span className="truncate text-destructive" title={u.error}>{u.error}</span><Button size="sm" variant="ghost" onClick={async () => { await post(`/api/uploads/${u.id}/retry`); qc.invalidateQueries({ queryKey: ["clip", id] }); }}>Retry</Button></>}
                      </div>
                    ))}
                  </div>
                );
              })}
              <Link href="/clips" className="block pt-1 text-xs text-muted-foreground hover:text-foreground">← Back to the library</Link>
            </CardContent>
          </Card>
        </div>
      </div>
      {uploadFor && <UploadDialog clip={clip} edit={uploadFor} onClose={() => setUploadFor(null)} />}
      {watch && (
        <Dialog open onOpenChange={(o) => !o && setWatch(null)}>
          <DialogContent className="max-w-md">
            <DialogHeader><DialogTitle>Version {watch.version}</DialogTitle></DialogHeader>
            <video src={watch.file_url!} controls autoPlay playsInline preload="metadata" className="max-h-[75vh] w-full rounded-md bg-black" />
          </DialogContent>
        </Dialog>
      )}
    </div>
  );
}
