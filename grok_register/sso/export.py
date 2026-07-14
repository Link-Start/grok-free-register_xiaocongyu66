"""SSO-first export + batch convert to CPA/sub2api.

Pipeline:
  1) Registration only writes SSO artifacts (accounts.txt / grok.txt / auth-sessions)
  2) Batch convert pending SSO → CPA via Go inventory-worker (Chrome TLS + high concurrency)
  3) Real-time progress bar on CLI; optional --sso-file / --emails-file batch
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from grok_register.inventory.accounts import key_export_dir, scan_accounts
from grok_register.memory_hygiene import note_op_and_maybe_trim, trim_memory

PROJECT_ROOT = Path(__file__).resolve().parents[2]
JOB_PATH = PROJECT_ROOT / "logs" / "sso-to-cpa-job.json"

_job_lock = threading.Lock()
_job: dict[str, Any] = {
    "running": False,
    "started_at": 0,
    "finished_at": 0,
    "total": 0,
    "done": 0,
    "ok": 0,
    "fail": 0,
    "skipped": 0,
    "message": "",
    "error": "",
    "formats": [],
    "updated_at": 0,
}


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _env_bool(name: str, default: bool = False) -> bool:
    raw = _env(name)
    if not raw:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def sso_only_export_enabled() -> bool:
    if _env_bool("KEY_EXPORT_LEGACY_LIVE_OAUTH", False):
        return False
    return True


def export_formats_for_register() -> list[str]:
    return ["legacy"]


def convert_formats_default() -> list[str]:
    raw = _env("SSO_CONVERT_FORMATS", "cpa")
    parts = [p.strip().lower() for p in raw.replace(";", ",").split(",") if p.strip()]
    out = [p for p in parts if p in {"cpa", "sub2api"}]
    return out or ["cpa"]


def default_convert_workers() -> int:
    return max(1, min(64, _env_int("CONVERT_WORKERS", 16)))


def _persist_job() -> None:
    path = Path(_env("SSO_CONVERT_JOB_FILE") or str(JOB_PATH))
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = dict(_job)
        payload["updated_at"] = time.time()
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass


def job_status() -> dict[str, Any]:
    with _job_lock:
        out = dict(_job)
    out["state_file"] = str(JOB_PATH)
    return out


def _set_job(**kwargs) -> None:
    with _job_lock:
        _job.update(kwargs)
        _job["updated_at"] = time.time()
        _persist_job()


def append_sso_artifacts(
    email: str,
    password: str,
    sso: str,
    *,
    cookies: list | None = None,
    browser_fingerprint_id: str | None = None,
    root: Path | None = None,
) -> dict[str, str]:
    root = root or key_export_dir()
    root.mkdir(parents=True, exist_ok=True)
    written: dict[str, str] = {}
    email = (email or "").strip()
    password = (password or "").strip()
    sso = (sso or "").strip()
    if not email or not sso:
        return written

    accounts = root / "accounts.txt"
    with accounts.open("a", encoding="utf-8") as f:
        f.write(f"{email}:{password}:{sso}\n")
        f.flush()
    written["legacy"] = str(accounts)

    grok = root / "grok.txt"
    with grok.open("a", encoding="utf-8") as f:
        f.write(sso + "\n")
        f.flush()
    written["grok"] = str(grok)

    sess_cookies = cookies or [
        {
            "name": "sso",
            "value": sso,
            "domain": "accounts.x.ai",
            "path": "/",
            "secure": True,
            "httpOnly": True,
            "sameSite": "Lax",
        },
        {
            "name": "sso-rw",
            "value": sso,
            "domain": "accounts.x.ai",
            "path": "/",
            "secure": True,
            "httpOnly": True,
            "sameSite": "Lax",
        },
    ]
    doc = {
        "email": email,
        "cookies": sess_cookies,
        "browser_fingerprint_id": browser_fingerprint_id,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "source": "sso_export",
    }
    sessions = root / "auth-sessions.jsonl"
    with sessions.open("a", encoding="utf-8") as f:
        f.write(json.dumps(doc, ensure_ascii=False, separators=(",", ":")) + "\n")
        f.flush()
    written["auth_sessions"] = str(sessions)
    note_op_and_maybe_trim()
    return written


def list_pending_sso(root: Path | None = None, *, limit: int = 5000) -> list[dict[str, Any]]:
    root = root or key_export_dir()
    records = scan_accounts(root)
    out = []
    for r in records:
        if r.status in {"oauth_pending", "legacy_sso"} or (
            r.has_sso and "cpa" not in r.formats and "sub2api" not in r.formats
        ):
            out.append(r.to_dict())
        if len(out) >= limit:
            break
    return out


# ── progress bar ──────────────────────────────────────────────

class ProgressBar:
    """Simple TTY progress bar for SSO→CPA convert."""

    def __init__(self, stream=None):
        self.stream = stream or sys.stderr
        self.total = 0
        self.done = 0
        self.ok = 0
        self.fail = 0
        self.skip = 0
        self.t0 = time.time()
        self._last_email = ""
        self._last_err = ""
        self._tty = hasattr(self.stream, "isatty") and self.stream.isatty()

    def on_event(self, **ev):
        event = ev.get("event") or ""
        if event == "start":
            self.total = int(ev.get("total") or 0)
            self.t0 = time.time()
            w = ev.get("workers") or "?"
            self._line(f"[*] SSO→CPA start total={self.total} workers={w} tls=chrome_131")
            return
        if event == "enroll_start":
            self._line(
                f"[*] protocol enroll jobs={ev.get('jobs')} workers={ev.get('workers')}"
                f" proxies={ev.get('proxies', '?')} retry={ev.get('retry', 0)}"
            )
            return
        if event == "retry_queued":
            em = str(ev.get("email") or "")
            if len(em) > 32:
                em = em[:29] + "…"
            self._line(
                f"[↻] re-queue {em} attempt={ev.get('attempt')}/{ev.get('max')} "
                f"({str(ev.get('error') or '')[:50]})"
            )
            return
        if event in {"progress", ""} or "done" in ev:
            if "done" in ev:
                self.done = int(ev.get("done") or self.done)
            if "total" in ev and int(ev.get("total") or 0) > 0:
                self.total = int(ev["total"])
            if "ok" in ev:
                self.ok = int(ev["ok"])
            if "fail" in ev:
                self.fail = int(ev["fail"])
            if "skip" in ev:
                self.skip = int(ev["skip"])
            if ev.get("email"):
                self._last_email = str(ev["email"])
            if ev.get("error"):
                self._last_err = str(ev["error"])[:80]
            elif ev.get("ok_one"):
                self._last_err = ""
            self._render()
        if event == "done":
            self.ok = int(ev.get("ok") or self.ok)
            self.fail = int(ev.get("fail") or self.fail)
            self.skip = int(ev.get("skip") or self.skip)
            self.total = int(ev.get("total") or self.total)
            self.done = self.total or self.done
            self._render(final=True)

    def _render(self, final: bool = False):
        total = max(self.total, 1)
        done = min(self.done, total) if self.total else self.done
        pct = (100.0 * done / total) if self.total else 0.0
        elapsed = max(0.001, time.time() - self.t0)
        rate = done / elapsed
        eta = (total - done) / rate if rate > 0 and self.total and done < total else 0
        width = 28
        filled = int(width * done / total) if self.total else 0
        bar = "█" * filled + "░" * (width - filled)
        email = self._last_email
        if len(email) > 28:
            email = email[:25] + "…"
        tail = f" ok={self.ok} fail={self.fail}"
        if self._last_err and not final:
            tail += f" last_err={self._last_err[:40]}"
        line = (
            f"\r[{bar}] {done}/{self.total or '?'} {pct:5.1f}% "
            f"{rate:.2f}/s ETA {eta:.0f}s{tail} {email}"
        )
        if final:
            line = (
                f"\r[{bar}] {done}/{self.total or done} 100% "
                f"{rate:.2f}/s done · ok={self.ok} fail={self.fail} skip={self.skip} "
                f"({elapsed:.1f}s)"
            )
            self.stream.write(line + "\n")
        elif self._tty:
            self.stream.write(line)
        else:
            # non-TTY: print every 5 or on milestones
            if done == 1 or done == self.total or done % 5 == 0:
                self.stream.write(line.lstrip("\r") + "\n")
        try:
            self.stream.flush()
        except Exception:
            pass

    def _line(self, msg: str):
        if self._tty and self.done:
            self.stream.write("\n")
        self.stream.write(msg + "\n")
        try:
            self.stream.flush()
        except Exception:
            pass


def convert_sso_to_product(
    *,
    formats: Iterable[str] | None = None,
    only_pending: bool = True,
    limit: int = 500,
    workers: int | None = None,
    enroll: bool = True,
    rebuild: bool = True,
    progress_cb=None,
    show_progress: bool = False,
    sso_file: str = "",
    emails_file: str = "",
    email: str = "",
    retry: int | None = None,
    retry_delay_ms: int | None = None,
) -> dict[str, Any]:
    """Batch convert SSO → CPA/sub2api via Go concurrent protocol enroll.

    retry: extra re-queue attempts for failed accounts (default SSO_CONVERT_RETRY or 1).
    Failed jobs are pushed to the **end** of the queue and retried later.
    """
    root = key_export_dir()
    formats = list(formats) if formats is not None else convert_formats_default()
    formats = [f for f in formats if f in {"cpa", "sub2api"}]
    if not formats:
        return {"ok": False, "message": "formats must include cpa and/or sub2api", "results": []}

    workers = workers if workers is not None else default_convert_workers()
    workers = max(1, min(int(workers), 64))
    limit = max(1, min(int(limit or 500), 10000))
    if retry is None:
        retry = _env_int("SSO_CONVERT_RETRY", 1)
    retry = max(0, min(int(retry), 5))
    if retry_delay_ms is None:
        retry_delay_ms = _env_int("SSO_CONVERT_RETRY_DELAY_MS", 1500)
    retry_delay_ms = max(0, min(int(retry_delay_ms), 60000))
    t0 = time.time()
    trim_memory(force=True)

    from grok_register.polyglot import go_inventory_worker_bin, inventory_convert, PolyglotError

    if go_inventory_worker_bin() is None:
        return {
            "ok": False,
            "message": "inventory-worker 未构建；请 bash scripts/build-native.sh",
            "results": [],
            "engine": "missing",
        }

    bar = None
    cbs = []
    if progress_cb:
        cbs.append(progress_cb)
    if show_progress:
        bar = ProgressBar()
        cbs.append(bar.on_event)

    def _fanout(**ev):
        for cb in cbs:
            try:
                cb(**ev)
            except Exception:
                pass

    # Multi-IP: pass pool file + optional single; Go round-robins per account.
    proxy = _env("SSO_CONVERT_PROXY")  # may be comma-separated list
    proxy_file = (
        _env("SSO_CONVERT_PROXY_FILE")
        or _env("PROXY_POOL_FILE")
        or ""
    )
    # Default to project 代理.txt when no explicit file and file exists
    if not proxy_file:
        cand = PROJECT_ROOT / "代理.txt"
        if cand.is_file():
            proxy_file = str(cand)
    try:
        go_out = inventory_convert(
            root,
            formats=formats,
            pending=only_pending and not bool(sso_file),
            enroll=enroll,
            limit=limit,
            workers=workers,
            proxy=proxy,
            proxy_file=proxy_file,
            email=email or "",
            emails_file=emails_file or "",
            sso_file=sso_file or "",
            progress=bool(cbs),
            progress_cb=_fanout if cbs else None,
            retry=retry,
            retry_delay_ms=retry_delay_ms,
        )
    except PolyglotError as exc:
        trim_memory(force=True)
        return {"ok": False, "message": str(exc)[:300], "results": [], "engine": "go"}
    except Exception as exc:
        trim_memory(force=True)
        return {
            "ok": False,
            "message": f"protocol convert failed: {exc}"[:300],
            "results": [],
            "engine": "go",
        }

    trim_memory(force=True)
    ok = int(go_out.get("ok_n") or 0)
    fail = int(go_out.get("fail_n") or 0)
    skipped = int(go_out.get("skip_n") or 0)
    bundles = {}
    if rebuild:
        try:
            from grok_register.inventory.accounts import ensure_bundles

            bundles = ensure_bundles(rebuild=True)
        except Exception as exc:
            bundles = {"error": str(exc)[:200]}
    elapsed = round(time.time() - t0, 2)
    rate = round(ok / elapsed, 2) if elapsed > 0 and ok else 0
    return {
        "ok": fail == 0 and (ok + skipped) > 0,
        "message": (
            f"SSO→{','.join(formats)}（Go×{workers} Chrome TLS retry={retry}）："
            f"成功 {ok} · 失败 {fail} · 跳过 {skipped} · {elapsed}s"
            + (f" · ~{rate}/s" if rate else "")
        ),
        "ok_count": ok,
        "fail_count": fail,
        "skipped": skipped,
        "total": int(go_out.get("total") or 0),
        "formats": formats,
        "results": go_out.get("results") or [],
        "bundles": bundles,
        "elapsed_sec": elapsed,
        "engine": go_out.get("engine") or "go",
        "workers": workers,
        "retry": retry,
        "retry_delay_ms": retry_delay_ms,
        "protocol": "device_code+verify+approve",
        "tls": "chrome_131",
        "sso_file": sso_file or None,
        "emails_file": emails_file or None,
    }


def main(argv: list[str] | None = None) -> int:
    import argparse

    argv = list(sys.argv[1:] if argv is None else argv)
    ap = argparse.ArgumentParser(
        description="SSO export + batch convert to CPA (Go concurrent, progress bar)"
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_pending = sub.add_parser("pending", help="list oauth_pending SSO accounts")
    p_pending.add_argument("--limit", type=int, default=50)

    p_conv = sub.add_parser("convert", help="batch SSO → CPA (Go×N, live progress)")
    p_conv.add_argument("--formats", default="cpa", help="cpa,sub2api")
    p_conv.add_argument("--limit", type=int, default=200)
    p_conv.add_argument(
        "--workers",
        type=int,
        default=0,
        help="concurrent workers (default CONVERT_WORKERS or 16)",
    )
    p_conv.add_argument(
        "--sso-file",
        default="",
        help="batch SSO file: email:password:sso or email:sso per line",
    )
    p_conv.add_argument(
        "--emails-file",
        default="",
        help="only convert these emails (one per line; or email:… lines)",
    )
    p_conv.add_argument("--email", default="", help="convert a single email")
    p_conv.add_argument(
        "--proxy",
        default="",
        help="single proxy or comma-separated pool (http:// / socks5://)",
    )
    p_conv.add_argument(
        "--proxy-file",
        default="",
        help="proxy pool file, one URL per line (default: 代理.txt / PROXY_POOL_FILE)",
    )
    p_conv.add_argument(
        "--retry",
        type=int,
        default=-1,
        help="extra retries for failed accounts (re-queue at end; default 1, 0=off)",
    )
    p_conv.add_argument(
        "--retry-delay-ms",
        type=int,
        default=-1,
        help="delay before re-queued retry (default 1500)",
    )
    p_conv.add_argument("--no-enroll", action="store_true", help="OAuth file transform only")
    p_conv.add_argument("--no-progress", action="store_true", help="disable progress bar")
    p_conv.add_argument("--background", action="store_true")
    p_conv.add_argument(
        "--all-pending",
        action="store_true",
        help="also include legacy_sso status (default pending already does)",
    )

    sub.add_parser("status", help="show convert job status")

    args = ap.parse_args(argv)
    if args.cmd == "pending":
        rows = list_pending_sso(limit=args.limit)
        print(
            json.dumps(
                {"ok": True, "total": len(rows), "accounts": rows[: args.limit]},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.cmd == "status":
        print(json.dumps(job_status(), ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "convert":
        formats = [x.strip() for x in args.formats.split(",") if x.strip()]
        workers = args.workers if args.workers and args.workers > 0 else None
        if args.background:
            out = start_sso_to_cpa_job(
                formats=formats,
                only_pending=True,
                limit=args.limit,
                allow_enroll=not args.no_enroll,
                workers=workers,
                sso_file=args.sso_file or "",
                emails_file=args.emails_file or "",
            )
            print(json.dumps(out, ensure_ascii=False, indent=2))
            return 0 if out.get("ok") else 1
        # optional CLI proxy overrides env for this run
        if getattr(args, "proxy", None):
            os.environ["SSO_CONVERT_PROXY"] = args.proxy
        if getattr(args, "proxy_file", None):
            os.environ["SSO_CONVERT_PROXY_FILE"] = args.proxy_file
        retry = args.retry if args.retry is not None and args.retry >= 0 else None
        retry_delay = (
            args.retry_delay_ms
            if args.retry_delay_ms is not None and args.retry_delay_ms >= 0
            else None
        )
        out = convert_sso_to_product(
            formats=formats,
            only_pending=True,
            limit=args.limit,
            workers=workers,
            enroll=not args.no_enroll,
            rebuild=True,
            show_progress=not args.no_progress,
            sso_file=args.sso_file or "",
            emails_file=args.emails_file or "",
            email=args.email or "",
            retry=retry,
            retry_delay_ms=retry_delay,
        )
        print(
            json.dumps(
                {k: v for k, v in out.items() if k != "results"},
                ensure_ascii=False,
                indent=2,
            )
        )
        fails = [r for r in (out.get("results") or []) if not r.get("ok")]
        if fails:
            print(f"[!] first fails ({len(fails)}):", flush=True, file=sys.stderr)
            for r in fails[:8]:
                print(
                    f"    {r.get('email')}: {r.get('error')}",
                    flush=True,
                    file=sys.stderr,
                )
        return 0 if out.get("ok") else 1
    return 2


def start_sso_to_cpa_job(
    *,
    formats: Iterable[str] | None = None,
    only_pending: bool = True,
    limit: int = 500,
    allow_enroll: bool = True,
    workers: int | None = None,
    sso_file: str = "",
    emails_file: str = "",
) -> dict[str, Any]:
    with _job_lock:
        if _job.get("running"):
            return {"ok": False, "message": "SSO→CPA 任务已在进行", "job": job_status()}

    formats = list(formats) if formats is not None else convert_formats_default()
    w = workers if workers is not None else default_convert_workers()

    def worker():
        _set_job(
            running=True,
            started_at=time.time(),
            finished_at=0,
            formats=formats,
            total=0,
            done=0,
            ok=0,
            fail=0,
            skipped=0,
            message=f"starting go workers={w}…",
            error="",
        )

        def on_progress(**kwargs):
            if kwargs.get("event") in {"progress", "done", "start"} or "done" in kwargs:
                _set_job(
                    total=kwargs.get("total") or _job.get("total") or 0,
                    done=kwargs.get("done") or 0,
                    ok=kwargs.get("ok") or 0,
                    fail=kwargs.get("fail") or 0,
                    skipped=kwargs.get("skip") or kwargs.get("skipped") or 0,
                    message=kwargs.get("email")
                    or kwargs.get("event")
                    or "running",
                )

        try:
            out = convert_sso_to_product(
                formats=formats,
                only_pending=only_pending,
                limit=limit,
                workers=w,
                enroll=allow_enroll,
                rebuild=True,
                progress_cb=on_progress,
                show_progress=False,
                sso_file=sso_file,
                emails_file=emails_file,
            )
            _set_job(
                running=False,
                finished_at=time.time(),
                ok=out.get("ok_count") or 0,
                fail=out.get("fail_count") or 0,
                skipped=out.get("skipped") or 0,
                total=out.get("total") or 0,
                done=(out.get("ok_count") or 0)
                + (out.get("fail_count") or 0)
                + (out.get("skipped") or 0),
                message=out.get("message") or "done",
                error="" if out.get("ok") else (out.get("message") or "failed"),
            )
        except Exception as exc:
            _set_job(
                running=False,
                finished_at=time.time(),
                message="failed",
                error=str(exc)[:300],
            )
            trim_memory(force=True)

    threading.Thread(target=worker, name="sso-to-cpa", daemon=True).start()
    return {"ok": True, "message": f"SSO→CPA 已启动（Go workers={w}）", "job": job_status()}


if __name__ == "__main__":
    raise SystemExit(main())
