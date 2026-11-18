"use client";
// Captions are edited line by line. Changing a line's text keeps its timing: the new words share the line's
// time span evenly. "Reset" goes back to the transcript.
import { RotateCcw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import type { Word } from "@/lib/types";
import { fmtTime } from "@/lib/utils";
import { groups } from "./preview";

export function CaptionsEditor({ words, start, end, per, onChange, onReset, onSeek, current }: {
  words: Word[]; start: number; end: number; per: number; onChange: (w: Word[]) => void; onReset: () => void; onSeek: (t: number) => void; current: number;
}) {
  const inside = words.filter((w) => w.s >= start - 0.05 && w.s < end);
  const lines = groups(inside, Math.max(per, 4));

  function editLine(idx: number, text: string) {
    const line = lines[idx];
    const s = line[0].s, e = line[line.length - 1].e;
    const parts = text.split(/\s+/).filter(Boolean);
    const step = (e - s) / Math.max(1, parts.length);
    const fresh = parts.map((w, i) => ({ w, s: +(s + i * step).toFixed(2), e: +(s + (i + 1) * step - 0.02).toFixed(2) }));
    const others = words.filter((w) => !line.includes(w));
    onChange([...others, ...fresh].sort((a, b) => a.s - b.s));
  }

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between">
        <span className="text-xs text-muted-foreground">{inside.length} words in the selection</span>
        <Button size="sm" variant="ghost" onClick={onReset}><RotateCcw /> Reset to transcript</Button>
      </div>
      {lines.length === 0 && <p className="text-sm text-muted-foreground">No speech was transcribed in this range.</p>}
      <div className="max-h-[340px] space-y-1 overflow-y-auto pr-1">
        {lines.map((line, i) => {
          const active = current >= line[0].s && current < line[line.length - 1].e + 0.2;
          return (
            <div key={`${line[0].s}-${i}`} className={`flex items-center gap-2 rounded-md p-1 ${active ? "bg-primary/10" : ""}`}>
              <button className="w-12 shrink-0 text-left text-[11px] tabular-nums text-muted-foreground hover:text-foreground" onClick={() => onSeek(line[0].s)}>{fmtTime(line[0].s - start)}</button>
              <Input className="h-8" defaultValue={line.map((w) => w.w).join(" ")} onBlur={(e) => e.target.value !== line.map((w) => w.w).join(" ") && editLine(i, e.target.value)} />
            </div>
          );
        })}
      </div>
    </div>
  );
}
