"use client";
import { Activity, Film, LayoutDashboard, ListChecks, LogOut, PlusCircle, Radio, Server, Scissors } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { post } from "@/lib/api";
import { useLive } from "@/lib/events";
import { cn } from "@/lib/utils";

const LINKS = [
  { href: "/", label: "Dashboard", icon: LayoutDashboard },
  { href: "/streams/new", label: "Add streams", icon: PlusCircle },
  { href: "/streams", label: "Monitoring", icon: Radio },
  { href: "/clips", label: "Highlights", icon: Film },
  { href: "/jobs", label: "Jobs", icon: ListChecks },
  { href: "/system", label: "System", icon: Server },
];

export function Nav({ role }: { role: string }) {
  const path = usePathname();
  const { connected, snapshot } = useLive();
  const level = snapshot?.resources?.level;
  return (
    <aside className="sticky top-0 z-30 flex shrink-0 flex-row items-center gap-1 overflow-x-auto border-b bg-card/80 px-2 py-2 backdrop-blur md:h-screen md:w-56 md:flex-col md:items-stretch md:border-b-0 md:border-r md:px-3 md:py-4">
      <div className="mr-2 flex items-center gap-2 px-2 md:mb-4">
        <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-primary/15 text-primary">
          <Scissors className="h-4 w-4" />
        </div>
        <span className="hidden font-semibold md:inline">ClippingShorts</span>
      </div>
      {LINKS.map(({ href, label, icon: Icon }) => {
        const active = href === "/" ? path === "/" : path === href || (href !== "/streams" && path.startsWith(href)) || (href === "/streams" && path === "/streams");
        return (
          <Link key={href} href={href}
            className={cn("flex items-center gap-2 whitespace-nowrap rounded-md px-3 py-2 text-sm text-muted-foreground transition-colors hover:bg-accent hover:text-foreground",
              active && "bg-accent text-foreground")}>
            <Icon className="h-4 w-4" />
            <span className="hidden sm:inline">{label}</span>
          </Link>
        );
      })}
      <div className="ml-auto flex items-center gap-2 md:ml-0 md:mt-auto md:flex-col md:items-stretch">
        <div className="flex items-center gap-2 px-3 py-1 text-xs text-muted-foreground">
          <Activity className={cn("h-3.5 w-3.5", connected ? "text-success" : "text-destructive")} />
          <span className="hidden md:inline">{connected ? "Live updates" : "Reconnecting…"}</span>
          {level && level !== "normal" && <span className={cn("rounded px-1.5", level === "critical" ? "bg-destructive/20 text-destructive" : "bg-warning/20 text-warning")}>{level}</span>}
        </div>
        <div className="hidden px-3 text-xs text-muted-foreground md:block">Signed in as {role}</div>
        <button
          onClick={async () => { await post("/api/auth/logout"); window.location.href = "/login"; }}
          className="flex items-center gap-2 rounded-md px-3 py-2 text-sm text-muted-foreground hover:bg-accent hover:text-foreground">
          <LogOut className="h-4 w-4" />
          <span className="hidden md:inline">Sign out</span>
        </button>
      </div>
    </aside>
  );
}
