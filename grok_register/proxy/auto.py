from __future__ import annotations

import base64
import binascii
import concurrent.futures
from dataclasses import dataclass
from datetime import datetime, timezone
import html
import json
import os
from pathlib import Path
import re
import threading
import time
from typing import Callable, Iterable
from urllib.parse import unquote, urlparse

import requests


NODE_SCHEMES = (
    "vmess",
    "vless",
    "trojan",
    "ss",
    "ssr",
    "hy2",
    "hysteria2",
    "tuic",
    "anytls",
    "http",
    "https",
    "socks4",
    "socks5",
    "socks5h",
)
SHARE_LINK_SCHEMES = (
    "vmess",
    "vless",
    "trojan",
    "ss",
    "ssr",
    "hy2",
    "hysteria2",
    "tuic",
    "anytls",
)
DEFAULT_SOURCES = (
    "https://raw.githubusercontent.com/peasoft/NoMoreWalls/master/list.txt",
    "https://cdn.jsdelivr.net/gh/peasoft/NoMoreWalls@master/list.txt",
)
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)


@dataclass(frozen=True)
class SourceSpec:
    url: str
    expands_to_sources: bool = False


@dataclass(frozen=True)
class ProxyAutoConfig:
    enabled: bool = False
    sources: tuple[str, ...] = DEFAULT_SOURCES
    sources_file: str = "proxy-sources.txt"
    interval_sec: int = 1200
    fetch_workers: int = 8
    test_workers: int = 16
    fetch_timeout: int = 12
    test_timeout: int = 10
    test_urls: tuple[str, ...] = ("https://accounts.x.ai/sign-up?redirect=grok-com",)
    accept_status: tuple[tuple[int, int], ...] = ((200, 399),)
    output_dir: str = "logs"
    active_file: str = "proxy-auto-active.txt"
    state_file: str = "proxy-auto-state.json"
    export_formats: tuple[str, ...] = ("raw", "sub2api")
    max_candidates: int = 0
    max_active: int = 0
    source_list_depth: int = 1
    include_bootstrap_candidates: bool = True

    @classmethod
    def from_env(cls, env=os.environ):
        sources = _split_env_list(env.get("PROXY_AUTO_FETCH_URLS"))
        if not sources:
            sources = DEFAULT_SOURCES
        output_dir = env.get("PROXY_AUTO_OUTPUT_DIR") or "logs"
        return cls(
            enabled=_env_bool(env, "PROXY_AUTO_FETCH_ENABLED", False),
            sources=tuple(sources),
            sources_file=(env.get("PROXY_AUTO_FETCH_SOURCES_FILE") or "proxy-sources.txt").strip(),
            interval_sec=_env_int(env, "PROXY_AUTO_FETCH_INTERVAL_SEC", 1200),
            fetch_workers=max(1, _env_int(env, "PROXY_AUTO_FETCH_WORKERS", 8)),
            test_workers=max(1, _env_int(env, "PROXY_AUTO_TEST_WORKERS", 16)),
            fetch_timeout=max(1, _env_int(env, "PROXY_AUTO_FETCH_TIMEOUT", 12)),
            test_timeout=max(1, _env_int(env, "PROXY_AUTO_TEST_TIMEOUT", 10)),
            test_urls=tuple(
                _split_env_list(env.get("PROXY_AUTO_TEST_URLS"))
                or ["https://accounts.x.ai/sign-up?redirect=grok-com"]
            ),
            accept_status=_parse_status_ranges(env.get("PROXY_AUTO_TEST_ACCEPT_STATUS") or "200-399"),
            output_dir=output_dir,
            active_file=(env.get("PROXY_AUTO_ACTIVE_FILE") or "proxy-auto-active.txt").strip(),
            state_file=(env.get("PROXY_AUTO_STATE_FILE") or "proxy-auto-state.json").strip(),
            export_formats=tuple(
                fmt.lower()
                for fmt in (_split_env_list(env.get("PROXY_AUTO_EXPORT_FORMATS")) or ["raw", "sub2api"])
            ),
            max_candidates=max(0, _env_int(env, "PROXY_AUTO_MAX_CANDIDATES", 0)),
            max_active=max(0, _env_int(env, "PROXY_AUTO_MAX_ACTIVE", 0)),
            source_list_depth=max(0, _env_int(env, "PROXY_AUTO_SOURCE_LIST_DEPTH", 1)),
            include_bootstrap_candidates=_env_bool(
                env,
                "PROXY_AUTO_INCLUDE_BOOTSTRAP_CANDIDATES",
                True,
            ),
        )

    @property
    def output_path(self):
        return Path(self.output_dir)

    @property
    def active_path(self):
        return self.output_path / self.active_file

    @property
    def state_path(self):
        return self.output_path / self.state_file

    @property
    def sub2api_path(self):
        return self.output_path / "proxy-auto-sub2api.json"

    @property
    def cpa_path(self):
        return self.output_path / "proxy-auto-cpa.json"

    @property
    def base64_path(self):
        return self.output_path / "proxy-auto-active.b64"


