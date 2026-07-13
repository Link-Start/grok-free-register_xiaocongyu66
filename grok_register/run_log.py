"""
Register / solver run logs for the web panel (downloadable).

Files under logs/ (HF: /app/logs, often shared via /data when mounted):
  register-dashboard.log  — full stdout/stderr of dashboard-spawned register
  register-fail.jsonl     — structured fail / exit / spawn events (one JSON per line)
  register-live.log       — optional live ring (same stream as terminal when enabled)

Panel downloads:
  GET /api/download?format=register_log
  GET /api/download?format=register_fail
  GET /api/download?format=register_logs_zip
"""
from __future__ import annotations

import json
import os
import threading
import time
import zipfile
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
_lock = threading.Lock()

# Keep fail log bounded so HF disk does not grow forever
_FAIL_MAX_BYTES = max(256_000, int(os.environ.get("REGISTER_FAIL_LOG_MAX_BYTES") or 2_000_000))
_DASH_MAX_BYTES = max(512_000, int(os.environ.get("REGISTER_DASH_LOG_MAX_BYTES") or 8_000_000))


def logs_dir() -> Path:
    raw = (os.environ.get("REGISTER_LOG_DIR") or "").strip()
    if raw:
        p = Path(raw).expanduser()
        return p if p.is_absolute() else PROJECT_ROOT / p
    # Prefer /data/logs on HF when present
    data_logs = Path("/data/logs")
    if data_logs.is_dir() or os.environ.get("KEY_EXPORT_DIR", "").startswith("/data"):
        try:
            data_logs.mkdir(parents=True, exist_ok=True)
            return data_logs
        except OSError:
            pass
    d = PROJECT_ROOT / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def register_dashboard_log_path() -> Path:
    return logs_dir() / "register-dashboard.log"


def register_fail_log_path() -> Path:
    return logs_dir() / "register-fail.jsonl"


def register_live_log_path() -> Path:
    return logs_dir() / "register-live.log"


def _trim_file(path: Path, max_bytes: int) -> None:
    try:
        if not path.is_file() or path.stat().st_size <= max_bytes:
            return
        data = path.read_bytes()
        # keep last 75% of budget
        keep = data[-(max_bytes * 3 // 4) :]
        # align to newline
        nl = keep.find(b"\n")
        if nl >= 0:
            keep = keep[nl + 1 :]
        path.write_bytes(keep)
    except OSError:
        pass


def append_fail(
    kind: str,
    message: str,
    *,
    level: str = "error",
    engine: str = "",
    worker: int | None = None,
    exit_code: int | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Append one structured failure / lifecycle line to register-fail.jsonl."""
    path = register_fail_log_path()
    rec: dict[str, Any] = {
        "ts": time.time(),
        "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "level": level,
        "kind": kind,
        "message": (message or "")[:2000],
        "engine": engine or (os.environ.get("REGISTER_ENGINE") or ""),
        "pid": os.getpid(),
    }
    if worker is not None:
        rec["worker"] = worker
    if exit_code is not None:
        rec["exit_code"] = exit_code
    if extra:
        for k, v in extra.items():
            if k in rec:
                continue
            try:
                json.dumps(v)
                rec[k] = v
            except Exception:
                rec[k] = str(v)[:500]
    line = json.dumps(rec, ensure_ascii=False) + "\n"
    with _lock:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(line)
            _trim_file(path, _FAIL_MAX_BYTES)
        except OSError:
            pass


def append_dashboard_note(text: str) -> None:
    """Mark a section in register-dashboard.log (spawn / early exit)."""
    path = register_dashboard_log_path()
    with _lock:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(text if text.endswith("\n") else text + "\n")
            _trim_file(path, _DASH_MAX_BYTES)
        except OSError:
            pass


def list_run_logs() -> list[dict[str, Any]]:
    """Metadata for panel download list."""
    specs = [
        {
            "id": "register_log",
            "name": "register-dashboard.log",
            "path": register_dashboard_log_path(),
            "desc": "注册进程完整 stdout/stderr（启动失败、Turnstile、worker fail）",
            "download": "/api/download?format=register_log",
        },
        {
            "id": "register_fail",
            "name": "register-fail.jsonl",
            "path": register_fail_log_path(),
            "desc": "结构化失败/退出事件（一行一条 JSON，便于排查秒退）",
            "download": "/api/download?format=register_fail",
        },
        {
            "id": "register_live",
            "name": "register-live.log",
            "path": register_live_log_path(),
            "desc": "实时运行日志（若存在）",
            "download": "/api/download?format=register_live",
        },
    ]
    out: list[dict[str, Any]] = []
    for s in specs:
        path: Path = s["path"]
        exists = path.is_file()
        size = 0
        mtime = ""
        if exists:
            try:
                st = path.stat()
                size = int(st.st_size)
                mtime = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(st.st_mtime))
            except OSError:
                exists = False
        out.append(
            {
                "id": s["id"],
                "name": s["name"],
                "desc": s["desc"],
                "exists": exists,
                "size": size,
                "updated_at": mtime,
                "download": s["download"],
            }
        )
    return out


def pack_run_logs_zip() -> Path:
    """Zip available run logs for one-click download."""
    root = logs_dir()
    latest = root / "register-logs.zip"
    tmp = latest.with_suffix(".zip.tmp")
    written = 0
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in (
            register_dashboard_log_path(),
            register_fail_log_path(),
            register_live_log_path(),
        ):
            if path.is_file() and path.stat().st_size > 0:
                try:
                    zf.write(path, arcname=path.name)
                    written += 1
                except OSError:
                    continue
        # also attach hybrid solver log if present
        for extra in (
            root / "turnstile-hybrid.log",
            PROJECT_ROOT / "logs" / "turnstile-solver" / "hybrid" / "solver.log",
            Path("/app/logs/turnstile-solver/hybrid/solver.log"),
        ):
            if extra.is_file() and extra.stat().st_size > 0:
                try:
                    zf.write(extra, arcname=f"turnstile/{extra.name}")
                    written += 1
                except OSError:
                    continue
        manifest = {
            "exported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "files": written,
            "note": "register-dashboard.log = full process log; register-fail.jsonl = structured fails",
        }
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    if written == 0:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        # create a tiny placeholder so download still works
        with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(
                "README.txt",
                "No register logs yet. Start register once; fails go to register-fail.jsonl.\n",
            )
    tmp.replace(latest)
    return latest


def tail_text(path: Path, *, max_bytes: int = 4000, max_lines: int = 40) -> str:
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    if len(data) > max_bytes:
        data = data[-max_bytes:]
    text = data.decode("utf-8", errors="replace").strip()
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return "\n".join(lines[-max_lines:])


def recent_fail_summary(*, limit: int = 8) -> list[dict[str, Any]]:
    path = register_fail_log_path()
    if not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
        if len(out) >= limit:
            break
    return out
