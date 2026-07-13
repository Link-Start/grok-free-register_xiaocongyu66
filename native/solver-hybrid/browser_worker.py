#!/usr/bin/env python3
"""
Hybrid Turnstile browser worker.

IPC: line-oriented JSON on stdin/stdout with solver-gateway (Go).

Solve strategy (critical):
  Reuse grok_register.register's proven local Turnstile path
  (cloakbrowser + playwright + proxy pool + inject + mouse click).
  This is the same code that already produces tokens in production.

Memory:
  - close browser after N solves / RSS soft-hard limits
  - gc.collect + malloc_trim on recycle
"""
from __future__ import annotations

import argparse
import asyncio
import gc
import json
import os
import resource
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Optional

os.environ.setdefault("PYTHONMALLOC", "malloc")
# Ensure project root imports
_PROJECT = Path(__file__).resolve().parents[2]
if str(_PROJECT) not in sys.path:
    sys.path.insert(0, str(_PROJECT))


def log(msg: str) -> None:
    sys.stderr.write(f"[browser-worker] {msg}\n")
    sys.stderr.flush()


def rss_mb() -> float:
    try:
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
    except Exception:
        return 0.0


def malloc_trim() -> None:
    try:
        import ctypes

        libc = ctypes.CDLL("libc.so.6")
        libc.malloc_trim(0)
    except Exception:
        pass
    try:
        gc.collect(2)
    except Exception:
        pass


def read_cmd() -> Optional[dict[str, Any]]:
    line = sys.stdin.readline()
    if not line:
        return None
    line = line.strip()
    if not line:
        return {"cmd": "ping"}
    try:
        return json.loads(line)
    except json.JSONDecodeError as exc:
        return {"cmd": "error", "error": f"bad json: {exc}"}


