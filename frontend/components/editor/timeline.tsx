"use client";
// Timeline: thumbnail strip (one sprite sheet, never the video itself), waveform, selection with start/end handles,
// and the playhead. Drag a handle to trim, drag the selection to move it, click anywhere to seek.
import { useEffect, useRef, useState } from "react";
import { fmtTime } from "@/lib/utils";

interface Sprite { every: number; cols: number; tile_w: number; rows: number; sheet_h: number }

export function Timeline({ duration, start, end, current, sprite, spriteUrl, peaks, perSecond, onChange, onSeek }: {
  duration: number; start: number; end: number; current: number; sprite?: Sprite | null; spriteUrl?: string | null;
  peaks: number[]; perSecond: number; onChange: (s: number, e: number) => void; onSeek: (t: number) => void;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const canvas = useRef<HTMLCanvasElement>(null);
  const [width, setWidth] = useState(800);
  const drag = useRef<{ kind: "start" | "end" | "move"; x0: number; s0: number; e0: number } | null>(null);

  useEffect(() => {
    if (!ref.current) return;
    const ro = new ResizeObserver(([e]) => setWidth(e.contentRect.width));
    ro.observe(ref.current);
    return () => ro.disconnect();
  }, []);

  useEffect(() => {
    const c = canvas.current;
    if (!c || !peaks.length) return;
    const dpr = window.devicePixelRatio || 1;
    c.width = width * dpr;
    c.height = 48 * dpr;
    const ctx = c.getContext("2d")!;
    ctx.scale(dpr, dpr);
    ctx.clearRect(0, 0, width, 48);
    ctx.fillStyle = "rgba(167,139,250,0.75)";
    const total = duration * perSecond;
    for (let x = 0; x < width; x++) {
      const a = Math.floor((x / width) * total), b = Math.max(a + 1, Math.floor(((x + 1) / width) * total));
      let m = 0;
      for (let i = a; i < b && i < peaks.length; i++) m = Math.max(m, peaks[i]);
      const h = Math.max(1, Math.sqrt(m) * 44);
      ctx.fillRect(x, 24 - h / 2, 1, h);
    }
  }, [peaks, width, duration, perSecond]);

  const toT = (clientX: number) => {
    const r = ref.current!.getBoundingClientRect();
    return Math.max(0, Math.min(duration, ((clientX - r.left) / r.width) * duration));
  };
  const px = (t: number) => (t / Math.max(duration, 0.001)) * 100;

  function down(kind: "start" | "end" | "move", e: React.PointerEvent) {
    e.stopPropagation();
    (e.target as HTMLElement).setPointerCapture(e.pointerId);
    drag.current = { kind, x0: e.clientX, s0: start, e0: end };
  }
  function move(e: React.PointerEvent) {
    const d = drag.current;
    if (!d) return;
    const t = toT(e.clientX);
    if (d.kind === "start") onChange(Math.min(t, d.e0 - 1), d.e0);
    else if (d.kind === "end") onChange(d.s0, Math.max(t, d.s0 + 1));
    else {
      const dt = ((e.clientX - d.x0) / width) * duration;
      const len = d.e0 - d.s0;
      const s = Math.max(0, Math.min(duration - len, d.s0 + dt));
      onChange(s, s + len);
    }
  }
  const up = (e: React.PointerEvent) => {
    const d = drag.current;
    if (d && d.kind === "move" && Math.abs(e.clientX - d.x0) < 3) onSeek(toT(e.clientX)); // a click, not a drag
    drag.current = null;
  };

  const tiles = sprite && spriteUrl ? Math.ceil(duration / sprite.every) : 0;
  const tileH = sprite && sprite.rows ? sprite.sheet_h / sprite.rows : 90;
  const ticks = Math.min(12, Math.max(4, Math.floor(width / 90)));

  return (
    <div className="select-none space-y-1">
      <div ref={ref} className="relative h-[104px] cursor-pointer overflow-hidden rounded-md border bg-muted"
        onPointerDown={(e) => onSeek(toT(e.clientX))} onPointerMove={move} onPointerUp={up}>
        {tiles > 0 && sprite && (
          <div className="absolute inset-x-0 top-0 flex h-14 overflow-hidden opacity-80">
            {Array.from({ length: tiles }).map((_, i) => {
              const col = i % sprite.cols, row = Math.floor(i / sprite.cols);
              const scale = 56 / tileH;
              return (
                <div key={i} className="h-14 shrink-0" style={{
                  width: `${(sprite.every / duration) * 100}%`,
                  backgroundImage: `url(${spriteUrl})`, backgroundRepeat: "no-repeat",
                  backgroundSize: `${sprite.cols * sprite.tile_w * scale}px auto`,
                  backgroundPosition: `-${col * sprite.tile_w * scale}px -${row * tileH * scale}px`,
                }} />
              );
            })}
          </div>
        )}
        <canvas ref={canvas} className="absolute inset-x-0 bottom-0 h-12 w-full" style={{ width }} />
        <div className="absolute inset-y-0 left-0 bg-black/60" style={{ width: `${px(start)}%` }} />
        <div className="absolute inset-y-0 right-0 bg-black/60" style={{ width: `${100 - px(end)}%` }} />
        <div className="absolute inset-y-0 cursor-grab border-y-2 border-primary" style={{ left: `${px(start)}%`, width: `${px(end) - px(start)}%` }}
          onPointerDown={(e) => down("move", e)} />
        <div className="absolute inset-y-0 w-3 -translate-x-1/2 cursor-ew-resize rounded-sm bg-primary" style={{ left: `${px(start)}%` }} onPointerDown={(e) => down("start", e)} />
        <div className="absolute inset-y-0 w-3 -translate-x-1/2 cursor-ew-resize rounded-sm bg-primary" style={{ left: `${px(end)}%` }} onPointerDown={(e) => down("end", e)} />
        <div className="pointer-events-none absolute inset-y-0 w-0.5 bg-white" style={{ left: `${px(current)}%` }} />
      </div>
      <div className="relative h-4 text-[10px] text-muted-foreground">
        {Array.from({ length: ticks + 1 }).map((_, i) => (
          <span key={i} className="absolute -translate-x-1/2" style={{ left: `${(i / ticks) * 100}%` }}>{fmtTime((i / ticks) * duration)}</span>
        ))}
      </div>
    </div>
  );
}
