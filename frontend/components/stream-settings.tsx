"use client";
import { Input, NativeSelect } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

export type DetectionSettings = Record<string, string | number>;

const FIELDS: { key: string; label: string; hint: string; min: number; max: number }[] = [
  { key: "local_trigger_score", label: "Trigger sensitivity", hint: "0–100, lower = more candidates (default 55)", min: 10, max: 100 },
  { key: "candidate_cooldown_seconds", label: "Cooldown (s)", hint: "least time between candidates (45)", min: 10, max: 3600 },
  { key: "max_highlights_per_hour", label: "Max highlights / hour", hint: "(8)", min: 0, max: 60 },
  { key: "ai_eval_interval_seconds", label: "Periodic AI check (s)", hint: "0 = only on spikes (300)", min: 0, max: 7200 },
  { key: "source_pre_seconds", label: "Saved before event (s)", hint: "source clip length before (120)", min: 10, max: 270 },
  { key: "source_post_seconds", label: "Saved after event (s)", hint: "source clip length after (40)", min: 5, max: 120 },
];

export function DetectionSettingsForm({ value, onChange }: { value: DetectionSettings; onChange: (v: DetectionSettings) => void }) {
  const set = (k: string, v: string) => {
    const next = { ...value };
    if (v === "") delete next[k];
    else next[k] = k === "trigger_words" || k === "auto_upload" ? v : Number(v);
    onChange(next);
  };
  return (
    <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
      {FIELDS.map((f) => (
        <div key={f.key} className="space-y-1">
          <Label>{f.label}</Label>
          <Input type="number" min={f.min} max={f.max} placeholder="default" value={value[f.key] ?? ""} onChange={(e) => set(f.key, e.target.value)} />
          <p className="text-[11px] text-muted-foreground">{f.hint}</p>
        </div>
      ))}
      <div className="space-y-1 sm:col-span-2">
        <Label>Trigger phrases</Label>
        <Input placeholder="comma separated, e.g. no way, let's go" value={String(value.trigger_words ?? "")} onChange={(e) => set("trigger_words", e.target.value)} />
        <p className="text-[11px] text-muted-foreground">“clip it / clip that” always counts.</p>
      </div>
      <div className="space-y-1">
        <Label>Auto upload</Label>
        <NativeSelect value={String(value.auto_upload ?? "")} onChange={(e) => set("auto_upload", e.target.value)}>
          <option value="">server default</option>
          <option value="off">off (review first)</option>
          <option value="private">private</option>
          <option value="unlisted">unlisted</option>
          <option value="public">public</option>
        </NativeSelect>
      </div>
    </div>
  );
}
