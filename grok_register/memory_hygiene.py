"""Lightweight process memory hygiene for long-running register/convert jobs.

Used after SSO export / CPA convert batches to reduce RSS growth under low RAM.
"""
from __future__ import annotations

import gc
import os
import threading
import time
from typing import Any

_lock = threading.Lock()
_last_trim_at = 0.0
_ops_since_trim = 0


def env_bool(name: str, default: bool = False) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return default


def env_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def rss_mb() -> float:
    try:
        with open("/proc/self/status", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024.0
    except Exception:
        pass
    try:
        import resource

        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
    except Exception:
        return 0.0


def host_available_mb() -> int:
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except Exception:
        pass
    return 0


def malloc_trim() -> None:
    try:
        import ctypes

        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:
        pass


def trim_memory(*, full: bool = True, force: bool = False) -> dict[str, Any]:
    """Run GC (+ optional malloc_trim). Rate-limited unless force=True."""
    global _last_trim_at, _ops_since_trim
    min_interval = max(1.0, float(env_int("MEMORY_TRIM_MIN_INTERVAL_SEC", 8)))
    now = time.time()
    with _lock:
        if not force and (now - _last_trim_at) < min_interval:
            return {"skipped": True, "rss_mb": round(rss_mb(), 2)}
        _last_trim_at = now
        _ops_since_trim = 0
    before = rss_mb()
    try:
        if full:
            gc.collect(2)
        else:
            gc.collect()
    except Exception:
        pass
    if env_bool("MEMORY_MALLOC_TRIM", True):
        malloc_trim()
    after = rss_mb()
    return {
        "skipped": False,
        "rss_before_mb": round(before, 2),
        "rss_after_mb": round(after, 2),
        "host_available_mb": host_available_mb(),
    }


def note_op_and_maybe_trim(every: int | None = None) -> dict[str, Any] | None:
    """Call after each registration/convert unit of work."""
    global _ops_since_trim
    n = every if every is not None else max(1, env_int("MEMORY_TRIM_EVERY_OPS", 8))
    with _lock:
        _ops_since_trim += 1
        count = _ops_since_trim
    if count >= n:
        return trim_memory(full=True)
    # Under pressure, trim more often
    avail = host_available_mb()
    if avail and avail < 1200 and count >= max(1, n // 2):
        return trim_memory(full=True)
    return None
