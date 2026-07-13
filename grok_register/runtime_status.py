"""
Runtime status publisher — bridge between register process and control plane.

Register writes `logs/runtime-status.json` on each monitor tick.
Control plane / dashboard reads it without coupling to asyncio internals.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STATUS_PATH = PROJECT_ROOT / "logs" / "runtime-status.json"
DEFAULT_PID_PATH = PROJECT_ROOT / "logs" / "register.pid"


def status_path() -> Path:
    raw = (os.environ.get("RUNTIME_STATUS_FILE") or "").strip()
    if raw:
        p = Path(raw).expanduser()
        return p if p.is_absolute() else PROJECT_ROOT / p
    return DEFAULT_STATUS_PATH


def pid_path() -> Path:
    raw = (os.environ.get("REGISTER_PID_FILE") or "").strip()
    if raw:
        p = Path(raw).expanduser()
        return p if p.is_absolute() else PROJECT_ROOT / p
    return DEFAULT_PID_PATH


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def publish(snapshot: dict[str, Any]) -> None:
    payload = dict(snapshot)
    payload.setdefault("updated_at", time.time())
    payload.setdefault("updated_at_iso", time.strftime("%Y-%m-%dT%H:%M:%S%z"))
    _atomic_write(status_path(), json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def read_status() -> dict[str, Any]:
    path = status_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def write_pid(pid: int) -> None:
    _atomic_write(pid_path(), f"{int(pid)}\n")


def read_pid() -> int | None:
    try:
        raw = pid_path().read_text(encoding="utf-8").strip()
        return int(raw) if raw else None
    except Exception:
        return None


def clear_pid() -> None:
    try:
        pid_path().unlink(missing_ok=True)
    except Exception:
        pass


def process_alive(pid: int | None = None) -> bool:
    pid = pid if pid is not None else read_pid()
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def build_register_snapshot(
    *,
    metrics,
    inventory,
    sems,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    snap = {
        "service": "register",
        "pid": os.getpid(),
        "running": True,
        "metrics": metrics.to_dict(inventory, sems) if hasattr(metrics, "to_dict") else {},
    }
    if extra:
        snap.update(extra)
    return snap
