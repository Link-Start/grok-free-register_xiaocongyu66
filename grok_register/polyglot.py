"""
Mandatory multi-language stack gate: Python + Go + Rust.

Startup and control-plane refuse to run unless all native binaries exist.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class PolyglotError(RuntimeError):
    """Raised when the required language stack is incomplete."""


def _env_path(name: str) -> Path | None:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return None
    return Path(raw).expanduser()


def python_bin() -> Path:
    override = _env_path("POLYGLOT_PY")
    if override and override.is_file():
        return override
    venv = PROJECT_ROOT / ".venv" / "bin" / "python"
    if venv.is_file():
        return venv
    return Path(sys.executable)


def go_proxy_worker_bin() -> Path | None:
    for cand in (
        _env_path("PROXY_WORKER_BIN"),
        _env_path("POLYGLOT_GO_PROXY"),
        PROJECT_ROOT / "native" / "proxy-worker" / "proxy-worker",
    ):
        if cand and cand.is_file() and os.access(cand, os.X_OK):
            return cand
    return None


def go_register_worker_bin() -> Path | None:
    for cand in (
        _env_path("REGISTER_WORKER_BIN"),
        _env_path("POLYGLOT_GO_REGISTER"),
        PROJECT_ROOT / "native" / "register-worker" / "register-worker",
    ):
        if cand and cand.is_file() and os.access(cand, os.X_OK):
            return cand
    return None


def rust_inventory_worker_bin() -> Path | None:
    for cand in (
        _env_path("INVENTORY_WORKER_BIN"),
        _env_path("POLYGLOT_RUST_INVENTORY"),
        PROJECT_ROOT / "native" / "inventory-worker" / "inventory-worker",
        PROJECT_ROOT / "native" / "inventory-worker" / "target" / "release" / "inventory-worker",
    ):
        if cand and cand.is_file() and os.access(cand, os.X_OK):
            return cand
    return None


def stack_status() -> dict[str, Any]:
    py = python_bin()
    go_proxy = go_proxy_worker_bin()
    go_reg = go_register_worker_bin()
    rust = rust_inventory_worker_bin()
    components = {
        "python": {
            "ok": py.is_file(),
            "path": str(py),
            "role": "orchestration · browser · control plane",
        },
        "go_proxy_worker": {
            "ok": go_proxy is not None,
            "path": str(go_proxy) if go_proxy else "",
            "role": "high-concurrency proxy health checks",
        },
        "go_register_worker": {
            "ok": go_reg is not None,
            "path": str(go_reg) if go_reg else "",
            "role": "HTTP concurrent registration path",
        },
        "rust_inventory_worker": {
            "ok": rust is not None,
            "path": str(rust) if rust else "",
            "role": "account inventory scan / CPA·sub2api bundles",
        },
    }
    ok = all(c["ok"] for c in components.values())
    missing = [name for name, c in components.items() if not c["ok"]]
    return {
        "ok": ok,
        "required": ["python", "go", "rust"],
        "components": components,
        "missing": missing,
        "hint": (
            "install go + rustc/cargo, then: bash setup.sh  "
            "or bash scripts/build-native.sh"
        ),
    }


def require_polyglot_stack(*, hard: bool | None = None) -> dict[str, Any]:
    """
    Enforce multi-language stack.

    hard defaults to True unless POLYGLOT_REQUIRED=0 (tests only).
    """
    if hard is None:
        hard = (os.environ.get("POLYGLOT_REQUIRED") or "1").strip().lower() not in {
            "0",
            "false",
            "no",
            "off",
        }
    status = stack_status()
    if status["ok"] or not hard:
        return status
    lines = [
        "polyglot stack incomplete — project requires Python + Go + Rust",
        f"missing: {', '.join(status['missing'])}",
        f"fix: {status['hint']}",
    ]
    for name, comp in status["components"].items():
        mark = "OK" if comp["ok"] else "MISSING"
        lines.append(f"  [{mark}] {name}: {comp['path'] or '(not found)'} — {comp['role']}")
    raise PolyglotError("\n".join(lines))


def run_rust_inventory(
    *args: str,
    keys_dir: str | Path | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    binary = rust_inventory_worker_bin()
    if binary is None:
        raise PolyglotError("rust inventory-worker not found; run bash scripts/build-native.sh")
    cmd = [str(binary), *args]
    if keys_dir is not None and "--keys-dir" not in args:
        cmd.extend(["--keys-dir", str(keys_dir)])
    return subprocess.run(
        cmd,
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        check=check,
    )


def rust_scan_accounts(keys_dir: str | Path | None = None) -> dict[str, Any]:
    proc = run_rust_inventory("scan", "--json", keys_dir=keys_dir, check=False)
    if proc.returncode != 0:
        raise PolyglotError(
            f"inventory-worker scan failed ({proc.returncode}): {proc.stderr or proc.stdout}"
        )
    return json.loads(proc.stdout)


def rust_rebuild_bundles(keys_dir: str | Path | None = None) -> dict[str, Any]:
    proc = run_rust_inventory("rebuild", keys_dir=keys_dir, check=False)
    if proc.returncode != 0:
        raise PolyglotError(
            f"inventory-worker rebuild failed ({proc.returncode}): {proc.stderr or proc.stdout}"
        )
    return json.loads(proc.stdout)


def print_stack_banner() -> None:
    status = stack_status()
    print("[*] polyglot stack: Python + Go + Rust", flush=True)
    for name, comp in status["components"].items():
        mark = "✓" if comp["ok"] else "✗"
        short = Path(comp["path"]).name if comp["path"] else "—"
        print(f"    [{mark}] {name}: {short}", flush=True)
    if not status["ok"]:
        print(f"[!] missing: {', '.join(status['missing'])}", flush=True)
        print(f"    {status['hint']}", flush=True)
