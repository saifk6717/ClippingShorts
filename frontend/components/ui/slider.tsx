"use client";
import * as React from "react";
import { cn } from "@/lib/utils";

// A native range input styled to match: keyboard accessible and light.
export function Slider({ value, min, max, step = 1, onChange, className, ...rest }: {
  value: number; min: number; max: number; step?: number; onChange: (v: number) => void; className?: string;
} & Omit<React.InputHTMLAttributes<HTMLInputElement>, "onChange" | "value">) {
  return (
    <input
      type="range"
      value={value}
      min={min}
      max={max}
      step={step}
      onChange={(e) => onChange(Number(e.target.value))}
      className={cn("h-2 w-full cursor-pointer appearance-none rounded-full bg-secondary accent-[hsl(var(--primary))]", className)}
      {...rest}
    />
  );
}