def write_resp(obj: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


class BrowserWorker:
    def __init__(
        self,
        *,
        worker_id: int,
        soft_mb: int,
        hard_mb: int,
        max_solves: int,
        timeout: float,
        concurrency: int = 1,
    ):
        self.worker_id = worker_id
        self.soft_mb = soft_mb
        self.hard_mb = hard_mb
        self.max_solves = max_solves
        self.timeout = timeout
        self.concurrency = max(1, int(concurrency))
        self.solves = 0
        self.browser = None
        self.playwright = None
        self._sem = asyncio.Semaphore(self.concurrency)
        self._browser_lock = asyncio.Lock()
        self._register = None

    def _import_register(self):
        if self._register is not None:
            return self._register
        # Force local solve path inside register module (not hybrid API recursion)
        os.environ["TURNSTILE_SOLVER"] = "local"
        import grok_register.register as reg

        self._register = reg
        return reg

    async def ensure_browser(self) -> None:
        async with self._browser_lock:
            if self.browser is not None:
                try:
                    if self.browser.is_connected():
                        return
                except Exception:
                    pass
                await self._close_browser_unlocked()

            reg = self._import_register()
            from playwright.async_api import async_playwright

            self.playwright = await async_playwright().start()
            exe = (
                (os.environ.get("SOLVER_CHROME_PATH") or os.environ.get("CHROME_PATH") or "")
                .strip()
                or reg.find_chrome()
            )
            self.browser = await self.playwright.chromium.launch(
                executable_path=exe,
                headless=True,
            )
            log(f"id={self.worker_id} browser launched exe={exe} rss={rss_mb():.1f}MB")

    async def _close_browser_unlocked(self) -> None:
        b, self.browser = self.browser, None
        if b is not None:
            try:
                await b.close()
            except Exception:
                pass
        p, self.playwright = self.playwright, None
        if p is not None:
            try:
                await p.stop()
            except Exception:
                pass
        malloc_trim()
        log(f"id={self.worker_id} browser closed rss={rss_mb():.1f}MB")

    async def recycle(self) -> None:
        async with self._browser_lock:
            await self._close_browser_unlocked()
        self.solves = 0
        malloc_trim()

    def _need_recycle(self) -> bool:
        mb = rss_mb()
        if self.hard_mb > 0 and mb >= self.hard_mb:
            return True
        if self.soft_mb > 0 and mb >= self.soft_mb:
            return True
        if self.max_solves > 0 and self.solves >= self.max_solves:
            return True
        return False

    async def solve(
        self,
        *,
        job_id: str,
        url: str,
        sitekey: str,
        action: str = "",
        cdata: str = "",
        proxy: str = "",
    ) -> dict[str, Any]:
        t0 = time.time()
        recycled = False
        async with self._sem:
            try:
                if self._need_recycle():
                    await self.recycle()
                    recycled = True
                await self.ensure_browser()
                reg = self._import_register()
                # Ensure sitekey is set for inject path
                if sitekey:
                    reg.SITE_KEY = sitekey
                if not reg.SITE_KEY:
                    reg.SITE_KEY = "0x4AAAAAAAhr9JGVDZbrZOo0"

                # Use the exact production solver
                token, trace = await asyncio.wait_for(
                    reg.solve_one_turnstile_with_trace(self.browser),
                    timeout=self.timeout,
                )
                self.solves += 1
                elapsed = time.time() - t0
                if not token or len(str(token)) <= 10:
                    return {
                        "ok": False,
                        "id": job_id,
                        "error": "CAPTCHA_FAIL",
                        "elapsed_sec": round(elapsed, 3),
                        "rss_mb": round(rss_mb(), 2),
                        "recycled": recycled,
                        "trace": {
                            k: trace.get(k)
                            for k in (
                                "goto_s",
                                "inject_s",
                                "wait_s",
                                "visible_frame",
                                "reused",
                            )
                        }
                        if isinstance(trace, dict)
                        else {},
                    }
                log(
                    f"id={self.worker_id} solved in {elapsed:.1f}s "
                    f"token={str(token)[:12]}... wait={trace.get('wait_s') if isinstance(trace, dict) else '?'}"
                )
                return {
                    "ok": True,
                    "id": job_id,
                    "value": token,
                    "elapsed_sec": round(elapsed, 3),
                    "rss_mb": round(rss_mb(), 2),
                    "recycled": recycled,
                }
            except asyncio.TimeoutError:
                return {
                    "ok": False,
                    "id": job_id,
                    "error": "timeout",
                    "elapsed_sec": round(time.time() - t0, 3),
                    "rss_mb": round(rss_mb(), 2),
                    "recycled": recycled,
                }
            except Exception as exc:
                log(f"id={self.worker_id} solve exception: {exc}")
                return {
                    "ok": False,
                    "id": job_id,
                    "error": str(exc)[:400],
                    "elapsed_sec": round(time.time() - t0, 3),
                    "rss_mb": round(rss_mb(), 2),
                    "recycled": recycled,
                }
            finally:
                if self._need_recycle():
                    try:
                        await self.recycle()
                    except Exception:
                        pass
                else:
                    gc.collect()

    async def prefetch(self) -> dict[str, Any]:
        t0 = time.time()
        try:
            await self.ensure_browser()
            return {
                "ok": True,
                "cmd": "prefetch",
                "elapsed_sec": round(time.time() - t0, 3),
                "rss_mb": round(rss_mb(), 2),
            }
        except Exception as exc:
            return {
                "ok": False,
                "cmd": "prefetch",
                "error": str(exc)[:300],
                "elapsed_sec": round(time.time() - t0, 3),
                "rss_mb": round(rss_mb(), 2),
            }

    async def shutdown(self) -> None:
        await self.recycle()


async def amain(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Hybrid Turnstile browser worker (register-backed)")
    p.add_argument("--worker-id", type=int, default=1)
    p.add_argument("--browser", default="chromium")  # accepted, ignored (uses register chrome)
    p.add_argument("--headless", action="store_true", default=False)
    p.add_argument("--soft-mb", type=int, default=700)
    p.add_argument("--hard-mb", type=int, default=1100)
    p.add_argument("--max-solves", type=int, default=8)
    p.add_argument("--concurrency", type=int, default=1)
    p.add_argument("--prefetch", action="store_true", default=False)
    p.add_argument("--proxy-file", default="")
    p.add_argument(
        "--timeout",
        type=float,
        default=float(os.environ.get("SOLVER_WORKER_TIMEOUT") or "90"),
    )
    args = p.parse_args(argv)

    worker = BrowserWorker(
        worker_id=args.worker_id,
        soft_mb=args.soft_mb,
        hard_mb=args.hard_mb,
        max_solves=args.max_solves,
        timeout=args.timeout,
        concurrency=args.concurrency,
    )
    log(
        f"ready id={args.worker_id} soft={args.soft_mb} hard={args.hard_mb} "
        f"max_solves={args.max_solves} conc={args.concurrency} "
        f"backend=register.local py={sys.executable}"
    )

    loop = asyncio.get_running_loop()
    while True:
        cmd = await loop.run_in_executor(None, read_cmd)
        if cmd is None:
            break
        name = str(cmd.get("cmd") or "").lower()
        if name in ("shutdown", "exit", "quit"):
            await worker.shutdown()
            write_resp({"ok": True, "cmd": "shutdown"})
            break
        if name == "ping":
            write_resp(
                {
                    "ok": True,
                    "cmd": "pong",
                    "rss_mb": round(rss_mb(), 2),
                    "solves": worker.solves,
                    "concurrency": worker.concurrency,
                }
            )
            continue
        if name == "prefetch":
            write_resp(await worker.prefetch())
            continue
        if name == "recycle":
            await worker.recycle()
            write_resp(
                {
                    "ok": True,
                    "cmd": "recycle",
                    "rss_mb": round(rss_mb(), 2),
                    "recycled": True,
                }
            )
            continue
        if name == "error":
            write_resp({"ok": False, "error": cmd.get("error") or "bad command"})
            continue
        if name != "solve":
            write_resp({"ok": False, "error": f"unknown cmd: {name}"})
            continue
        resp = await worker.solve(
            job_id=str(cmd.get("id") or ""),
            url=str(cmd.get("url") or ""),
            sitekey=str(cmd.get("sitekey") or ""),
            action=str(cmd.get("action") or ""),
            cdata=str(cmd.get("cdata") or ""),
            proxy=str(cmd.get("proxy") or ""),
        )
        write_resp(resp)
    return 0


def main() -> int:
    try:
        return asyncio.run(amain())
    except KeyboardInterrupt:
        return 130
    except Exception:
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
