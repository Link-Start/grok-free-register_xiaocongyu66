"""Protocol auth service: SSO → CPA via Go inventory-worker (grok2api sso_build).

Entry: ``bash auth-service.sh``
Flow:
  1) Read SSO from local keys/ (or optional --sso-file batch)
  2) Concurrent Go Chrome-TLS device_code + verify + approve + token
  3) Token response includes refresh_token (scope offline_access)
  4) Write keys/cpa/xai-*.json (+ optional mirror to authenticated/)
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import sys
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = _env(name)
    if not raw:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv(PROJECT_ROOT / ".env")
    except Exception:
        pass


def mirror_cpa_to_authenticated(cpa_dir: Path, dest: Path) -> int:
    """Copy new xai-*.json into auth-service authenticated/ for take-compatibility."""
    dest.mkdir(mode=0o700, parents=True, exist_ok=True)
    n = 0
    if not cpa_dir.is_dir():
        return 0
    for src in cpa_dir.glob("xai-*.json"):
        target = dest / src.name
        try:
            if target.is_file() and target.stat().st_size == src.stat().st_size:
                # same size — skip if mtime not newer
                if target.stat().st_mtime >= src.stat().st_mtime - 1:
                    continue
            shutil.copy2(src, target)
            try:
                os.chmod(target, 0o600)
            except OSError:
                pass
            n += 1
        except OSError:
            continue
    return n


def count_pending(keys: Path) -> int:
    try:
        from grok_register.sso.export import list_pending_sso

        return len(list_pending_sso(keys, limit=50000))
    except Exception:
        return -1


def count_cpa(keys: Path) -> int:
    d = keys / "cpa"
    if not d.is_dir():
        return 0
    return sum(1 for _ in d.glob("xai-*.json"))


def run_once(
    *,
    limit: int,
    workers: int,
    sso_file: str,
    emails_file: str,
    show_progress: bool,
    mirror_dir: Path | None,
) -> dict[str, Any]:
    from grok_register.sso.export import convert_sso_to_product

    out = convert_sso_to_product(
        formats=["cpa"],
        only_pending=True,
        limit=limit,
        workers=workers,
        enroll=True,
        rebuild=True,
        show_progress=show_progress,
        sso_file=sso_file or "",
        emails_file=emails_file or "",
    )
    if mirror_dir is not None:
        keys = Path(_env("KEY_EXPORT_DIR") or "keys")
        if not keys.is_absolute():
            keys = PROJECT_ROOT / keys
        n = mirror_cpa_to_authenticated(keys / "cpa", mirror_dir)
        out["mirrored"] = n
    return out


def run_watch(
    *,
    limit: int,
    workers: int,
    interval: int,
    sso_file: str,
    emails_file: str,
    show_progress: bool,
    mirror_dir: Path | None,
    once: bool,
) -> int:
    stop = False

    def _sig(*_a):
        nonlocal stop
        stop = True
        print("\n[■] 收到停止信号，完成本轮后退出…", flush=True)

    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)

    keys = Path(_env("KEY_EXPORT_DIR") or "keys")
    if not keys.is_absolute():
        keys = PROJECT_ROOT / keys

    print(
        f"[✓] 协议认证服务 (Go sso_build / Chrome TLS)\n"
        f"    keys={keys}\n"
        f"    workers={workers} batch_limit={limit} interval={interval}s\n"
        f"    sso_file={sso_file or '(keys/sso.txt email:sso)'}\n"
        f"    mirror={mirror_dir or '(off)'}\n"
        f"    命令: 一轮转换后等待；Ctrl-C 停止",
        flush=True,
    )
    # ensure inventory-worker
    try:
        from grok_register.polyglot import go_inventory_worker_bin

        if go_inventory_worker_bin() is None:
            print(
                "[✗] inventory-worker 未构建。请: bash scripts/build-native.sh",
                file=sys.stderr,
                flush=True,
            )
            return 2
    except Exception as exc:
        print(f"[✗] polyglot: {exc}", file=sys.stderr, flush=True)
        return 2

    rounds = 0
    total_ok = 0
    total_fail = 0
    while not stop:
        rounds += 1
        pending = count_pending(keys)
        cpa_n = count_cpa(keys)
        print(
            f"\n[↻] 第 {rounds} 轮 | pending≈{pending} | cpa={cpa_n} | "
            f"累计 ok={total_ok} fail={total_fail}",
            flush=True,
        )
        if pending == 0 and not sso_file:
            if once:
                print("[•] 无 pending SSO，退出", flush=True)
                return 0
            print(f"[•] 无 pending，{interval}s 后再扫…", flush=True)
            for _ in range(interval):
                if stop:
                    break
                time.sleep(1)
            continue

        out = run_once(
            limit=limit,
            workers=workers,
            sso_file=sso_file,
            emails_file=emails_file,
            show_progress=show_progress,
            mirror_dir=mirror_dir,
        )
        ok = int(out.get("ok_count") or 0)
        fail = int(out.get("fail_count") or 0)
        total_ok += ok
        total_fail += fail
        print(f"[*] {out.get('message')}", flush=True)
        if out.get("mirrored"):
            print(f"[*] 已镜像 {out['mirrored']} 个 CPA → {mirror_dir}", flush=True)

        if once or sso_file:
            # single batch file or --once
            return 0 if fail == 0 and ok >= 0 else 1

        if stop:
            break
        # if nothing left or all failed, still wait (SSO may expire — user re-registers)
        wait = interval
        if ok == 0 and fail > 0:
            wait = max(interval, 30)
            print(f"[!] 本轮无成功，{wait}s 后重试（旧 SSO 可能已失效）", flush=True)
        for _ in range(wait):
            if stop:
                break
            time.sleep(1)

    print(
        f"[■] 协议认证服务已停止 | 轮次 {rounds} | 累计 ok={total_ok} fail={total_fail}",
        flush=True,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    _load_dotenv()
    argv = list(sys.argv[1:] if argv is None else argv)
    ap = argparse.ArgumentParser(
        description="Protocol auth service: SSO→CPA (Go concurrent, not Playwright)"
    )
    ap.add_argument(
        "--once",
        action="store_true",
        help="run one convert batch then exit",
    )
    ap.add_argument(
        "--limit",
        type=int,
        default=_env_int("AUTH_PROTOCOL_LIMIT", _env_int("SSO_CONVERT_LIMIT", 200)),
        help="max accounts per round (default 200)",
    )
    ap.add_argument(
        "--workers",
        type=int,
        default=_env_int("AUTH_PROTOCOL_WORKERS", _env_int("CONVERT_WORKERS", 16)),
        help="Go concurrent workers (default 16)",
    )
    ap.add_argument(
        "--interval",
        type=int,
        default=_env_int("AUTH_PROTOCOL_INTERVAL_SEC", 30),
        help="seconds between rounds in watch mode (default 30)",
    )
    ap.add_argument(
        "--sso-file",
        default=_env("AUTH_PROTOCOL_SSO_FILE"),
        help="batch SSO file email:password:sso",
    )
    ap.add_argument(
        "--emails-file",
        default=_env("AUTH_PROTOCOL_EMAILS_FILE"),
        help="only these emails",
    )
    ap.add_argument(
        "--no-progress",
        action="store_true",
        help="disable progress bar",
    )
    ap.add_argument(
        "--mirror-auth-dir",
        default=_env("AUTH_PROTOCOL_MIRROR_DIR"),
        help="also copy CPA into this dir (default: ~/Downloads/.../authenticated if exists)",
    )
    ap.add_argument(
        "--no-mirror",
        action="store_true",
        help="do not mirror into authenticated/",
    )
    ap.add_argument(
        "--proxy",
        default=_env("SSO_CONVERT_PROXY"),
        help="proxy or comma-separated pool for multi-IP enroll",
    )
    ap.add_argument(
        "--proxy-file",
        default=_env("SSO_CONVERT_PROXY_FILE") or _env("PROXY_POOL_FILE"),
        help="proxy pool file (one URL per line); default 代理.txt",
    )
    ap.add_argument(
        "--debug",
        action="store_true",
        help="compat flag (same as verbose progress)",
    )
    args = ap.parse_args(argv)

    if args.proxy:
        os.environ["SSO_CONVERT_PROXY"] = args.proxy
    if args.proxy_file:
        os.environ["SSO_CONVERT_PROXY_FILE"] = args.proxy_file

    mirror: Path | None = None
    if not args.no_mirror:
        raw = (args.mirror_auth_dir or "").strip()
        if raw:
            mirror = Path(raw).expanduser()
        else:
            # Compat with old auth-service layout
            default = Path.home() / "Downloads" / "grok-free-register-auth" / "authenticated"
            # Always prepare mirror so take/inventory users keep working
            mirror = default
            if _env_bool("AUTH_PROTOCOL_MIRROR_KEYS_CPA", True):
                # also always write keys/cpa (convert does that); mirror is extra
                pass

    return run_watch(
        limit=max(1, min(int(args.limit), 10000)),
        workers=max(1, min(int(args.workers), 64)),
        interval=max(5, int(args.interval)),
        sso_file=args.sso_file or "",
        emails_file=args.emails_file or "",
        show_progress=not args.no_progress,
        mirror_dir=mirror,
        once=bool(args.once or args.sso_file),
    )


if __name__ == "__main__":
    raise SystemExit(main())
