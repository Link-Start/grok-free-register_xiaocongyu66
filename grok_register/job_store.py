"""
Durable job snapshots for the control plane.

Architecture principle:
  - Workers (Python/Go/Rust) run on the host and write progress to disk.
  - The web UI is display-only for runtime progress (poll /api/status).
  - Configuration is editable via the control plane and stored in .env.

This module is the shared schema for job files under logs/.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOGS = PROJECT_ROOT / "logs"

# Canonical job files
REGISTER_JOB = LOGS / "register-job.json"
PROXY_BATCH_JOB = LOGS / "proxy-batch-job.json"
SCRAPE_JOB = LOGS / "scrape-job.json"
CONVERT_JOB = LOGS / "account-convert-job.json"
LAST_ACTION = LOGS / "dashboard-last-action.json"


def _path(env_key: str, default: Path) -> Path:
    raw = (os.environ.get(env_key) or "").strip()
    if not raw:
        return default
    p = Path(raw).expanduser()
    return p if p.is_absolute() else PROJECT_ROOT / p


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(data)
    payload.setdefault("updated_at", time.time())
    payload.setdefault("updated_at_iso", time.strftime("%Y-%m-%dT%H:%M:%S%z"))
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def pid_alive(pid: Any) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError, TypeError):
        return False


def normalize_job(raw: dict[str, Any] | None, *, kind: str) -> dict[str, Any]:
    """Normalize a job dict for UI consumption."""
    d = dict(raw or {})
    running = bool(d.get("running"))
    pid = d.get("pid")
    if running and pid and not pid_alive(pid):
        running = False
        d["running"] = False
        d["message"] = (d.get("message") or "") + " · 进程已退出"
        d.setdefault("finished_at", time.time())
    return {
        "kind": kind,
        "running": running,
        "engine": d.get("engine") or "",
        "pid": pid,
        "started_at": d.get("started_at") or 0,
        "finished_at": d.get("finished_at") or 0,
        "updated_at": d.get("updated_at") or 0,
        "message": d.get("message") or "",
        "error": d.get("error") or "",
        "total": d.get("total") or d.get("target") or 0,
        "tested": d.get("tested") or d.get("done") or 0,
        "ok": d.get("ok") or d.get("success") or d.get("ok_count") or 0,
        "fail": d.get("fail") or d.get("failed") or d.get("fail_count") or 0,
        "workers": d.get("workers") or 0,
        "progress_pct": _pct(d),
        "raw": d,
    }


def _pct(d: dict[str, Any]) -> float | None:
    total = d.get("total") or d.get("target") or 0
    try:
        total = float(total)
    except (TypeError, ValueError):
        return None
    if total <= 0:
        return None
    done = d.get("tested") or d.get("done") or d.get("success") or 0
    try:
        done = float(done)
    except (TypeError, ValueError):
        return None
    return round(min(100.0, max(0.0, 100.0 * done / total)), 1)


def write_register_job(**fields: Any) -> Path:
    path = _path("REGISTER_JOB_FILE", REGISTER_JOB)
    cur = read_json(path)
    cur.update(fields)
    cur.setdefault("kind", "register")
    atomic_write_json(path, cur)
    return path


def read_register_job() -> dict[str, Any]:
    path = _path("REGISTER_JOB_FILE", REGISTER_JOB)
    return normalize_job(read_json(path), kind="register")


def write_scrape_job(**fields: Any) -> Path:
    path = _path("SCRAPE_JOB_FILE", SCRAPE_JOB)
    cur = read_json(path)
    cur.update(fields)
    cur.setdefault("kind", "scrape")
    atomic_write_json(path, cur)
    return path


def read_scrape_job() -> dict[str, Any]:
    path = _path("SCRAPE_JOB_FILE", SCRAPE_JOB)
    return normalize_job(read_json(path), kind="scrape")


def control_plane_snapshot(
    *,
    register_runtime: dict[str, Any] | None = None,
    proxy_batch: dict[str, Any] | None = None,
    convert: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Unified jobs block for GET /api/status.
    Browser is display-only: it polls this structure.
    """
    reg_job = read_register_job()
    # Merge live runtime_status into register job for richer UI
    rt = register_runtime or {}
    if rt:
        metrics = rt.get("metrics") if isinstance(rt.get("metrics"), dict) else {}
        reg_job["raw"] = {**(reg_job.get("raw") or {}), "runtime": rt}
        if metrics:
            reg_job["ok"] = metrics.get("success_count") or reg_job.get("ok") or 0
            reg_job["tested"] = metrics.get("registration_starts") or reg_job.get("tested") or 0
            if rt.get("running"):
                reg_job["running"] = True
                reg_job["pid"] = rt.get("pid") or reg_job.get("pid")
                reg_job["engine"] = reg_job.get("engine") or "python"
                reg_job["message"] = (
                    f"注册运行中 · success={reg_job['ok']} starts={reg_job['tested']} "
                    f"T={((metrics.get('t') or {}).get('depth'))} "
                    f"Q={((metrics.get('q') or {}).get('depth'))}"
                )

    batch = normalize_job(proxy_batch, kind="proxy_batch") if proxy_batch is not None else normalize_job(
        read_json(_path("PROXY_BATCH_JOB_FILE", PROXY_BATCH_JOB)), kind="proxy_batch"
    )
    conv = normalize_job(convert, kind="convert") if convert is not None else normalize_job(
        read_json(_path("ACCOUNT_CONVERT_JOB_FILE", CONVERT_JOB)), kind="convert"
    )
    scrape = read_scrape_job()

    any_running = any(j.get("running") for j in (reg_job, batch, conv, scrape))
    return {
        "model": "server-runs / browser-displays / config-editable",
        "any_running": any_running,
        "register": reg_job,
        "proxy_batch": batch,
        "convert": conv,
        "scrape": scrape,
        "files": {
            "register": str(_path("REGISTER_JOB_FILE", REGISTER_JOB)),
            "proxy_batch": str(_path("PROXY_BATCH_JOB_FILE", PROXY_BATCH_JOB)),
            "convert": str(_path("ACCOUNT_CONVERT_JOB_FILE", CONVERT_JOB)),
            "scrape": str(_path("SCRAPE_JOB_FILE", SCRAPE_JOB)),
            "runtime_status": str(LOGS / "runtime-status.json"),
        },
    }
