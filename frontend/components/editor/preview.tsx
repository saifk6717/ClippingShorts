"use client";
// The video streams from the server with HTTP Range requests (preload="metadata"): the browser only fetches what it
// plays. On top: the crop window for the chosen aspect (drag it), and a preview of the captions and hook.
import { forwardRef, useMemo, useRef } from "react";
import type { EditParams, Word } from "@/lib/types";

const STYLE = {
  bounce: { font: "Poppins ExtraBold", size: 132, active: "#FFF000", base: "#FFFFFF", upper: true, per: 3 },
  hype: { font: "Anton", size: 152, active: "#FF3C3C", base: "#FFFFFF", upper: true, per: 2 },
  clean: { font: "Poppins ExtraBold", size: 100, active: "#FFD76B", base: "#FFFFFF", upper: false, per: 4 },
};

export function groups(words: Word[], per: number): Word[][] {
  const out: Word[][] = [];
  let cur: Word[] = [];
  for (const w of words) {
    if (cur.length && (cur.length >= per || cur.reduce((a, x) => a + x.w.length, 0) + w.w.length > 16 || w.s - cur[cur.length - 1].e > 0.45 || /[.?!]$/.test(cur[cur.length - 1].w))) {
      out.push(cur);
      cur = [];
    }
    cur.push(w);
  }
  if (cur.length) out.push(cur);
  return out;
}

export function cropBox(p: EditParams, srcW: number, srcH: number, cropX: number) {
  const [aw, ah] = p.aspect.split(":").map(Number);
  const A = aw / ah, srcA = srcW / Math.max(1, srcH);
  const z = Math.max(1, p.zoom || 1);
  let cw: number, ch: number;
  if (srcA > A) { cw = (A / srcA) / z; ch = 1 / z; } else { cw = 1 / z; ch = (srcA / A) / z; }
  const cy = p.crop_y ?? 0.5;
  const left = Math.min(Math.max(0, cropX - cw / 2), 1 - cw);
  const top = Math.min(Math.max(0, cy - ch / 2), 1 - ch);
  return { left, top, cw, ch };
}

interface Props {
  src: string; width: number; height: number; params: EditParams; words: Word[]; time: number; onTime: (t: number) => void;
  onCrop: (x: number, y: number) => void; onLoaded?: (d: number) => void;
}

export const Preview = forwardRef<HTMLVideoElement, Props>(function Preview({ src, width, height, params, words, time, onTime, onCrop, onLoaded }, ref) {
  const box = useRef<HTMLDivElement>(null);
  const drag = useRef<{ x0: number; y0: number; cx: number; cy: number } | null>(null);
  const cropX = params.crop_x ?? 0.5;
  const crop = cropBox(params, width, height, cropX);
  const blur = params.fit === "blur";
  const st = STYLE[params.captions.style] ?? STYLE.bounce;
  const grouped = useMemo(() => groups(words.filter((w) => w.s >= params.start - 0.05 && w.s < params.end), st.per), [words, params.start, params.end, st.per]);
  const g = grouped.find((x, i) => time >= x[0].s && time < Math.max(x[x.length - 1].e + 0.25, grouped[i + 1]?.[0].s ?? params.end));

  function down(e: React.PointerEvent) {
    if (blur) return;
    (e.target as HTMLElement).setPointerCapture(e.pointerId);
    drag.current = { x0: e.clientX, y0: e.clientY, cx: crop.left + crop.cw / 2, cy: crop.top + crop.ch / 2 };
  }
  function move(e: React.PointerEvent) {
    const d = drag.current;
    if (!d || !box.current) return;
    const r = box.current.getBoundingClientRect();
    const x = Math.min(1 - crop.cw / 2, Math.max(crop.cw / 2, d.cx + (e.clientX - d.x0) / r.width));
    const y = Math.min(1 - crop.ch / 2, Math.max(crop.ch / 2, d.cy + (e.clientY - d.y0) / r.height));
    onCrop(Number(x.toFixed(4)), Number(y.toFixed(4)));
  }

  // the output frame inside the preview, to place captions/hook like the render will
  const frame = blur ? { left: 0, top: 0, cw: 1, ch: 1 } : crop;
  const outW = (box.current?.clientWidth ?? 640) * frame.cw;
  const isLandscape = params.aspect === "16:9";
  const k = (outW / 1080) * (params.captions.font_scale || 1) * (isLandscape ? 0.62 : 1);

  return (
    <div ref={box} className="relative w-full overflow-hidden rounded-md bg-black" style={{ aspectRatio: `${width || 16} / ${height || 9}` }}
      onPointerMove={move} onPointerUp={() => (drag.current = null)}>
      <video ref={ref} src={src} preload="metadata" playsInline className="absolute inset-0 h-full w-full"
        onTimeUpdate={(e) => onTime(e.currentTarget.currentTime)} onLoadedMetadata={(e) => onLoaded?.(e.currentTarget.duration)} />
      {!blur && (
        <>
          <div className="pointer-events-none absolute inset-0 bg-black/55" style={{ clipPath: `polygon(0 0, 100% 0, 100% 100%, 0 100%, 0 0, ${crop.left * 100}% ${crop.top * 100}%, ${crop.left * 100}% ${(crop.top + crop.ch) * 100}%, ${(crop.left + crop.cw) * 100}% ${(crop.top + crop.ch) * 100}%, ${(crop.left + crop.cw) * 100}% ${crop.top * 100}%, ${crop.left * 100}% ${crop.top * 100}%)` }} />
          <div onPointerDown={down} className="absolute cursor-move border-2 border-primary"
            style={{ left: `${crop.left * 100}%`, top: `${crop.top * 100}%`, width: `${crop.cw * 100}%`, height: `${crop.ch * 100}%` }}>
            <span className="absolute left-1 top-1 rounded bg-primary px-1 text-[10px] text-primary-foreground">{params.aspect}{params.crop_x == null && params.smart_crop ? " · smart crop at render" : ""}</span>
          </div>
        </>
      )}
      <div className="pointer-events-none absolute" style={{ left: `${frame.left * 100}%`, top: `${frame.top * 100}%`, width: `${frame.cw * 100}%`, height: `${frame.ch * 100}%` }}>
        {params.hook.enabled && params.hook.text && (
          <div className="absolute left-1/2 -translate-x-1/2 -translate-y-1/2 rounded-xl bg-white px-3 py-1 text-center font-extrabold leading-tight text-[#0E1014]"
            style={{ top: `${params.hook.position * 100}%`, fontFamily: "Poppins ExtraBold", fontSize: Math.max(9, 96 * k * 0.8), maxWidth: "88%" }}>
            {params.hook.text}
          </div>
        )}
        {params.captions.enabled && g && (
          <div className="absolute inset-x-0 -translate-y-1/2 text-center" style={{ top: `${params.captions.position * 100}%`, fontFamily: st.font, fontSize: Math.max(9, st.size * k * 0.8), lineHeight: 1.1, WebkitTextStroke: `${Math.max(1, 6 * k)}px black`, paintOrder: "stroke fill" }}>
            {g.map((w, i) => {
              const active = time >= w.s && time < (g[i + 1]?.s ?? w.e + 0.25);
              const t = w.w.replace(/[.,;:]+$/, "");
              return <span key={i} className="mx-[0.18em] inline-block transition-transform" style={{ color: active ? st.active : st.base, transform: active ? "scale(1.1)" : "none" }}>{st.upper ? t.toUpperCase() : t}</span>;
            })}
          </div>
        )}
      </div>
    </div>
  );
});
