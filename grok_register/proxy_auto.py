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
        logger: Callable[[str], None] | None = None,
    ):
        self.config = config
        self.normalize_proxy = normalize_proxy
        self.bootstrap_proxies = bootstrap_proxies or (lambda: ())
        self.logger = logger or (lambda _msg: None)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._refresh_lock = threading.Lock()

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
            try:
                self.refresh_once()
            except Exception as exc:
                self.logger(f"[proxy-auto] refresh failed: {exc}")
            self._stop.wait(max(1, self.config.interval_sec))

    def refresh_once(self):
        if not self.config.enabled:
            return []
        if not self._refresh_lock.acquire(blocking=False):
            return []
        try:
            previous_active = load_active_proxies(self.config)
            bootstrap = list(_unique(list(self.bootstrap_proxies()) + previous_active))
            bodies = fetch_source_bodies(self.config, bootstrap)
            candidates = list(previous_active)
            for body in bodies:
                candidates.extend(extract_nodes_from_text(body.text))
            candidates = list(_unique(candidates))
            if self.config.max_candidates:
                candidates = candidates[: self.config.max_candidates]

            results = test_candidates(self.config, candidates, self.normalize_proxy)
            active = [item for item in results if item.ok and item.proxy]
            active.sort(key=lambda item: item.latency_ms if item.latency_ms is not None else 10**9)
            if self.config.max_active:
                active = active[: self.config.max_active]
            proxies = list(_unique(item.proxy for item in active if item.proxy))
            write_outputs(self.config, active)
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


def test_candidates(
    config: ProxyAutoConfig,
    candidates: Iterable[str],
    normalize_proxy: Callable[[str], str | None],
    request_get: Callable[[str, str | None, int, dict[str, str]], object] | None = None,
):
    candidates = list(_unique(candidates))
    if not candidates:
        return []

    def run(candidate):
        started = time.monotonic()
        proxy = None
        try:
            proxy = normalize_proxy(candidate)
            if not proxy:
                return ProxyTestResult(candidate, None, False, error="unsupported proxy")
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
                    return ProxyTestResult(
                        candidate,
                        proxy,
                        False,
                        latency_ms=int((time.monotonic() - started) * 1000),
                        status_code=status_code,
                        error=f"status {status_code}",
                    )
            return ProxyTestResult(
                candidate,
                proxy,
                True,
                latency_ms=int((time.monotonic() - started) * 1000),
                status_code=status_code,
            )
        except Exception as exc:
            return ProxyTestResult(
                candidate,
                proxy,
                False,
                latency_ms=int((time.monotonic() - started) * 1000),
                error=str(exc),
            )

    with concurrent.futures.ThreadPoolExecutor(max_workers=config.test_workers) as executor:
        futures = [executor.submit(run, candidate) for candidate in candidates]
        return [future.result() for future in concurrent.futures.as_completed(futures)]


def write_outputs(config: ProxyAutoConfig, active_results: Iterable[ProxyTestResult]):
    active_results = list(active_results)
    proxies = list(_unique(item.proxy for item in active_results if item.proxy))
    config.output_path.mkdir(parents=True, exist_ok=True)
    _atomic_write(config.active_path, "\n".join(proxies) + ("\n" if proxies else ""))
    if "base64" in config.export_formats or "b64" in config.export_formats:
        payload = "\n".join(proxies)
        _atomic_write(config.base64_path, base64.b64encode(payload.encode()).decode() + "\n")
    state = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "active_count": len(proxies),
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
    decoded = _try_decode_base64_payload(text)
    if decoded and decoded != text:
        _extract_nodes_into(decoded, out, depth + 1)


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
