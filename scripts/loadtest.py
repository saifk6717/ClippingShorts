#!/usr/bin/env python3
"""Synthetic load test: start N synthetic streams through the API and MEASURE what they cost on this machine.

    python3 scripts/loadtest.py --url http://127.0.0.1:3100 --password '<admin password>' --streams 5 --minutes 5

Needs CLIPPINGSHORTS_ENABLE_SYNTHETIC=1 on the server. Uses only the Python standard library. The streams it
creates are named loadtest-* and are stopped and deleted at the end (other streams are not touched).
Writes loadtest-results/<timestamp>-<N>streams.json and .md with the measured numbers.
"""
import argparse
import http.cookiejar
import json
import statistics
import time
import urllib.request
from pathlib import Path


class Api:
    def __init__(self, base, password):
        self.base = base.rstrip("/")
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.call("POST", "/api/auth/login", {"password": password})

    def call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else (b"{}" if method != "GET" else None)
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json", "X-ClippingShorts": "1"})
        with self.op.open(req, timeout=60) as r:
            t = r.read()
            return json.loads(t) if t else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:3100")
    ap.add_argument("--password", required=True)
    ap.add_argument("--streams", type=int, default=2)
    ap.add_argument("--minutes", type=float, default=5)
    ap.add_argument("--interval", type=float, default=10)
    a = ap.parse_args()
    api = Api(a.url, a.password)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    items = [{"url": f"synthetic://loadtest-{stamp}-{i}?bursts=60&chat={2 + i % 4}&spam=1", "platform": "synthetic",
              "creator_name": f"loadtest-{i}"} for i in range(a.streams)]
    created = []
    for i in range(0, len(items), 10):
        res = api.call("POST", "/api/streams", {"streams": items[i:i + 10], "start": True})
        created += [c["id"] for c in res["created"]]
        for e in res["errors"]:
            print("not added:", e)
    print(f"started {len(created)} synthetic streams; measuring for {a.minutes} min…")
    samples, t_end, t0 = [], time.time() + a.minutes * 60, time.time()
    try:
        while time.time() < t_end:
            res = api.call("GET", "/api/system/resources")
            streams = api.call("GET", "/api/streams")["streams"]
            mine = [s for s in streams if s["id"] in created]
            per = [res["streams"].get(s["id"]) or {} for s in mine]
            samples.append({
                "t": round(time.time() - t0), "cpu_pct": res["cpu_pct"], "mem_pct": res["mem_pct"],
                "mem_available_mb": res["mem_available_mb"], "level": res["level"],
                "live": sum(1 for s in mine if s["status"] == "live"),
                "stream_rss_mb": [p.get("rss_mb", 0) for p in per], "stream_cpu_pct": [p.get("cpu_pct", 0) for p in per],
                "buffer_bytes": sum((s.get("live") or {}).get("buffer", {}).get("bytes", 0) for s in mine),
                "tr_lag": [(s.get("live") or {}).get("transcription", {}).get("lag") for s in mine],
                "containers": {k: (v.get("container") or {}).get("used_mb") for k, v in res["workers"].items()},
            })
            s = samples[-1]
            print(f"t={s['t']:>4}s live={s['live']}/{len(created)} cpu={s['cpu_pct']:.0f}% mem={s['mem_pct']:.0f}% "
                  f"avg stream rss={statistics.mean(s['stream_rss_mb'] or [0]):.0f}MB level={s['level']}")
            time.sleep(a.interval)
        dash = api.call("GET", "/api/dashboard")
    finally:
        for sid in created:
            try:
                api.call("POST", f"/api/streams/{sid}/stop")
            except Exception:  # noqa: BLE001
                pass
        time.sleep(15)
        for sid in created:
            try:
                api.call("DELETE", f"/api/streams/{sid}")
            except Exception:  # noqa: BLE001
                pass
    steady = [s for s in samples if s["live"] == len(created)] or samples
    rss = [v for s in steady for v in s["stream_rss_mb"] if v]
    cpu = [v for s in steady for v in s["stream_cpu_pct"]]
    seg_rate = None
    if len(steady) >= 2:
        seg_rate = None  # the buffer is rolling (bounded), so its size is reported, not a growth rate
    report = {
        "streams": len(created), "minutes": a.minutes, "samples": len(samples),
        "per_stream_rss_mb": {"median": round(statistics.median(rss), 1) if rss else None, "max": max(rss) if rss else None},
        "per_stream_cpu_pct": {"median": round(statistics.median(cpu), 1) if cpu else None, "max": max(cpu) if cpu else None},
        "host_cpu_pct": {"median": round(statistics.median(s["cpu_pct"] for s in steady), 1),
                         "max": max(s["cpu_pct"] for s in steady)},
        "host_mem_pct_max": max(s["mem_pct"] for s in steady),
        "min_mem_available_mb": min(s["mem_available_mb"] for s in steady),
        "rolling_buffer_bytes_max": max(s["buffer_bytes"] for s in steady),
        "pressure_levels_seen": sorted({s["level"] for s in samples}),
        "latency": dash["latency"], "containers_last": samples[-1]["containers"] if samples else {},
        "raw": samples, "seg_rate": seg_rate,
    }
    out = Path("loadtest-results")
    out.mkdir(exist_ok=True)
    base = out / f"{stamp}-{len(created)}streams"
    base.with_suffix(".json").write_text(json.dumps(report, indent=1))
    md = [f"# Load test: {len(created)} synthetic streams, {a.minutes} min", "",
          f"- per-stream RSS (worker + FFmpeg): median {report['per_stream_rss_mb']['median']} MB, max {report['per_stream_rss_mb']['max']} MB",
          f"- per-stream CPU: median {report['per_stream_cpu_pct']['median']}%, max {report['per_stream_cpu_pct']['max']}%",
          f"- host CPU: median {report['host_cpu_pct']['median']}%, max {report['host_cpu_pct']['max']}%",
          f"- host memory: max {report['host_mem_pct_max']}%, lowest available {report['min_mem_available_mb']} MB",
          f"- rolling buffers on disk (all streams): max {report['rolling_buffer_bytes_max'] / 2**20:.0f} MB",
          f"- pressure levels seen: {', '.join(report['pressure_levels_seen'])}",
          f"- latencies: {json.dumps(report['latency'])}"]
    base.with_suffix(".md").write_text("\n".join(md) + "\n")
    print("\n".join(md))
    print(f"\nreport: {base}.json / .md")


if __name__ == "__main__":
    main()
