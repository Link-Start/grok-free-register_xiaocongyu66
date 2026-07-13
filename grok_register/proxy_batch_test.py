"""
Batch-test proxies for x.ai / accounts.x.ai reachability.

Sources (selectable):
  - manual pool: 代理.txt / PROXY_POOL_FILE
  - active pool: logs/proxy-auto-active.txt
  - public nodes: logs/proxy-scraper-candidates.txt (from proxy_scraper)
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

PROJECT_ROOT = Path(__file__).resolve().parents[1]
JOB_STATE_PATH = PROJECT_ROOT / "logs" / "proxy-batch-job.json"

_job_lock = threading.Lock()
_job: dict[str, Any] = {
    "running": False,
    "started_at": 0,
    "finished_at": 0,
    "message": "",
    "error": "",
    "use_public": False,
    "workers": 0,
    "timeout_sec": 0,
    "test_urls": [],
    "total": 0,
    "tested": 0,
    "ok": 0,
    "fail": 0,
    "active_file": "",
    "report_file": "",
    "top": [],
    "updated_at": 0,
}


def _job_state_path() -> Path:
    raw = (os.environ.get("PROXY_BATCH_JOB_FILE") or "").strip()
    if raw:
        p = Path(raw).expanduser()
        return p if p.is_absolute() else PROJECT_ROOT / p
    return JOB_STATE_PATH


def _persist_job_unlocked() -> None:
    """Write job snapshot for refresh / process restart recovery."""
    path = _job_state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = dict(_job)
        payload["updated_at"] = time.time()
        payload["updated_at_iso"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        # keep top list small on disk
        top = payload.get("top") or []
        if isinstance(top, list) and len(top) > 30:
            payload["top"] = top[:30]
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass


def _load_job_from_disk() -> dict[str, Any] | None:
    path = _job_state_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return None


def _pid_alive(pid: Any) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError, TypeError):
        return False


def _hydrate_job_from_disk() -> None:
    """Always merge disk snapshot — Go batch owns the progress file."""
    disk = _load_job_from_disk()
    if not disk:
        return
    with _job_lock:
        if disk.get("running"):
            pid = disk.get("pid")
            engine = str(disk.get("engine") or "")
            stale = False
            if pid:
                stale = not _pid_alive(pid)
            else:
                # Old Python path / crashed job left running=true without pid
                # Treat as stale if no update for > 3 minutes
                age = time.time() - float(disk.get("updated_at") or disk.get("started_at") or 0)
                stale = age > 180 or engine != "go"
            if stale:
                disk = dict(disk)
                disk["running"] = False
                disk["finished_at"] = disk.get("finished_at") or time.time()
                disk["message"] = (disk.get("message") or "") + " · 任务已结束/中断（已恢复显示）"
                try:
                    path = _job_state_path()
                    path.write_text(
                        json.dumps(disk, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8",
                    )
                except OSError:
                    pass
            for k, v in disk.items():
                _job[k] = v
            return

        mem_has = bool(_job.get("started_at") or _job.get("finished_at") or _job.get("message"))
        if not mem_has or float(disk.get("updated_at") or 0) >= float(_job.get("updated_at") or 0):
            for k, v in disk.items():
                _job[k] = v


# Single URL by default: half the work vs dual-URL probes (batch speed).
DEFAULT_TEST_URLS = (
    "https://accounts.x.ai/sign-up?redirect=grok-com",
)


def job_status() -> dict[str, Any]:
    _hydrate_job_from_disk()
    with _job_lock:
        out = dict(_job)
        out["state_file"] = str(_job_state_path())
        # do not let Python memory "running" override a finished Go job on disk
        return out


def _set_job(**kwargs) -> None:
    with _job_lock:
        _job.update(kwargs)
        _job["updated_at"] = time.time()
        _persist_job_unlocked()


def _read_proxy_lines(
    path: Path,
    *,
    limit: int | None = None,
    prefer_share_links: bool = False,
) -> list[str]:
    out: list[str] = []
    if not path.is_file():
        return out
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return out
    cleaned: list[str] = []
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("scheme="):
            continue
        cleaned.append(line)
    if prefer_share_links:
        share = [x for x in cleaned if _is_share_or_auth_socks(x)]
        plain = [x for x in cleaned if not _is_share_or_auth_socks(x)]
        cleaned = share + plain
    if limit is not None:
        cleaned = cleaned[: max(0, int(limit))]
    return cleaned


def _manual_pool_paths() -> list[Path]:
    paths: list[Path] = []
    raw = (os.environ.get("PROXY_POOL_FILE") or "").strip()
    if raw:
        p = Path(raw).expanduser()
        paths.append(p if p.is_absolute() else PROJECT_ROOT / p)
    paths.extend(
        [
            PROJECT_ROOT / "代理.txt",
            PROJECT_ROOT / "proxy.txt",
        ]
    )
    return paths


def _parse_proxy_blob(text: str | None) -> list[str]:
    """Parse free-form custom proxy list (newline / comma / space separated)."""
    if not text:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for raw in text.replace(",", "\n").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line in seen:
            continue
        seen.add(line)
        out.append(line)
    return out


_SHARE_PREFIXES = (
    "vless://",
    "vmess://",
    "trojan://",
    "ss://",
    "hy2://",
    "hysteria2://",
    "tuic://",
    "anytls://",
)


def _is_share_or_auth_socks(line: str) -> bool:
    low = (line or "").strip().lower()
    if any(low.startswith(p) for p in _SHARE_PREFIXES):
        return True
    if "t.me/socks" in low or "telegram.me/socks" in low:
        return True
    # socks5 with userinfo needs sing-box relay for Chromium; also for consistent HTTP test
    if low.startswith(("socks5://", "socks5h://", "socks://")) and "@" in low.split("://", 1)[-1]:
        return True
    return False


def _priority_key(line: str, source: str) -> tuple:
    """Prefer manual/custom share links over dead public plain HTTP."""
    src_rank = {"custom": 0, "manual": 1, "active": 2, "public": 3}.get(source, 9)
    kind = 0 if _is_share_or_auth_socks(line) else 1
    return (src_rank, kind)


def collect_proxy_candidates(
    *,
    use_manual: bool = True,
    use_active: bool = True,
    use_public: bool = False,
    max_candidates: int = 200,
    custom_proxies: Iterable[str] | None = None,
    custom_file: str | Path | None = None,
    prefer_share_links: bool = True,
) -> dict[str, Any]:
    """Gather unique proxy candidates with source tags."""
    max_candidates = max(1, min(int(max_candidates or 200), 40000))
    by_proxy: dict[str, str] = {}  # proxy -> source

    def add_lines(lines: Iterable[str], source: str) -> None:
        for line in lines:
            line = (line or "").strip()
            if not line or line in by_proxy:
                continue
            by_proxy[line] = source

    sources_meta = {"manual": 0, "active": 0, "public": 0, "custom": 0, "share_links": 0}

    custom_list = list(custom_proxies or [])
    if custom_file:
        p = Path(str(custom_file)).expanduser()
        if not p.is_absolute():
            p = PROJECT_ROOT / p
        custom_list.extend(_read_proxy_lines(p))
    if custom_list:
        before = len(by_proxy)
        add_lines(custom_list, "custom")
        sources_meta["custom"] += len(by_proxy) - before

    if use_manual:
        for path in _manual_pool_paths():
            lines = _read_proxy_lines(path)
            before = len(by_proxy)
            add_lines(lines, "manual")
            sources_meta["manual"] += len(by_proxy) - before

    if use_active:
        path = PROJECT_ROOT / "logs" / "proxy-auto-active.txt"
        lines = _read_proxy_lines(path)
        before = len(by_proxy)
        add_lines(lines, "active")
        sources_meta["active"] += len(by_proxy) - before

    public_path = Path(
        os.environ.get("PROXY_SCRAPER_OUT") or "logs/proxy-scraper-candidates.txt"
    )
    if not public_path.is_absolute():
        public_path = PROJECT_ROOT / public_path
    public_count_file = 0
    try:
        public_count_file = sum(
            1
            for line in public_path.read_text(encoding="utf-8", errors="replace").splitlines()
            if line.strip() and not line.strip().startswith("#")
        )
    except OSError:
        public_count_file = 0

    if use_public:
        # Prefer vless/ss/trojan from public scraper over dead plain HTTP.
        pull = max_candidates if not prefer_share_links else min(40000, max(max_candidates * 4, 2000))
        lines = _read_proxy_lines(
            public_path, limit=pull, prefer_share_links=prefer_share_links
        )
        before = len(by_proxy)
        add_lines(lines, "public")
        sources_meta["public"] += len(by_proxy) - before

    ordered = sorted(by_proxy.keys(), key=lambda c: _priority_key(c, by_proxy[c]))
    if prefer_share_links:
        # Fill first with share/auth-socks, then plain http/socks
        share = [c for c in ordered if _is_share_or_auth_socks(c)]
        plain = [c for c in ordered if not _is_share_or_auth_socks(c)]
        ordered = share + plain
    candidates = ordered[:max_candidates]
    sources_meta["share_links"] = sum(1 for c in candidates if _is_share_or_auth_socks(c))

    return {
        "candidates": candidates,
        "sources": {c: by_proxy[c] for c in candidates},
        "counts": {
            "total": len(candidates),
            "manual": sources_meta["manual"],
            "active": sources_meta["active"],
            "public": sources_meta["public"],
            "custom": sources_meta["custom"],
            "share_links": sources_meta["share_links"],
            "public_file": public_count_file,
        },
        "public_file": str(public_path),
        "use_public": use_public,
    }


def prepare_candidates_with_relay(
    candidates: list[str],
    sources: dict[str, str],
    *,
    use_relay: bool = True,
    max_relay: int | None = None,
) -> dict[str, Any]:
    """
    Convert vless/vmess/trojan/ss + authenticated SOCKS into local HTTP
    via built-in sing-box relay so Go tester can use them.
    """
    if not use_relay:
        return {
            "candidates": list(candidates),
            "sources": dict(sources),
            "relayed": 0,
            "relay_failed": 0,
            "direct": len(candidates),
            "message": "未启用订阅/中继转换",
        }

    from grok_register.register import (
        _normalize_proxy_line,
        _builtin_proxy_relay_manager,
        PROXY_RELAY_ENABLED,
        PROXY_RELAY_BUILTIN_ENABLED,
    )

    if not PROXY_RELAY_ENABLED:
        return {
            "candidates": list(candidates),
            "sources": dict(sources),
            "relayed": 0,
            "relay_failed": 0,
            "direct": len(candidates),
            "message": "PROXY_RELAY_ENABLED=0，跳过转换",
        }

    max_relay = int(
        max_relay
        if max_relay is not None
        else (os.environ.get("PROXY_RELAY_BATCH_MAX") or "200")
    )
    max_relay = max(8, min(max_relay, 500))

    manager = None
    if PROXY_RELAY_BUILTIN_ENABLED:
        try:
            manager = _builtin_proxy_relay_manager()
            if manager is not None:
                # BuiltinProxyRelayConfig is frozen — swap config object
                from dataclasses import replace as dc_replace

                manager.config = dc_replace(manager.config, max_nodes=max_relay)
        except Exception:
            manager = None

    out: list[str] = []
    out_sources: dict[str, str] = {}
    relayed = 0
    relay_failed = 0
    direct = 0
    seen: set[str] = set()
    convert_budget = max_relay

    for raw in candidates:
        raw = (raw or "").strip()
        if not raw:
            continue
        src = sources.get(raw, "unknown")
        need = _is_share_or_auth_socks(raw)
        if not need:
            if raw not in seen:
                seen.add(raw)
                out.append(raw)
                out_sources[raw] = src
                direct += 1
            continue
        if convert_budget <= 0:
            relay_failed += 1
            continue
        try:
            converted = _normalize_proxy_line(raw)
        except Exception:
            converted = None
        if converted and converted != raw:
            convert_budget -= 1
            if converted not in seen:
                seen.add(converted)
                out.append(converted)
                out_sources[converted] = f"{src}+relay"
                relayed += 1
        elif converted:
            if converted not in seen:
                seen.add(converted)
                out.append(converted)
                out_sources[converted] = src
                direct += 1
        else:
            relay_failed += 1

    return {
        "candidates": out,
        "sources": out_sources,
        "relayed": relayed,
        "relay_failed": relay_failed,
        "direct": direct,
        "message": f"中继转换：成功 {relayed} · 失败 {relay_failed} · 直连保留 {direct} · 上限 {max_relay}",
    }


def _simple_normalize(candidate: str) -> str | None:
    """Lightweight normalize for http/socks URLs; share links need register relay."""
    c = (candidate or "").strip()
    if not c:
        return None
    lower = c.lower()
    # share-link schemes need full normalize from register (sing-box relay)
    if lower.startswith(
        ("vmess://", "vless://", "trojan://", "ss://", "hy2://", "hysteria2://", "tuic://", "anytls://")
    ):
        try:
            from grok_register.register import _normalize_proxy_line

            return _normalize_proxy_line(c)
        except Exception:
            return None
    if "://" not in c:
        # bare host:port → try http
        if ":" in c and not c.startswith("["):
            return f"http://{c}"
        return None
    parsed = urlparse(c)
    if parsed.scheme.lower() in {"http", "https", "socks5", "socks5h", "socks4"}:
        return c
    return None


def _normalize_test_urls(test_urls: Iterable[str] | str | None) -> tuple[str, ...]:
    if test_urls is None:
        env_urls = (os.environ.get("PROXY_AUTO_TEST_URLS") or "").strip()
        if env_urls:
            parts = [u.strip() for u in env_urls.split(",") if u.strip()]
            if parts:
                return tuple(parts)
        return DEFAULT_TEST_URLS
    if isinstance(test_urls, str):
        parts = [u.strip() for u in test_urls.replace("\n", ",").split(",") if u.strip()]
        return tuple(parts) if parts else DEFAULT_TEST_URLS
    parts = [str(u).strip() for u in test_urls if str(u).strip()]
    return tuple(parts) if parts else DEFAULT_TEST_URLS


def run_batch_xai_test(
    *,
    use_public: bool = False,
    use_manual: bool = True,
    use_active: bool = True,
    max_candidates: int = 200,
    workers: int | None = None,
    timeout: int | None = None,
    test_urls: Iterable[str] | str | None = None,
    custom_proxies: Iterable[str] | str | None = None,
    custom_file: str | Path | None = None,
    max_active: int | None = None,
    write_active: bool = True,
) -> dict[str, Any]:
    """
    Batch test candidates against configurable URLs (default x.ai).
    Concurrent via Go proxy-worker / Python thread pool (test_workers).
    """
    from dataclasses import replace

    from grok_register.proxy_auto import ProxyAutoConfig, test_candidates, write_outputs

    if isinstance(custom_proxies, str):
        custom_list = _parse_proxy_blob(custom_proxies)
    else:
        custom_list = list(custom_proxies or [])

    collected = collect_proxy_candidates(
        use_manual=use_manual,
        use_active=use_active,
        use_public=use_public,
        max_candidates=max_candidates,
        custom_proxies=custom_list,
        custom_file=custom_file,
    )
    candidates = collected["candidates"]
    if not candidates:
        return {
            "ok": False,
            "message": "没有可测代理（手动/自定义池为空"
            + ("；公共节点文件也为空，可先爬取" if use_public else "；未启用公共节点")
            + "）",
            "counts": collected["counts"],
            "results": [],
            "active": [],
            "workers": workers,
            "timeout_sec": timeout,
        }

    workers = int(
        workers
        if workers is not None
        else (os.environ.get("PROXY_AUTO_TEST_WORKERS") or "128")
    )
    # I/O-bound bulk scan: allow very high fan-out (Go goroutines / thread pool).
    workers = max(1, min(workers, 2048))
    timeout = int(
        timeout if timeout is not None else (os.environ.get("PROXY_AUTO_TEST_TIMEOUT") or "5")
    )
    timeout = max(2, min(timeout, 120))
    urls = _normalize_test_urls(test_urls)
    max_active_n = max_active
    if max_active_n is None:
        try:
            max_active_n = int(os.environ.get("PROXY_AUTO_MAX_ACTIVE") or "0")
        except ValueError:
            max_active_n = 0
    max_active_n = max(0, min(int(max_active_n or 0), 40000))

    base = ProxyAutoConfig.from_env()
    config = replace(
        base,
        test_workers=workers,
        test_timeout=timeout,
        test_urls=urls,
        max_candidates=0,
        max_active=max_active_n,
        include_bootstrap_candidates=False,
        output_dir=str(PROJECT_ROOT / "logs"),
    )

    # Progress-friendly sharding:
    # One shard ≈ 1 concurrent wave (size ≈ workers). UI updates every wave
    # instead of every 5000 (old) which looked stuck at 0/30000 for minutes.
    total_n = len(candidates)
    # Keep each Go/Python call ~1–2 timeout-rounds so progress moves often.
    chunk_size = max(workers, min(workers * 2, 800))
    if total_n <= chunk_size:
        shards = [candidates]
    else:
        shards = [candidates[i : i + chunk_size] for i in range(0, total_n, chunk_size)]

    results = []
    started = time.monotonic()
    eta_hint = max(1, (len(shards) * timeout) // 60)
    _set_job(
        total=total_n,
        tested=0,
        ok=0,
        fail=0,
        workers=workers,
        timeout_sec=timeout,
        shard=0,
        shards=len(shards),
        message=(
            f"准备测活 {total_n} 个 · 并发 {workers} · 分片 {len(shards)} "
            f"(约每 {timeout}s 更新) · 预估 ≥{eta_hint} 分钟 · "
            f"公共={collected['counts'].get('public', 0)} "
            f"手动={collected['counts'].get('manual', 0)} "
            f"自定义={collected['counts'].get('custom', 0)}"
        ),
        counts=collected["counts"],
        top=[],
    )

    for shard_i, batch in enumerate(shards, 1):
        # Mark "in flight" before blocking call so UI is not stuck at 0
        _set_job(
            message=(
                f"测活中 {len(results)}/{total_n} · "
                f"正在跑分片 {shard_i}/{len(shards)} "
                f"({len(batch)} 个并发) · 并发 {workers} · 超时 {timeout}s · "
                f"已用 {round(time.monotonic() - started, 1)}s"
            ),
            shard=shard_i,
            shards=len(shards),
            workers=workers,
            timeout_sec=timeout,
        )
        part = test_candidates(config, batch, _simple_normalize)
        results.extend(part)
        ok_so_far = sum(1 for r in results if r.ok and r.proxy)
        fail_so_far = len(results) - ok_so_far
        elapsed_so_far = round(time.monotonic() - started, 1)
        rps = round(len(results) / elapsed_so_far, 1) if elapsed_so_far > 0 else 0
        remain = total_n - len(results)
        eta_sec = int(remain / rps) if rps > 0 else int(remain / max(workers, 1) * timeout)
        _set_job(
            tested=len(results),
            ok=ok_so_far,
            fail=fail_so_far,
            workers=workers,
            timeout_sec=timeout,
            shard=shard_i,
            shards=len(shards),
            message=(
                f"测活中 {len(results)}/{total_n} · "
                f"{ok_so_far}✓/{fail_so_far}✗ · "
                f"并发 {workers} · 分片 {shard_i}/{len(shards)} · "
                f"{elapsed_so_far}s · ~{rps}/s · 剩余约 {max(0, eta_sec)}s"
            ),
            top=[
                {
                    "proxy": (r.proxy or "")[:160],
                    "candidate": (r.candidate or "")[:160],
                    "latency_ms": r.latency_ms,
                    "status_code": r.status_code,
                    "source": collected["sources"].get(r.candidate, ""),
                }
                for r in sorted(
                    [x for x in results if x.ok and x.proxy],
                    key=lambda r: (r.latency_ms if r.latency_ms is not None else 10**9),
                )[:15]
            ],
        )

    elapsed = round(time.monotonic() - started, 2)

    ok_items = [r for r in results if r.ok and r.proxy]
    ok_items.sort(key=lambda r: (r.latency_ms if r.latency_ms is not None else 10**9))
    active_proxies = [r.proxy for r in ok_items if r.proxy]
    if max_active_n:
        active_proxies = active_proxies[:max_active_n]

    active_path = PROJECT_ROOT / "logs" / "proxy-auto-active.txt"
    report_path = PROJECT_ROOT / "logs" / "proxy-batch-xai-report.json"
    if write_active:
        try:
            write_outputs(config, results)
            active_path = config.active_path
        except Exception:
            active_path.parent.mkdir(parents=True, exist_ok=True)
            active_path.write_text(
                "\n".join(active_proxies) + ("\n" if active_proxies else ""),
                encoding="utf-8",
            )

    report = {
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "elapsed_sec": elapsed,
        "use_public": use_public,
        "use_manual": use_manual,
        "use_active": use_active,
        "custom_count": collected["counts"].get("custom", 0),
        "test_urls": list(urls),
        "workers": workers,
        "timeout_sec": timeout,
        "max_candidates": max_candidates,
        "max_active": max_active_n,
        "total": len(results),
        "ok": len(ok_items),
        "fail": len(results) - len(ok_items),
        "counts": collected["counts"],
        "active_count": len(active_proxies),
        "active_file": str(active_path),
        "top": [
            {
                "proxy": (r.proxy or "")[:160],
                "candidate": (r.candidate or "")[:160],
                "latency_ms": r.latency_ms,
                "status_code": r.status_code,
                "source": collected["sources"].get(r.candidate, ""),
            }
            for r in ok_items[:30]
        ],
        "failed_sample": [
            {
                "candidate": (r.candidate or "")[:120],
                "error": (r.error or "")[:120],
                "status_code": r.status_code,
                "source": collected["sources"].get(r.candidate, ""),
            }
            for r in results
            if not r.ok
        ][:20],
    }
    try:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass

    rps = round(len(results) / elapsed, 1) if elapsed > 0 else 0
    msg = (
        f"批量测活完成：{len(ok_items)}/{len(results)} 可用 · "
        f"并发 {workers} · 超时 {timeout}s · {elapsed}s (~{rps}/s) · "
        f"公共={collected['counts'].get('public', 0)} "
        f"手动={collected['counts'].get('manual', 0)} "
        f"自定义={collected['counts'].get('custom', 0)}"
    )
    if not ok_items:
        msg += " · 本轮无可用代理（公共免费节点多数已死属正常）"

    return {
        "ok": len(ok_items) > 0,
        "message": msg,
        "elapsed_sec": elapsed,
        "total": len(results),
        "ok_count": len(ok_items),
        "fail_count": len(results) - len(ok_items),
        "workers": workers,
        "timeout_sec": timeout,
        "test_urls": list(urls),
        "active": active_proxies[:50],
        "active_count": len(active_proxies),
        "active_file": str(active_path),
        "report_file": str(report_path),
        "use_public": use_public,
        "counts": collected["counts"],
        "top": report["top"],
        "report": report,
    }


def _resolve_proxy_worker_bin() -> Path | None:
    try:
        from grok_register.proxy_auto import resolve_proxy_worker_bin

        return resolve_proxy_worker_bin()
    except Exception:
        pass
    for cand in (
        PROJECT_ROOT / "native" / "proxy-worker" / "proxy-worker",
        Path(os.environ.get("PROXY_WORKER_BIN") or ""),
    ):
        if cand and cand.is_file() and os.access(cand, os.X_OK):
            return cand
    return None


def _go_batch_running_from_disk() -> dict[str, Any] | None:
    """If a Go batch process is still alive, return its progress snapshot."""
    # hydrate first so stale running flags are cleared
    st = job_status()
    if st.get("running") and st.get("engine") == "go" and _pid_alive(st.get("pid")):
        return st
    return None


def start_batch_job(
    *,
    use_public: bool = False,
    use_manual: bool = True,
    use_active: bool = True,
    max_candidates: int = 200,
    workers: int | None = None,
    timeout: int | None = None,
    test_urls: Iterable[str] | str | None = None,
    custom_proxies: Iterable[str] | str | None = None,
    custom_file: str | Path | None = None,
    max_active: int | None = None,
    use_relay: bool = True,
    max_relay: int | None = None,
) -> dict[str, Any]:
    """
    Start batch test.

    Preferred: spawn Go `proxy-worker batch` (progress on disk; browser only polls).
    Before Go: convert vless/ss/auth-socks → local HTTP via sing-box relay.
    Fallback: Python thread + chunked test_candidates if Go binary missing.
    """
    live = _go_batch_running_from_disk()
    if live and live.get("running"):
        return {
            "ok": False,
            "message": f"批量测活已在进行中（Go pid={live.get('pid')}）",
            "job": job_status(),
        }
    with _job_lock:
        if _job.get("running"):
            return {"ok": False, "message": "批量测活已在进行中（请看下方进度）", "job": job_status()}

    w_disp = workers if workers is not None else int(os.environ.get("PROXY_AUTO_TEST_WORKERS") or "128")
    t_disp = timeout if timeout is not None else int(os.environ.get("PROXY_AUTO_TEST_TIMEOUT") or "5")
    urls_disp = list(_normalize_test_urls(test_urls))

    if isinstance(custom_proxies, str):
        custom_list = _parse_proxy_blob(custom_proxies)
    else:
        custom_list = list(custom_proxies or [])
    try:
        preview = collect_proxy_candidates(
            use_manual=use_manual,
            use_active=use_active,
            use_public=use_public,
            max_candidates=max_candidates,
            custom_proxies=custom_list,
            custom_file=custom_file,
            prefer_share_links=True,
        )
        preview_counts = dict(preview["counts"])
        candidates = preview["candidates"]
        sources = preview["sources"]
    except Exception as exc:
        return {"ok": False, "message": f"收集候选失败: {exc}", "job": job_status()}

    if not candidates:
        return {
            "ok": False,
            "message": (
                "没有可测代理：请勾选「使用公共节点」或「手动池」，"
                "或先点「爬取公共节点」/填入自定义代理"
            ),
            "job": job_status(),
            "counts": preview_counts,
        }

    # Seed "converting" state so UI is not blank during sing-box import
    _set_job(
        running=True,
        started_at=time.time(),
        finished_at=0,
        engine="go",
        message=(
            f"正在转换订阅/分享链接为本地 HTTP（sing-box）· "
            f"候选 {len(candidates)} · 分享链接约 {preview_counts.get('share_links', 0)}"
        ),
        error="",
        workers=int(w_disp),
        timeout_sec=int(t_disp),
        test_urls=urls_disp,
        total=len(candidates),
        tested=0,
        ok=0,
        fail=0,
        use_public=use_public,
        counts=preview_counts,
        top=[],
    )

    relay_info = prepare_candidates_with_relay(
        candidates,
        sources,
        use_relay=use_relay,
        max_relay=max_relay,
    )
    candidates = relay_info["candidates"]
    sources = relay_info["sources"]
    preview_counts["relayed"] = relay_info.get("relayed", 0)
    preview_counts["relay_failed"] = relay_info.get("relay_failed", 0)
    preview_counts["direct"] = relay_info.get("direct", 0)
    preview_counts["total"] = len(candidates)
    preview_n = len(candidates)

    if preview_n <= 0:
        _set_job(
            running=False,
            finished_at=time.time(),
            message="转换后无可用候选（分享链接中继失败）",
            error=relay_info.get("message") or "",
        )
        return {
            "ok": False,
            "message": "转换后无可用候选：" + (relay_info.get("message") or ""),
            "job": job_status(),
            "counts": preview_counts,
        }

    max_active_n = max_active
    if max_active_n is None:
        try:
            max_active_n = int(os.environ.get("PROXY_AUTO_MAX_ACTIVE") or "0")
        except ValueError:
            max_active_n = 0

    go_bin = _resolve_proxy_worker_bin()
    prefer_go = (os.environ.get("PROXY_WORKER_ENGINE") or "go").strip().lower() in {
        "go",
        "auto",
        "",
    }

    # ── Go path: write job file + spawn detached process ──
    if prefer_go and go_bin is not None:
        logs = PROJECT_ROOT / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        job_file = logs / "proxy-batch-job-request.json"
        progress_file = _job_state_path()
        active_file = logs / "proxy-auto-active.txt"
        report_file = logs / "proxy-batch-xai-report.json"
        go_log = logs / "proxy-batch-go.log"

        job_doc = {
            "candidates": candidates,
            "test_urls": urls_disp,
            "timeout_sec": int(t_disp),
            "workers": int(w_disp),
            "accept_status": [[200, 399]],
            "max_active": int(max_active_n or 0),
            "sources": sources,
            "counts": preview_counts,
            "use_public": use_public,
            "use_manual": use_manual,
            "use_active": use_active,
            "relay": {
                "relayed": relay_info.get("relayed"),
                "relay_failed": relay_info.get("relay_failed"),
                "direct": relay_info.get("direct"),
            },
        }
        job_file.write_text(json.dumps(job_doc, ensure_ascii=False) + "\n", encoding="utf-8")

        seed = {
            "running": True,
            "engine": "go",
            "started_at": time.time(),
            "finished_at": 0,
            "message": (
                f"{relay_info.get('message')} · 即将 Go 测活 {preview_n} 个 · "
                f"并发 {w_disp} · 超时 {t_disp}s"
            ),
            "error": "",
            "workers": int(w_disp),
            "timeout_sec": int(t_disp),
            "test_urls": urls_disp,
            "total": preview_n,
            "tested": 0,
            "ok": 0,
            "fail": 0,
            "use_public": use_public,
            "counts": preview_counts,
            "top": [],
            "active_file": str(active_file),
            "report_file": str(report_file),
            "state_file": str(progress_file),
            "updated_at": time.time(),
        }
        progress_file.write_text(json.dumps(seed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        with _job_lock:
            _job.update(seed)

        cmd = [
            str(go_bin),
            "batch",
            "--job",
            str(job_file),
            "--progress",
            str(progress_file),
            "--active",
            str(active_file),
            "--report",
            str(report_file),
            "--progress-every",
            "50",
        ]
        try:
            with open(go_log, "ab", buffering=0) as logf:
                proc = subprocess.Popen(
                    cmd,
                    cwd=str(PROJECT_ROOT),
                    stdout=logf,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    start_new_session=True,
                )
        except OSError as exc:
            return {"ok": False, "message": f"启动 Go 测活失败: {exc}", "job": job_status()}

        seed["pid"] = proc.pid
        seed["message"] = (
            f"Go 测活已启动 pid={proc.pid} · {preview_n} 候选 "
            f"(中继 {relay_info.get('relayed', 0)} / 直连 {relay_info.get('direct', 0)}) · "
            f"并发 {w_disp} · 超时 {t_disp}s（关页面不影响）"
        )
        progress_file.write_text(json.dumps(seed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        with _job_lock:
            _job.update(seed)

        return {
            "ok": True,
            "message": seed["message"],
            "engine": "go",
            "pid": proc.pid,
            "job": job_status(),
            "counts": preview_counts,
            "relay": relay_info,
        }

    # ── Python fallback ──
    def worker():
        _set_job(
            running=True,
            started_at=time.time(),
            finished_at=0,
            message=(
                f"Python 测活（无 Go 二进制）· {preview_n} 候选 · 并发 {w_disp}"
            ),
            error="",
            engine="python",
            use_public=use_public,
            workers=int(w_disp),
            timeout_sec=int(t_disp),
            test_urls=urls_disp,
            total=preview_n,
            tested=0,
            ok=0,
            fail=0,
            active_file="",
            report_file="",
            top=[],
            counts=preview_counts,
        )
        try:
            out = run_batch_xai_test(
                use_public=use_public,
                use_manual=use_manual,
                use_active=use_active,
                max_candidates=max_candidates,
                workers=workers,
                timeout=timeout,
                test_urls=test_urls,
                custom_proxies=custom_proxies,
                custom_file=custom_file,
                max_active=max_active,
                write_active=True,
            )
            _set_job(
                running=False,
                finished_at=time.time(),
                message=out.get("message") or "",
                total=out.get("total") or 0,
                tested=out.get("total") or 0,
                ok=out.get("ok_count") or 0,
                fail=out.get("fail_count") or 0,
                workers=out.get("workers") or w_disp,
                timeout_sec=out.get("timeout_sec") or t_disp,
                test_urls=out.get("test_urls") or urls_disp,
                active_file=out.get("active_file") or "",
                report_file=out.get("report_file") or "",
                top=out.get("top") or [],
                error="",
                last_result=out,
                counts=out.get("counts") or preview_counts,
                engine="python",
            )
        except Exception as exc:
            _set_job(
                running=False,
                finished_at=time.time(),
                message="批量测活失败",
                error=str(exc)[:400],
            )

    t = threading.Thread(target=worker, name="proxy-batch-xai", daemon=True)
    t.start()
    return {
        "ok": True,
        "message": (
            f"已启动 Python 测活（未找到 Go proxy-worker）：{preview_n} 候选 · 并发 {w_disp}"
        ),
        "engine": "python",
        "job": job_status(),
        "counts": preview_counts,
    }


