"use client";
import { useQuery } from "@tanstack/react-query";
import { Nav } from "@/components/nav";
import { api } from "@/lib/api";
import { LiveProvider } from "@/lib/events";

export default function AppLayout({ children }: { children: React.ReactNode }) {
  const me = useQuery({ queryKey: ["me"], queryFn: () => api<{ role: string }>("/api/auth/me"), retry: false });
  if (me.isLoading) return <div className="p-8 text-sm text-muted-foreground">Loading…</div>;
  if (me.isError || !me.data) {
    if (typeof window !== "undefined") window.location.href = "/login";
    return null;
  }
  return (
    <LiveProvider>
      <div className="flex min-h-screen flex-col md:flex-row">
        <Nav role={me.data.role} />
        <main className="min-w-0 flex-1 p-4 md:p-6">{children}</main>
      </div>
    </LiveProvider>
  );
}
