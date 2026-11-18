"""Resource sampling and the global admission controller.

One sampler (in the API container) measures the whole machine every couple of seconds and stores the result in
Redis. Every worker asks `admit(kind)` before starting expensive work. Levels:

  normal    everything allowed
  high      no new streams, renders and periodic AI checks wait; ingestion, event checks and extraction continue
  critical  only ingestion of already running streams continues; transcription chunks are dropped

Docker memory limits are a last line of defence, not the plan: the plan is to never get near them.
"""
from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

import psutil

from . import redis_bus as rb
from .config import settings

KEY = rb.k("resources")

# what each kind of work needs: the highest pressure level it may still start at
ALLOWED_AT = {
    "ingest": "critical",        # keep existing streams alive
    "extract": "high",           # a highlight that was already found should still be saved
    "event_eval": "high",
    "transcribe": "high",
    "new_stream": "normal",
    "periodic_eval": "normal",
    "render": "normal",
    "upload": "high",
}
LEVELS = {"normal": 0, "high": 1, "critical": 2}


def cgroup_memory() -> dict:
    """This container's memory use and limit (cgroup v2, then v1). Empty outside a container."""
    for cur, lim in (("/sys/fs/cgroup/memory.current", "/sys/fs/cgroup/memory.max"),
                     ("/sys/fs/cgroup/memory/memory.usage_in_bytes", "/sys/fs/cgroup/memory/memory.limit_in_bytes")):
        try:
            used = int(Path(cur).read_text().strip())
            raw = Path(lim).read_text().strip()
            limit = 0 if raw == "max" else int(raw)
            if limit > 1 << 60:
                limit = 0
            return {"used_mb": round(used / 2**20, 1), "limit_mb": round(limit / 2**20, 1) if limit else 0,
                    "pct": round(used * 100 / limit, 1) if limit else 0}
        except (OSError, ValueError):
            continue
    return {}


def dir_size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


def sample() -> dict:
    vm = psutil.virtual_memory()
    try:
        du = shutil.disk_usage(settings.data_dir)
        disk = {"total_gb": round(du.total / 2**30, 1), "used_gb": round(du.used / 2**30, 1),
                "free_gb": round(du.free / 2**30, 1), "pct": round(du.used * 100 / du.total, 1)}
    except OSError:
        disk = {"total_gb": 0, "used_gb": 0, "free_gb": 0, "pct": 0}
    load1, load5, _ = os.getloadavg()
    snap = {
        "t": time.time(),
        "cpu_pct": psutil.cpu_percent(interval=None),
        "cpu_count": psutil.cpu_count() or 1,
        "load1": round(load1, 2), "load5": round(load5, 2),
        "mem_total_mb": round(vm.total / 2**20), "mem_available_mb": round(vm.available / 2**20),
        "mem_pct": round(vm.percent, 1),
        "swap_pct": round(psutil.swap_memory().percent, 1),
        "disk": disk,
    }
    snap["level"], snap["reasons"] = level_for(snap)
    return snap


def level_for(s: dict, data_bytes: int | None = None) -> tuple[str, list]:
    reasons, level = [], 0
    checks = [("cpu", s["cpu_pct"], settings.cpu_high_pct, settings.cpu_critical_pct),
              ("memory", s["mem_pct"], settings.mem_high_pct, settings.mem_critical_pct),
              ("disk", s["disk"]["pct"], settings.disk_high_pct, settings.disk_critical_pct)]
    for name, v, hi, crit in checks:
        if v >= crit:
            level = max(level, 2)
            reasons.append(f"{name} {v:.0f}% ≥ {crit:.0f}% (critical)")
        elif v >= hi:
            level = max(level, 1)
            reasons.append(f"{name} {v:.0f}% ≥ {hi:.0f}%")
    storage = s.get("storage_used_gb")
    if storage is not None and settings.storage_limit_gb > 0:
        if storage >= settings.storage_limit_gb:
            level = max(level, 2)
            reasons.append(f"storage {storage:.1f} GB ≥ limit {settings.storage_limit_gb:g} GB")
        elif storage >= settings.storage_limit_gb * 0.9:
            level = max(level, 1)
            reasons.append(f"storage {storage:.1f} GB ≥ 90% of limit")
    return ["normal", "high", "critical"][level], reasons


def publish_sample(extra: dict | None = None) -> dict:
    s = sample()
    if extra:
        s.update(extra)
        s["level"], s["reasons"] = level_for(s)
    rb.set_json(KEY, s, ttl=30)
    return s


_cache = {"t": 0.0, "v": None}


def current(max_age: float = 2.0) -> dict:
    if _cache["v"] is None or time.time() - _cache["t"] > max_age:
        v = rb.get_json(KEY)
        if v is None or time.time() - v.get("t", 0) > 20:
            v = sample()              # sampler is down: measure here rather than fly blind
        _cache.update(t=time.time(), v=v)
    return _cache["v"]


def admit(kind: str) -> tuple[bool, str]:
    """May work of this kind start now? Also refuses when this container is near its own memory limit."""
    s = current()
    allowed = ALLOWED_AT.get(kind, "normal")
    own = cgroup_memory()
    if own.get("pct", 0) >= 90 and kind != "ingest":
        return False, f"container memory {own['pct']:.0f}% of its limit"
    if LEVELS[s["level"]] > LEVELS[allowed]:
        return False, "; ".join(s.get("reasons") or [s["level"]])
    return True, ""


_procs: dict[int, psutil.Process] = {}


def _proc(pid: int) -> psutil.Process:
    """Process objects are kept between calls: psutil measures CPU as the change since the previous call."""
    p = _procs.get(pid)
    if p is None or not p.is_running():
        p = _procs[pid] = psutil.Process(pid)
    return p


def process_tree_usage(pid: int) -> dict:
    """RSS and CPU of a process and all its children (e.g. a stream worker and its FFmpeg)."""
    try:
        p = _proc(pid)
        procs = [p] + [_proc(c.pid) for c in p.children(recursive=True)]
    except psutil.Error:
        return {"rss_mb": 0, "cpu_pct": 0, "procs": 0}
    if len(_procs) > 500:                     # forget processes that have gone
        for dead in [q for q, o in _procs.items() if not o.is_running()]:
            _procs.pop(dead, None)
    rss, cpu = 0, 0.0
    for q in procs:
        try:
            rss += q.memory_info().rss
            cpu += q.cpu_percent(interval=None)
        except psutil.Error:
            pass
    return {"rss_mb": round(rss / 2**20, 1), "cpu_pct": round(cpu, 1), "procs": len(procs)}
