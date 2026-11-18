"use client";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { MonitorPlay } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input, NativeSelect, Textarea } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { api, post } from "@/lib/api";
import type { Clip, Edit } from "@/lib/types";

export function UploadDialog({ clip, edit, onClose }: { clip: Clip; edit: Edit; onClose: () => void }) {
  const qc = useQueryClient();
  const channel = useQuery({ queryKey: ["yt-channel"], queryFn: () => api<{ configured: boolean; id?: string; title?: string; error?: string }>("/api/youtube/channel") });
  const [title, setTitle] = useState(edit.params.title || clip.title || "");
  const [description, setDescription] = useState(edit.params.description || clip.description || "");
  const [tags, setTags] = useState((edit.params.hashtags?.length ? edit.params.hashtags : clip.hashtags || []).concat(["shorts"]).filter((v, i, a) => a.indexOf(v) === i).join(" "));
  const [privacy, setPrivacy] = useState("private");
  const [when, setWhen] = useState("");
  const up = useMutation({
    mutationFn: () => post(`/api/youtube/upload/${clip.id}`, {
      edit_id: edit.id, title, description, privacy,
      hashtags: tags.split(/[\s,]+/).map((t) => t.replace(/^#/, "")).filter(Boolean),
      publish_at: when ? new Date(when).toISOString() : null,
    }),
    onSuccess: () => { toast.success("Upload queued."); qc.invalidateQueries({ queryKey: ["clip", clip.id] }); onClose(); },
    onError: (e: Error) => toast.error(e.message),
  });
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2"><MonitorPlay className="h-4 w-4 text-[#ff0033]" /> Publish version {edit.version} to YouTube</DialogTitle>
          <DialogDescription>
            {channel.data?.configured ? (channel.data.title ? `Channel: ${channel.data.title}` : channel.data.error) : "YouTube is not configured on the server."}
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-3">
          <div className="space-y-1"><Label>Title ({title.length}/100)</Label><Input maxLength={100} value={title} onChange={(e) => setTitle(e.target.value)} /></div>
          <div className="space-y-1"><Label>Description</Label><Textarea rows={4} value={description} onChange={(e) => setDescription(e.target.value)} /></div>
          <div className="space-y-1"><Label>Hashtags</Label><Input value={tags} onChange={(e) => setTags(e.target.value)} placeholder="gaming clips shorts" /></div>
          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-1"><Label>Visibility</Label>
              <NativeSelect value={privacy} onChange={(e) => setPrivacy(e.target.value)} disabled={!!when}>
                <option value="private">Private</option><option value="unlisted">Unlisted</option><option value="public">Public</option>
              </NativeSelect>
            </div>
            <div className="space-y-1"><Label>Schedule (optional)</Label><Input type="datetime-local" value={when} onChange={(e) => setWhen(e.target.value)} /></div>
          </div>
          {when && <p className="text-xs text-muted-foreground">Scheduled videos upload as private and YouTube publishes them at that time.</p>}
          <p className="text-xs text-muted-foreground">Only publish clips you have the rights to. Each version can be published once per channel.</p>
        </div>
        <DialogFooter>
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
          <Button onClick={() => up.mutate()} disabled={up.isPending || !title.trim() || !channel.data?.configured}>{when ? "Schedule" : "Upload"}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
