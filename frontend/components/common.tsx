"use client";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { cn } from "@/lib/utils";

const STATUS: Record<string, "success" | "warning" | "destructive" | "secondary" | "default"> = {
  live: "success", done: "success", ready: "success", accepted: "success", extracted: "success", approved: "success",
  connecting: "default", starting: "default", queued: "default", rendering: "default", uploading: "default", evaluating: "default",
  candidate: "default", reconnecting: "warning", deferred: "warning", waiting: "warning", offline: "secondary",
  disconnected: "warning", stopping: "secondary", stopped: "secondary", pending: "secondary", draft: "secondary",
  restarting: "warning", worker_down: "destructive", failed: "destructive", error: "destructive", rejected: "secondary",
  duplicate: "secondary", skipped: "secondary", none: "secondary", extracting: "default",
};

export function StatusBadge({ status, className }: { status?: string; className?: string }) {
  const s = status || "unknown";
  return (
    <Badge variant={STATUS[s] ?? "secondary"} className={className}>
      {s === "live" && <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-success" />}
      {s.replace("_", " ")}
    </Badge>
  );
}

export function PlatformBadge({ platform }: { platform?: string }) {
  const cls = platform === "twitch" ? "bg-[#9146ff]/20 text-[#b48cff]" : platform === "youtube" ? "bg-[#ff0033]/15 text-[#ff6b81]" : "bg-secondary text-secondary-foreground";
  const label = platform === "youtube" ? "YouTube Live" : platform === "twitch" ? "Twitch" : platform || "?";
  return <span className={cn("rounded-md px-2 py-0.5 text-xs font-medium", cls)}>{label}</span>;
}

export function Stat({ label, value, hint, tone }: { label: string; value: React.ReactNode; hint?: React.ReactNode; tone?: "warning" | "destructive" | "success" }) {
  return (
    <Card>
      <CardContent className="p-4">
        <div className="text-xs text-muted-foreground">{label}</div>
        <div className={cn("mt-1 text-2xl font-semibold tabular-nums", tone === "warning" && "text-warning", tone === "destructive" && "text-destructive", tone === "success" && "text-success")}>{value}</div>
        {hint && <div className="mt-0.5 text-xs text-muted-foreground">{hint}</div>}
      </CardContent>
    </Card>
  );
}

export function Sparkline({ values, min, max, className, color = "hsl(var(--primary))" }: { values: number[]; min?: number; max?: number; className?: string; color?: string }) {
  if (!values || values.length < 2) return <div className={cn("h-8", className)} />;
  const lo = min ?? Math.min(...values), hi = max ?? Math.max(...values);
  const span = hi - lo || 1;
  const pts = values.map((v, i) => `${(i / (values.length - 1)) * 100},${30 - ((Math.max(lo, Math.min(hi, v)) - lo) / span) * 28 - 1}`).join(" ");
  return (
    <svg viewBox="0 0 100 30" preserveAspectRatio="none" className={cn("h-8 w-full", className)}>
      <polyline points={pts} fill="none" stroke={color} strokeWidth="1.5" vectorEffect="non-scaling-stroke" />
    </svg>
  );
}

export function PageHeader({ title, description, children }: { title: string; description?: string; children?: React.ReactNode }) {
  return (
    <div className="mb-5 flex flex-wrap items-end justify-between gap-3">
      <div>
        <h1 className="text-xl font-semibold">{title}</h1>
        {description && <p className="mt-1 text-sm text-muted-foreground">{description}</p>}
      </div>
      <div className="flex flex-wrap gap-2">{children}</div>
    </div>
  );
}

export function Meter({ label, pct, detail }: { label: string; pct?: number; detail?: string }) {
  const v = pct ?? 0;
  const color = v >= 90 ? "bg-destructive" : v >= 80 ? "bg-warning" : "bg-primary";
  return (
    <div>
      <div className="mb-1 flex justify-between text-xs"><span className="text-muted-foreground">{label}</span><span className="tabular-nums">{pct == null ? "–" : `${v.toFixed(0)}%`}</span></div>
      <div className="h-2 overflow-hidden rounded-full bg-secondary"><div className={cn("h-full transition-all", color)} style={{ width: `${Math.min(100, v)}%` }} /></div>
      {detail && <div className="mt-1 text-xs text-muted-foreground">{detail}</div>}
    </div>
  );
}