@dataclass(frozen=True)
class FetchResult:
    url: str
    text: str
    proxy: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class ProxyTestResult:
    candidate: str
    proxy: str | None
    ok: bool
    latency_ms: int | None = None
    status_code: int | None = None
    error: str | None = None


@dataclass(frozen=True)
class TextResponse:
    text: str
    status_code: int = 200


class RotatingProxySelector:
    def __init__(self, proxies: Iterable[str]):
        self.proxies = tuple(_unique(p for p in proxies if p))
        self._idx = 0
        self._lock = threading.Lock()

    def next(self):
        if not self.proxies:
            return None
        with self._lock:
            proxy = self.proxies[self._idx % len(self.proxies)]
            self._idx += 1
            return proxy


class ProxyAutoManager:
    def __init__(
        self,
        config: ProxyAutoConfig,
        normalize_proxy: Callable[[str], str | None],
        bootstrap_proxies: Callable[[], Iterable[str]] | None = None,
        cleanup_proxy: Callable[[str, str | None, bool], None] | None = None,
        logger: Callable[[str], None] | None = None,
    ):
        self.config = config
        self.normalize_proxy = normalize_proxy
        self.bootstrap_proxies = bootstrap_proxies or (lambda: ())
        self.cleanup_proxy = cleanup_proxy or (lambda _candidate, _proxy, _ok: None)
        self.logger = logger or (lambda _msg: None)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._refresh_lock = threading.Lock()
        self._last_refresh_at = 0.0

    def start(self):
        if not self.config.enabled:
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="proxy-auto-refresh", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        while not self._stop.is_set():
            if self._last_refresh_at > 0:
                elapsed = time.monotonic() - self._last_refresh_at
                wait_for = max(0, max(1, self.config.interval_sec) - elapsed)
                if wait_for and self._stop.wait(wait_for):
                    break
            try:
                self.refresh_once()
            except Exception as exc:
                self._last_refresh_at = time.monotonic()
                self.logger(f"[proxy-auto] refresh failed: {exc}")

    def refresh_once(self):
        if not self.config.enabled:
            return []
        if not self._refresh_lock.acquire(blocking=False):
            self._last_refresh_at = time.monotonic()
            return []
        try:
            previous_active = load_active_proxies(self.config)
            previous_candidates = load_previous_candidates(self.config)
            bootstrap = list(_unique(list(self.bootstrap_proxies()) + previous_active))
            bodies = fetch_source_bodies(self.config, bootstrap)
            candidates = list(previous_candidates or previous_active)
            if self.config.include_bootstrap_candidates:
                candidates.extend(bootstrap)
            for body in bodies:
                # share links + free-proxy host:port lines
                candidates.extend(extract_candidates_from_text(body.text))
            # Optional: candidates produced by proxy_scraper (public free lists).
            # Disable with PROXY_SCRAPER_MERGE=0 (tests should set this when isolating).
            if _env_bool(os.environ, "PROXY_SCRAPER_MERGE", True):
                scraper_file = (
                    os.environ.get("PROXY_SCRAPER_OUT") or "logs/proxy-scraper-candidates.txt"
                ).strip()
                if scraper_file:
                    try:
                        scraper_path = Path(scraper_file).expanduser()
                        if not scraper_path.is_absolute():
                            scraper_path = Path.cwd() / scraper_path
                        for line in scraper_path.read_text(encoding="utf-8").splitlines():
                            line = line.strip()
                            if line and not line.startswith("#"):
                                candidates.append(line)
                    except OSError:
                        pass
            candidates = list(_unique(candidates))
            if self.config.max_candidates:
                candidates = candidates[: self.config.max_candidates]

            results = test_candidates(
                self.config,
                candidates,
                self.normalize_proxy,
                cleanup_proxy=self.cleanup_proxy,
            )
            active = [item for item in results if item.ok and item.proxy]
            active.sort(key=_proxy_result_sort_key)
            if self.config.max_active:
                active = active[: self.config.max_active]
            proxies = list(_unique(item.proxy for item in active if item.proxy))
            write_outputs(self.config, results)
            self._last_refresh_at = time.monotonic()
            self.logger(
                f"[proxy-auto] sources={len(bodies)} candidates={len(candidates)} active={len(proxies)}"
            )
            return proxies
        finally:
            self._refresh_lock.release()


