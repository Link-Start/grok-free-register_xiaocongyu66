"""
Mandatory multi-language stack gate: Python + Go (+ hybrid Rust/C++).

Inventory scan / rebuild / protocol convert are owned by the Go inventory-worker.
Startup and control-plane refuse to run unless required native binaries exist.
"""
from __future__ import annotations

import json
import os
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


def go_inventory_worker_bin() -> Path | None:
    """Go inventory-worker (scan / rebuild / convert). Legacy Rust path still accepted."""
    for cand in (
        _env_path("INVENTORY_WORKER_BIN"),
        _env_path("POLYGLOT_GO_INVENTORY"),
        _env_path("POLYGLOT_RUST_INVENTORY"),  # back-compat
        PROJECT_ROOT / "native" / "inventory-worker" / "inventory-worker",
        PROJECT_ROOT / "native" / "inventory-worker" / "target" / "release" / "inventory-worker",
    ):
        if cand and cand.is_file() and os.access(cand, os.X_OK):
            return cand
    return None


# Back-compat aliases used by older call sites / tests
def rust_inventory_worker_bin() -> Path | None:
    return go_inventory_worker_bin()


def stack_status() -> dict[str, Any]:
    py = python_bin()
    go_proxy = go_proxy_worker_bin()
    go_reg = go_register_worker_bin()
    inv = go_inventory_worker_bin()
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
        "go_inventory_worker": {
            "ok": inv is not None,
            "path": str(inv) if inv else "",
            "role": "account inventory scan / CPA·sub2api / protocol convert",
        },
        # alias for older status consumers
        "rust_inventory_worker": {
            "ok": inv is not None,
            "path": str(inv) if inv else "",
            "role": "alias of go_inventory_worker",
        },
    }
    core_keys = ("python", "go_proxy_worker", "go_register_worker", "go_inventory_worker")
    ok = all(components[k]["ok"] for k in core_keys)
    missing = [name for name in core_keys if not components[name]["ok"]]
    return {
        "ok": ok,
        "required": ["python", "go"],
        "components": components,
        "missing": missing,
        "hint": (
            "install go (+ rustc/cargo for hybrid watchdog), then: bash setup.sh  "
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
        "polyglot stack incomplete — project requires Python + Go (inventory-worker)",
        f"missing: {', '.join(status['missing'])}",
        f"fix: {status['hint']}",
    ]
    for name, comp in status["components"].items():
        if name == "rust_inventory_worker":
            continue
        mark = "OK" if comp["ok"] else "MISSING"
        lines.append(f"  [{mark}] {name}: {comp['path'] or '(not found)'} — {comp['role']}")
    raise PolyglotError("\n".join(lines))


def run_inventory(
    *args: str,
    keys_dir: str | Path | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    binary = go_inventory_worker_bin()
    if binary is None:
        raise PolyglotError("inventory-worker not found; run bash scripts/build-native.sh")
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


# Back-compat
def run_rust_inventory(
    *args: str,
    keys_dir: str | Path | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return run_inventory(*args, keys_dir=keys_dir, check=check)


def inventory_scan_accounts(keys_dir: str | Path | None = None) -> dict[str, Any]:
    proc = run_inventory("scan", "--json", keys_dir=keys_dir, check=False)
    if proc.returncode != 0:
        raise PolyglotError(
            f"inventory-worker scan failed ({proc.returncode}): {proc.stderr or proc.stdout}"
        )
    return json.loads(proc.stdout)


def inventory_rebuild_bundles(keys_dir: str | Path | None = None) -> dict[str, Any]:
    proc = run_inventory("rebuild", keys_dir=keys_dir, check=False)
    if proc.returncode != 0:
        raise PolyglotError(
            f"inventory-worker rebuild failed ({proc.returncode}): {proc.stderr or proc.stdout}"
        )
    return json.loads(proc.stdout)


def inventory_convert(
    keys_dir: str | Path | None = None,
    *,
    formats: list[str] | None = None,
    pending: bool = False,
    enroll: bool = False,
    limit: int = 500,
    workers: int = 16,
    proxy: str = "",
    proxy_file: str = "",
    email: str = "",
    emails_file: str = "",
    sso_file: str = "",
    progress: bool = False,
    progress_cb=None,
    retry: int = 1,
    retry_delay_ms: int = 1500,
) -> dict[str, Any]:
    """Run Go protocol/file convert. formats default cpa,sub2api.

    When progress=True (or progress_cb set), streams PROGRESS\\tjson from stderr
    and invokes progress_cb(**event) for real-time UI.

    proxy / proxy_file: multi-IP pool (round-robin per account inside Go worker).
    retry: extra attempts for failed enroll (re-queued at end of queue).
    """
    fmt = ",".join(formats or ["cpa", "sub2api"])
    args = ["convert", "--formats", fmt, "--limit", str(limit), "--workers", str(workers)]
    if pending:
        args.append("--pending")
    if enroll:
        args.append("--enroll")
    if proxy:
        args.extend(["--proxy", proxy])
    if proxy_file:
        args.extend(["--proxy-file", str(proxy_file)])
    if email:
        args.extend(["--email", email])
    if emails_file:
        args.extend(["--emails-file", str(emails_file)])
    if sso_file:
        args.extend(["--sso-file", str(sso_file)])
    if retry is not None and int(retry) >= 0:
        args.extend(["--retry", str(int(retry))])
    if retry_delay_ms is not None and int(retry_delay_ms) >= 0:
        args.extend(["--retry-delay-ms", str(int(retry_delay_ms))])
    want_progress = bool(progress or progress_cb)
    if want_progress:
        args.append("--progress")

    binary = go_inventory_worker_bin()
    if binary is None:
        raise PolyglotError("inventory-worker not found; run bash scripts/build-native.sh")
    cmd = [str(binary), *args]
    if keys_dir is not None:
        cmd.extend(["--keys-dir", str(keys_dir)])

    if not want_progress:
        proc = subprocess.run(
            cmd,
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        if not (proc.stdout or "").strip():
            raise PolyglotError(
                f"inventory-worker convert failed ({proc.returncode}): {proc.stderr or '(no output)'}"
            )
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise PolyglotError(
                f"inventory-worker convert bad JSON: {proc.stdout[:300]!r} / {proc.stderr[:200]!r}"
            ) from exc
        if proc.returncode != 0 and not data.get("results"):
            raise PolyglotError(
                f"inventory-worker convert failed ({proc.returncode}): {proc.stderr or proc.stdout}"
            )
        return data

    # Stream stderr for PROGRESS lines; collect stdout JSON at end.
    proc = subprocess.Popen(
        cmd,
        cwd=str(PROJECT_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    stderr_lines: list[str] = []

    def _read_stderr():
        assert proc.stderr is not None
        for line in proc.stderr:
            stderr_lines.append(line)
            if not line.startswith("PROGRESS\t"):
                # pass through non-progress diagnostics
                try:
                    sys.stderr.write(line)
                    sys.stderr.flush()
                except Exception:
                    pass
                continue
            raw = line[len("PROGRESS\t") :].strip()
            try:
                ev = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if progress_cb:
                try:
                    progress_cb(**ev)
                except Exception:
                    pass

    import threading

    t = threading.Thread(target=_read_stderr, name="inv-progress", daemon=True)
    t.start()
    assert proc.stdout is not None
    stdout = proc.stdout.read()
    code = proc.wait()
    t.join(timeout=5)
    if not (stdout or "").strip():
        err = "".join(stderr_lines)[-800:]
        raise PolyglotError(f"inventory-worker convert failed ({code}): {err or '(no output)'}")
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise PolyglotError(
            f"inventory-worker convert bad JSON: {stdout[:300]!r}"
        ) from exc
    if code != 0 and not data.get("results"):
        raise PolyglotError(
            f"inventory-worker convert failed ({code}): {''.join(stderr_lines)[-400:]}"
        )
    return data


# Back-compat names
def rust_scan_accounts(keys_dir: str | Path | None = None) -> dict[str, Any]:
    return inventory_scan_accounts(keys_dir)


def rust_rebuild_bundles(keys_dir: str | Path | None = None) -> dict[str, Any]:
    return inventory_rebuild_bundles(keys_dir)


def print_stack_banner() -> None:
    status = stack_status()
    print("[*] polyglot stack: Python + Go (inventory) + hybrid", flush=True)
    for name, comp in status["components"].items():
        if name == "rust_inventory_worker":
            continue
        mark = "✓" if comp["ok"] else "✗"
        short = Path(comp["path"]).name if comp["path"] else "—"
        print(f"    [{mark}] {name}: {short}", flush=True)
    if not status["ok"]:
        print(f"[!] missing: {', '.join(status['missing'])}", flush=True)
        print(f"    {status['hint']}", flush=True)
