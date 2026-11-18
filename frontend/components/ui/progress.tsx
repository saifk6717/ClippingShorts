"use client";
import * as ProgressPrimitive from "@radix-ui/react-progress";
import * as React from "react";
import { cn } from "@/lib/utils";

export function Progress({ value = 0, className, indicatorClassName }: { value?: number; className?: string; indicatorClassName?: string }) {
  const v = Math.max(0, Math.min(100, value || 0));
  return (
    <ProgressPrimitive.Root value={v} className={cn("relative h-2 w-full overflow-hidden rounded-full bg-secondary", className)}>
      <ProgressPrimitive.Indicator className={cn("h-full bg-primary transition-all", indicatorClassName)} style={{ width: `${v}%` }} />
    </ProgressPrimitive.Root>
  );
}