def load_active_proxies(config: ProxyAutoConfig):
    try:
        return [
            line.strip()
            for line in config.active_path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
    except OSError:
        return []


def load_previous_candidates(config: ProxyAutoConfig):
    try:
        state = json.loads(config.state_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    proxies = state.get("proxies") if isinstance(state, dict) else None
    if not isinstance(proxies, list):
        return []
    candidates = []
    for item in proxies:
        if not isinstance(item, dict):
            continue
        candidate = str(item.get("candidate") or "").strip()
        proxy = str(item.get("proxy") or "").strip()
        candidates.append(candidate or proxy)
    return list(_unique(candidates))


def fetch_source_bodies(
    config: ProxyAutoConfig,
    bootstrap_proxies: Iterable[str] = (),
    request_get: Callable[[str, str | None, int, dict[str, str]], object] | None = None,
):
    specs = load_source_specs(config)
    bodies: list[FetchResult] = []
    for depth in range(config.source_list_depth + 1):
        if not specs:
            break
        results = _fetch_specs(config, specs, bootstrap_proxies, request_get=request_get)
        specs = []
        for result, spec in results:
            if result.error:
                continue
            if spec.expands_to_sources and depth < config.source_list_depth:
                specs.extend(parse_source_lines(result.text))
            else:
                bodies.append(result)
    return bodies


def load_source_specs(config: ProxyAutoConfig):
    specs: list[SourceSpec] = []
    for raw in config.sources:
        parsed = parse_source_line(raw)
        if parsed:
            specs.append(parsed)
    source_path = Path(config.sources_file).expanduser()
    try:
        for raw in source_path.read_text(encoding="utf-8").splitlines():
            if raw.strip().upper() == "EOF":
                break
            parsed = parse_source_line(raw)
            if parsed:
                specs.append(parsed)
    except OSError:
        pass
    return list(_unique_specs(specs))


def parse_source_lines(text: str):
    specs = []
    for raw in text.splitlines():
        parsed = parse_source_line(raw)
        if parsed:
            specs.append(parsed)
    return list(_unique_specs(specs))


def parse_source_line(raw: str):
    line = (raw or "").strip()
    if not line or line.startswith("#"):
        return None
    expands = False
    while line.startswith("!"):
        line = line[1:].lstrip()
    if line.startswith("*"):
        expands = True
        line = line[1:].lstrip()
    if line.startswith("+date"):
        line = datetime.now().strftime(line[len("+date") :].strip())
    if not line.lower().startswith(("http://", "https://", "file://")):
        return None
    return SourceSpec(line, expands)


def extract_nodes_from_text(text: str):
    seen = []
    _extract_nodes_into(text, seen, depth=0)
    return list(_unique(seen))


def extract_candidates_from_text(text: str, *, default_scheme: str = "http"):
    """Extract share links + bare host:port proxies for free-proxy lists."""
    nodes = extract_nodes_from_text(text)
    bare = extract_bare_host_ports(text, default_scheme=default_scheme)
    return list(_unique(list(nodes) + list(bare)))


def test_candidates(
    config: ProxyAutoConfig,
    candidates: Iterable[str],
    normalize_proxy: Callable[[str], str | None],
    cleanup_proxy: Callable[[str, str | None, bool], None] | None = None,
    request_get: Callable[[str, str | None, int, dict[str, str]], object] | None = None,
):
    candidates = list(_unique(candidates))
    if not candidates:
        return []

    # Prefer Go proxy-worker for bulk HTTP/SOCKS testing when available.
    # Custom request_get (unit tests) always uses the Python path.
    engine = (os.environ.get("PROXY_WORKER_ENGINE") or "auto").strip().lower()
    if request_get is None and engine in {"auto", "go"}:
        go_results = _test_candidates_via_go(config, candidates, normalize_proxy)
        if go_results is not None:
            if cleanup_proxy:
                for result in go_results:
                    try:
                        cleanup_proxy(result.candidate, result.proxy, result.ok)
                    except Exception:
                        pass
            return go_results
        if engine == "go":
            # Forced Go but unavailable/failed — fall through only if auto would;
            # for explicit go we still fall back so registration does not die.
            pass

    return _test_candidates_python(
        config,
        candidates,
        normalize_proxy,
        cleanup_proxy=cleanup_proxy,
        request_get=request_get,
    )


def _test_candidates_python(
    config: ProxyAutoConfig,
    candidates: list[str],
    normalize_proxy: Callable[[str], str | None],
    cleanup_proxy: Callable[[str, str | None, bool], None] | None = None,
    request_get: Callable[[str, str | None, int, dict[str, str]], object] | None = None,
):
    def run(candidate):
        started = time.monotonic()
        proxy = None
        result = None
        try:
            proxy = normalize_proxy(candidate)
            if not proxy:
                result = ProxyTestResult(candidate, None, False, error="unsupported proxy")
                return result
            status_code = None
            for url in config.test_urls:
                response = _request_get(
                    url,
                    proxy,
                    config.test_timeout,
                    {"User-Agent": USER_AGENT, "Accept": "*/*"},
                    request_get=request_get,
                )
                status_code = getattr(response, "status_code", None)
                if status_code is None or not _status_allowed(int(status_code), config.accept_status):
                    result = ProxyTestResult(
                        candidate,
                        proxy,
                        False,
                        latency_ms=int((time.monotonic() - started) * 1000),
                        status_code=status_code,
                        error=f"status {status_code}",
                    )
                    return result
            result = ProxyTestResult(
                candidate,
                proxy,
                True,
                latency_ms=int((time.monotonic() - started) * 1000),
                status_code=status_code,
            )
            return result
        except Exception as exc:
            result = ProxyTestResult(
                candidate,
                proxy,
                False,
                latency_ms=int((time.monotonic() - started) * 1000),
                error=str(exc),
            )
            return result
        finally:
            if cleanup_proxy and result is not None:
                try:
                    cleanup_proxy(candidate, proxy, result.ok)
                except Exception:
                    pass

    results = []
    stop_after_active = max(0, int(config.max_active or 0))
    active_count = 0
    pending_candidates = iter(candidates)

    with concurrent.futures.ThreadPoolExecutor(max_workers=config.test_workers) as executor:
        futures = set()
        for _ in range(min(config.test_workers, len(candidates))):
            try:
                futures.add(executor.submit(run, next(pending_candidates)))
            except StopIteration:
                break
        while futures:
            done, futures = concurrent.futures.wait(
                futures,
                return_when=concurrent.futures.FIRST_COMPLETED,
            )
            for future in done:
                result = future.result()
                results.append(result)
                if result.ok and result.proxy:
                    active_count += 1
            if stop_after_active and active_count >= stop_after_active:
                for future in futures:
                    future.cancel()
                executor.shutdown(wait=False, cancel_futures=True)
                return results
            while len(futures) < config.test_workers:
                try:
                    futures.add(executor.submit(run, next(pending_candidates)))
                except StopIteration:
                    break
    return results


def resolve_proxy_worker_bin() -> Path | None:
    raw = (os.environ.get("PROXY_WORKER_BIN") or "").strip()
    candidates = []
    if raw:
        candidates.append(Path(raw).expanduser())
    root = Path(__file__).resolve().parents[2]
    candidates.append(root / "native" / "proxy-worker" / "proxy-worker")
    for path in candidates:
        try:
            if path.is_file() and os.access(path, os.X_OK):
                return path
        except OSError:
            continue
    return None


def _test_candidates_via_go(
    config: ProxyAutoConfig,
    candidates: list[str],
    normalize_proxy: Callable[[str], str | None],
) -> list[ProxyTestResult] | None:
    """Return results from Go worker, or None to fall back to Python."""
    payload = {
        "candidates": list(candidates),
        "test_urls": list(config.test_urls),
        "timeout_sec": int(config.test_timeout),
        "workers": int(config.test_workers),
        "accept_status": [[int(a), int(b)] for a, b in config.accept_status],
        "max_active": int(config.max_active or 0),
        "user_agent": USER_AGENT,
    }
    worker_url = (os.environ.get("PROXY_WORKER_URL") or "").strip().rstrip("/")
    try:
        if worker_url:
            data = _go_worker_http_test(worker_url, payload)
        else:
            binary = resolve_proxy_worker_bin()
            if binary is None:
                return None
            data = _go_worker_cli_test(binary, payload)
    except Exception:
        return None

    raw_results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(raw_results, list):
        return None

    out: list[ProxyTestResult] = []
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        candidate = str(item.get("candidate") or "")
        proxy = item.get("proxy")
        if proxy is not None:
            proxy = str(proxy)
        # Prefer Python-side normalize when Go left proxy empty but candidate is valid.
        if not proxy:
            try:
                proxy = normalize_proxy(candidate)
            except Exception:
                proxy = None
        ok = bool(item.get("ok"))
        latency = item.get("latency_ms")
        try:
            latency_ms = int(latency) if latency is not None else None
        except (TypeError, ValueError):
            latency_ms = None
        status = item.get("status_code")
        try:
            status_code = int(status) if status is not None else None
        except (TypeError, ValueError):
            status_code = None
        error = item.get("error")
        out.append(
            ProxyTestResult(
                candidate,
                proxy,
                ok,
                latency_ms=latency_ms,
                status_code=status_code,
                error=str(error) if error else None,
            )
        )
    return out


def _go_worker_cli_test(binary: Path, payload: dict) -> dict:
    import subprocess

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    # Wall clock ≈ rounds * per-proxy timeout. rounds = ceil(n / workers).
    n = len(payload.get("candidates") or [])
    workers = max(1, int(payload.get("workers") or 64))
    per = max(1, int(payload.get("timeout_sec") or 10))
    rounds = max(1, (n + workers - 1) // workers)
    # +2 rounds slack for scheduling; hard cap 2 hours.
    timeout = max(60, min(7200, (rounds + 2) * per + 30))
    proc = subprocess.run(
        [str(binary), "test"],
        input=body,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode("utf-8", "replace")[:500] or f"exit {proc.returncode}")
    return json.loads(proc.stdout.decode("utf-8"))


def _go_worker_http_test(base_url: str, payload: dict) -> dict:
    session = requests.Session()
    session.trust_env = False
    try:
        timeout = max(30, int(payload.get("timeout_sec") or 10) * 3 + 60)
        resp = session.post(
            base_url.rstrip("/") + "/v1/test",
            json=payload,
            timeout=timeout,
            headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"proxy-worker HTTP {resp.status_code}")
        return resp.json()
    finally:
        session.close()


def write_outputs(config: ProxyAutoConfig, test_results: Iterable[ProxyTestResult]):
    test_results = list(test_results)
    active_results = [item for item in test_results if item.ok and item.proxy]
    active_results.sort(key=_proxy_result_sort_key)
    if config.max_active:
        active_results = active_results[: config.max_active]
    proxies = list(_unique(item.proxy for item in active_results if item.proxy))
    failed_count = sum(1 for item in test_results if not item.ok)
    config.output_path.mkdir(parents=True, exist_ok=True)
    _atomic_write(config.active_path, "\n".join(proxies) + ("\n" if proxies else ""))
    if "base64" in config.export_formats or "b64" in config.export_formats:
        payload = "\n".join(proxies)
        _atomic_write(config.base64_path, base64.b64encode(payload.encode()).decode() + "\n")
    state = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "active_count": len(proxies),
        "test_count": len(test_results),
        "failed_count": failed_count,
        "error_summary": _proxy_test_error_summary(test_results),
        "proxies": [
            {
                "candidate": item.candidate,
                "proxy": item.proxy,
                "latency_ms": item.latency_ms,
                "status_code": item.status_code,
            }
            for item in active_results
            if item.proxy
        ],
    }
    _atomic_write(config.state_path, json.dumps(state, ensure_ascii=False, indent=2) + "\n")
    if "sub2api" in config.export_formats:
        _atomic_write(config.sub2api_path, json.dumps(sub2api_payload(proxies), ensure_ascii=False, indent=2) + "\n")
    if "cpa" in config.export_formats:
        _atomic_write(config.cpa_path, json.dumps(sub2api_payload(proxies), ensure_ascii=False, indent=2) + "\n")


def _proxy_result_sort_key(item: ProxyTestResult):
    latency = item.latency_ms if item.latency_ms is not None else 10**9
    return (latency, item.proxy or item.candidate)


def _proxy_test_error_summary(results: Iterable[ProxyTestResult]):
    summary = {}
    for item in results:
        if item.ok:
            continue
        reason = item.error
        if not reason and item.status_code is not None:
            reason = f"status {item.status_code}"
        reason = _compact_error_reason(reason or "unknown error")
        summary[reason] = summary.get(reason, 0) + 1
    return dict(sorted(summary.items(), key=lambda pair: (-pair[1], pair[0])))


def _compact_error_reason(reason: str):
    line = str(reason or "unknown error").splitlines()[0].strip()
    if not line:
        return "unknown error"
    status = re.search(r"\bstatus\s+(\d{3})\b", line, re.I)
    if status:
        return f"status {status.group(1)}"
    lowered = line.lower()
    if "unsupported proxy" in lowered:
        return "unsupported proxy"
    if "tunnel connection failed" in lowered:
        return "proxy tunnel failed"
    if "unable to connect to proxy" in lowered:
        return "unable to connect to proxy"
    if "sslcertverificationerror" in lowered:
        return "proxy TLS verification failed"
    if "connection refused" in lowered:
        return "connection refused"
    if "timed out" in lowered or "timeout" in lowered:
        return "timeout"
    return line[:200]


def sub2api_payload(proxies: Iterable[str]):
    items = []
    for idx, proxy_url in enumerate(proxies, 1):
        item = proxy_url_to_sub2api_proxy(proxy_url, idx)
        if item:
            items.append(item)
    return {
        "type": "sub2api-data",
        "version": 1,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "proxies": items,
        "accounts": [],
    }


def proxy_url_to_sub2api_proxy(proxy_url: str, idx=1):
    parsed = urlparse(proxy_url)
    if parsed.scheme not in {"http", "https", "socks5", "socks5h"}:
        return None
    if not parsed.hostname or not parsed.port:
        return None
    username = unquote(parsed.username or "")
    password = unquote(parsed.password or "")
    protocol = parsed.scheme
    key = f"{protocol}|{parsed.hostname}|{parsed.port}|{username}|{password}"
    return {
        "proxy_key": key,
        "name": f"auto-{idx}-{protocol}-{parsed.hostname}-{parsed.port}",
        "protocol": protocol,
        "host": parsed.hostname,
        "port": parsed.port,
        "username": username,
        "password": password,
        "status": "active",
    }


def _fetch_specs(config, specs, bootstrap_proxies, request_get=None):
    selector = RotatingProxySelector(bootstrap_proxies)

    def run(spec):
        proxy = selector.next()
        try:
            response = _request_get(
                spec.url,
                proxy,
                config.fetch_timeout,
                {"User-Agent": USER_AGENT, "Accept": "*/*"},
                request_get=request_get,
            )
            if getattr(response, "status_code", 200) >= 400:
                return FetchResult(spec.url, "", proxy=proxy, error=f"status {response.status_code}"), spec
            return FetchResult(spec.url, getattr(response, "text", "") or "", proxy=proxy), spec
        except Exception as exc:
            if proxy:
                try:
                    response = _request_get(
                        spec.url,
                        None,
                        config.fetch_timeout,
                        {"User-Agent": USER_AGENT, "Accept": "*/*"},
                        request_get=request_get,
                    )
                    if getattr(response, "status_code", 200) >= 400:
                        return FetchResult(spec.url, "", error=f"status {response.status_code}"), spec
                    return FetchResult(spec.url, getattr(response, "text", "") or ""), spec
                except Exception:
                    pass
            return FetchResult(spec.url, "", proxy=proxy, error=str(exc)), spec

    with concurrent.futures.ThreadPoolExecutor(max_workers=config.fetch_workers) as executor:
        futures = [executor.submit(run, spec) for spec in specs]
        return [future.result() for future in concurrent.futures.as_completed(futures)]


def _request_get(url, proxy, timeout, headers, request_get=None):
    if request_get:
        return request_get(url, proxy, timeout, headers)
    if str(url).lower().startswith("file://"):
        parsed = urlparse(url)
        return TextResponse(Path(unquote(parsed.path)).read_text(encoding="utf-8"))
    session = requests.Session()
    session.trust_env = False
    proxies = {"http": proxy, "https": proxy} if proxy else None
    try:
        return session.get(url, timeout=timeout, headers=headers, proxies=proxies, allow_redirects=True)
    finally:
        session.close()


def _extract_nodes_into(text, out, depth):
    if not text or depth > 1:
        return
    text = html.unescape(text)
    pattern = re.compile(
        r"(?P<link>(?:"
        + "|".join(re.escape(scheme) for scheme in NODE_SCHEMES)
        + r")://[^\s\"'<>`]+)",
        re.I,
    )
    for match in pattern.finditer(text):
        link = _clean_node_link(match.group("link"))
        if _looks_like_proxy_or_node(link):
            out.append(link)
    # Clash/Meta YAML proxy blocks → approximate share/proxy URLs
    for link in extract_clash_proxy_links(text):
        out.append(link)
    decoded = _try_decode_base64_payload(text)
    if decoded and decoded != text:
        _extract_nodes_into(decoded, out, depth + 1)


def extract_bare_host_ports(text: str, *, default_scheme: str = "http"):
    """Parse free-proxy list lines like `1.2.3.4:8080` or `1.2.3.4:1080:user:pass`."""
    if not text:
        return []
    scheme = (default_scheme or "http").strip().lower() or "http"
    if scheme not in {"http", "https", "socks4", "socks5", "socks5h"}:
        scheme = "http"
    out = []
    # ip:port or host:port
    host_port = re.compile(
        r"(?m)^\s*(?P<host>(?:\d{1,3}\.){3}\d{1,3}|[a-zA-Z0-9][a-zA-Z0-9._-]*[a-zA-Z0-9])"
        r":(?P<port>\d{2,5})"
        r"(?::(?P<user>[^\s:#]+):(?P<password>[^\s#]+))?\s*$"
    )
    for match in host_port.finditer(text):
        host = match.group("host")
        port = int(match.group("port"))
        if port < 1 or port > 65535:
            continue
        # skip obvious non-proxy high-noise ports sometimes found in docs
        if host.lower() in {"0.0.0.0", "127.0.0.1", "localhost"}:
            continue
        user = match.group("user")
        password = match.group("password")
        if user and password:
            out.append(f"{scheme}://{user}:{password}@{host}:{port}")
        else:
            out.append(f"{scheme}://{host}:{port}")
    return list(_unique(out))


def extract_clash_proxy_links(text: str):
    """Best-effort extraction of Clash YAML proxy entries into usable URLs.

    Full Clash conversion is complex; we cover common ss/trojan/http/socks5 and
    leave vmess/vless when uuid/server/port are present as share links when possible.
    """
    if not text or "type:" not in text or "server:" not in text:
        return []
    # Split loosely on list items under proxies
    blocks = re.split(r"(?m)^\s*-\s+(?=name:|\{)", text)
    out = []
    for block in blocks:
        if "server:" not in block or "type:" not in block:
            # one-line map style: { name: x, type: ss, server: y, port: 1, ... }
            if not re.search(r"\btype\s*:", block):
                continue
        item = _parse_clash_proxy_block(block)
        if not item:
            continue
        link = _clash_item_to_link(item)
        if link:
            out.append(link)
    return list(_unique(out))


def _parse_clash_proxy_block(block: str):
    data = {}
    # inline JSON-ish / flow style
    for key in (
        "name", "type", "server", "port", "uuid", "password", "cipher", "method",
        "username", "udp", "tls", "network", "sni", "skip-cert-verify",
    ):
        m = re.search(
            rf"(?i)(?:^|[\s,{{]){re.escape(key)}\s*:\s*([^\n,}}]+)",
            block,
        )
        if not m:
            continue
        value = m.group(1).strip().strip("'\"")
        data[key.lower().replace("-", "_")] = value
    if "server" not in data or "type" not in data:
        return None
    if "port" not in data:
        return None
    try:
        data["port"] = int(str(data["port"]).strip())
    except ValueError:
        return None
    return data


def _clash_item_to_link(item: dict):
    ptype = str(item.get("type") or "").lower()
    server = item.get("server")
    port = item.get("port")
    if not server or not port:
        return None
    if ptype in {"http", "https"}:
        user = item.get("username")
        password = item.get("password")
        if user or password:
            return f"http://{user or ''}:{password or ''}@{server}:{port}"
        return f"http://{server}:{port}"
    if ptype in {"socks5", "socks"}:
        user = item.get("username")
        password = item.get("password")
        if user or password:
            return f"socks5://{user or ''}:{password or ''}@{server}:{port}"
        return f"socks5://{server}:{port}"
    if ptype in {"ss", "shadowsocks"}:
        method = item.get("cipher") or item.get("method") or "aes-256-gcm"
        password = item.get("password") or ""
        # ss://base64(method:password)@host:port
        userinfo = base64.urlsafe_b64encode(f"{method}:{password}".encode()).decode().rstrip("=")
        return f"ss://{userinfo}@{server}:{port}"
    if ptype == "trojan":
        password = item.get("password") or ""
        sni = item.get("sni") or ""
        q = f"?sni={sni}" if sni else ""
        return f"trojan://{password}@{server}:{port}{q}"
    if ptype == "vmess":
        uuid = item.get("uuid") or ""
        if not uuid:
            return None
        payload = {
            "v": "2",
            "ps": item.get("name") or server,
            "add": server,
            "port": str(port),
            "id": uuid,
            "aid": "0",
            "net": item.get("network") or "tcp",
            "type": "none",
            "tls": "tls" if str(item.get("tls") or "").lower() in {"true", "1", "yes"} else "",
        }
        encoded = base64.urlsafe_b64encode(
            json.dumps(payload, ensure_ascii=False).encode()
        ).decode().rstrip("=")
        return f"vmess://{encoded}"
    if ptype == "vless":
        uuid = item.get("uuid") or ""
        if not uuid:
            return None
        return f"vless://{uuid}@{server}:{port}"
    return None


def _clean_node_link(link):
    return (link or "").strip().strip(" \t\r\n'\"`<>[](){}，,;")


def _looks_like_proxy_or_node(link):
    parsed = urlparse(link)
    scheme = parsed.scheme.lower()
    if scheme in SHARE_LINK_SCHEMES:
        return True
    if scheme in {"http", "https", "socks4", "socks5", "socks5h"}:
        return bool(parsed.hostname and parsed.port and not parsed.path.strip("/"))
    return False


def _try_decode_base64_payload(text):
    compact = re.sub(r"\s+", "", text or "")
    if len(compact) < 16:
        return None
    if not re.fullmatch(r"[A-Za-z0-9_+/=-]+", compact):
        return None
    padding = "=" * ((4 - len(compact) % 4) % 4)
    for decoder in (base64.b64decode, base64.urlsafe_b64decode):
        try:
            decoded = decoder((compact + padding).encode()).decode("utf-8", "ignore")
        except (binascii.Error, ValueError):
            continue
        if "://" in decoded:
            return decoded
    return None


def _split_env_list(value):
    if not value:
        return []
    parts = []
    for chunk in re.split(r"[\n,]+", value):
        item = chunk.strip()
        if item:
            parts.append(item)
    return parts


def _parse_status_ranges(value):
    ranges = []
    for part in _split_env_list(value):
        try:
            if "-" in part:
                start, end = part.split("-", 1)
                ranges.append((int(start), int(end)))
            else:
                status = int(part)
                ranges.append((status, status))
        except ValueError:
            continue
    return tuple(ranges or [(200, 399)])


def _status_allowed(status, ranges):
    return any(start <= status <= end for start, end in ranges)


def _env_bool(env, key, default):
    raw = env.get(key)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(env, key, default):
    try:
        return int(env.get(key, default))
    except (TypeError, ValueError):
        return default


def _unique(values):
    seen = set()
    for value in values:
        if value is None:
            continue
        key = str(value).strip()
        if not key or key in seen:
            continue
        seen.add(key)
        yield value


def _unique_specs(specs):
    seen = set()
    for spec in specs:
        key = (spec.url, spec.expands_to_sources)
        if key in seen:
            continue
        seen.add(key)
        yield spec


def _atomic_write(path: Path, text: str):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
