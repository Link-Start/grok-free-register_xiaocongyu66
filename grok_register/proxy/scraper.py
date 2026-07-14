"""
Public free-proxy / subscription scraper.

Pulls from curated catalogs (GitHub raw lists, subscription URLs, free proxy APIs)
and optional GitHub code-search discovery. Output is candidate lines that
`proxy_auto` can test against Grok/xAI.

This is intentionally HTTP-first (no browser JS runtime). Pages that only expose
proxies via heavy client-side JS should publish a raw/API endpoint instead; we
still scrape HTML for embedded share links / host:port patterns when present.

Usage:
  python -m grok_register.proxy_scraper scrape
  python -m grok_register.proxy_scraper scrape --github
  python -m grok_register.proxy_scraper sources
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import quote, urlparse

import requests

from grok_register.proxy.auto import (
    USER_AGENT,
    extract_candidates_from_text,
    extract_nodes_from_text,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CATALOG = PROJECT_ROOT / "proxy-scraper-sources.txt"
DEFAULT_OUT = PROJECT_ROOT / "logs" / "proxy-scraper-candidates.txt"
DEFAULT_REPORT = PROJECT_ROOT / "logs" / "proxy-scraper-report.json"

# Curated direct feeds (raw text / JSON / base64 subs). Prefer CDN mirrors.
BUILTIN_CATALOG: tuple[str, ...] = (
    # --- proxifly/free-proxy-list ---
    "https://cdn.jsdelivr.net/gh/proxifly/free-proxy-list@main/proxies/all/data.txt",
    "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/all/data.txt",
    "https://cdn.jsdelivr.net/gh/proxifly/free-proxy-list@main/proxies/protocols/http/data.txt",
    "https://cdn.jsdelivr.net/gh/proxifly/free-proxy-list@main/proxies/protocols/https/data.txt",
    "https://cdn.jsdelivr.net/gh/proxifly/free-proxy-list@main/proxies/protocols/socks5/data.txt",
    "https://cdn.jsdelivr.net/gh/proxifly/free-proxy-list@main/proxies/protocols/socks4/data.txt",
    # --- snakem982/proxypool ---
    "https://raw.githubusercontent.com/snakem982/proxypool/main/source/v2ray-2.txt",
    "https://cdn.jsdelivr.net/gh/snakem982/proxypool@main/source/v2ray-2.txt",
    "https://raw.githubusercontent.com/snakem982/proxypool/main/source/clash-meta.yaml",
    "https://cdn.jsdelivr.net/gh/snakem982/proxypool@main/source/clash-meta.yaml",
    "https://raw.githubusercontent.com/snakem982/proxypool/main/source/clash-meta-2.yaml",
    # --- common public node pools / subscriptions ---
    "https://raw.githubusercontent.com/peasoft/NoMoreWalls/master/list.txt",
    "https://cdn.jsdelivr.net/gh/peasoft/NoMoreWalls@master/list.txt",
    "https://raw.githubusercontent.com/mfuu/v2ray/master/v2ray",
    "https://raw.githubusercontent.com/mahdibland/V2RayAggregator/master/sub/sub_merge.txt",
    "https://raw.githubusercontent.com/Pawdroid/Free-servers/main/sub",
    "https://raw.githubusercontent.com/freefq/free/master/v2",
    "https://raw.githubusercontent.com/clarketm/proxy-list/master/proxy-list-raw.txt#scheme=http",
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt#scheme=http",
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt#scheme=socks5",
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks4.txt#scheme=socks4",
    "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/http.txt#scheme=http",
    "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/socks5.txt#scheme=socks5",
    "https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies-http.txt#scheme=http",
    "https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies-socks5.txt#scheme=socks5",
    "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=10000&country=all&ssl=all&anonymity=all#scheme=http",
    "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=socks5&timeout=10000&country=all#scheme=socks5",
    "https://raw.githubusercontent.com/hookzof/socks5_list/master/proxy.txt#scheme=socks5",
    "https://raw.githubusercontent.com/roosterkid/openproxylist/main/HTTPS_RAW.txt#scheme=http",
    "https://raw.githubusercontent.com/roosterkid/openproxylist/main/SOCKS5_RAW.txt#scheme=socks5",
    # Geonode free proxy API (JSON)
    "https://proxylist.geonode.com/api/proxy-list?limit=500&page=1&sort_by=lastChecked&sort_type=desc",
)

GITHUB_SEARCH_QUERIES = (
    "vless:// OR vmess:// OR trojan:// filename:sub extension:txt",
    "proxies all data.txt proxifly",
    "free-proxy-list socks5 data.txt",
    "v2ray free sub merge",
)


@dataclass(frozen=True)
class SourceJob:
    url: str
    default_scheme: str = "http"
    label: str = ""


@dataclass
class SourceFetch:
    url: str
    ok: bool
    status: int | None
    candidates: int
    error: str | None = None
    elapsed_ms: float = 0.0
    sample: tuple[str, ...] = ()


def _env_bool(key: str, default: bool = False) -> bool:
    raw = os.environ.get(key)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(key: str, default: int) -> int:
    try:
        return int(str(os.environ.get(key, "")).strip() or default)
    except ValueError:
        return default


def load_catalog_lines(path: Path | None = None) -> list[str]:
    lines = list(BUILTIN_CATALOG)
    catalog = path or Path(os.environ.get("PROXY_SCRAPER_SOURCES_FILE") or DEFAULT_CATALOG)
    if not catalog.is_absolute():
        catalog = PROJECT_ROOT / catalog
    if catalog.is_file():
        for raw in catalog.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            lines.append(line)
    # also merge project proxy-sources.txt as scrape inputs
    project_sources = PROJECT_ROOT / "proxy-sources.txt"
    if project_sources.is_file():
        for raw in project_sources.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or line.upper() == "EOF":
                continue
            # strip expand markers used by proxy_auto
            while line.startswith("!") or line.startswith("*"):
                line = line[1:].lstrip()
            if line.lower().startswith(("http://", "https://")):
                lines.append(line)
    # de-dupe preserve order
    seen = set()
    out = []
    for item in lines:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def parse_source_job(raw: str) -> SourceJob | None:
    line = (raw or "").strip()
    if not line or line.startswith("#"):
        return None
    default_scheme = "http"
    label = ""
    # support URL#scheme=socks5&label=foo
    if "#" in line:
        base, frag = line.split("#", 1)
        line = base.strip()
        for part in frag.split("&"):
            if "=" not in part:
                continue
            k, v = part.split("=", 1)
            k = k.strip().lower()
            v = v.strip()
            if k == "scheme" and v:
                default_scheme = v.lower()
            elif k == "label" and v:
                label = v
    if not line.lower().startswith(("http://", "https://")):
        return None
    # path-based scheme hints
    lower = line.lower()
    if default_scheme == "http":
        if "socks5" in lower:
            default_scheme = "socks5"
        elif "socks4" in lower:
            default_scheme = "socks4"
        elif re.search(r"/https(?:/|$)", lower) or "https_raw" in lower:
            default_scheme = "http"
    return SourceJob(url=line, default_scheme=default_scheme, label=label)


def fetch_url(
    url: str,
    *,
    timeout: int = 15,
    proxy: str | None = None,
    session: requests.Session | None = None,
) -> tuple[int | None, str, str | None]:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
    }
    own = session is None
    session = session or requests.Session()
    session.trust_env = False
    proxies = {"http": proxy, "https": proxy} if proxy else None
    try:
        resp = session.get(url, timeout=timeout, headers=headers, proxies=proxies, allow_redirects=True)
        text = resp.text or ""
        if resp.status_code >= 400:
            return resp.status_code, text, f"status {resp.status_code}"
        return resp.status_code, text, None
    except Exception as exc:
        return None, "", f"{type(exc).__name__}: {exc}"
    finally:
        if own:
            session.close()


def extract_from_body(text: str, *, default_scheme: str = "http", content_type_hint: str = "") -> list[str]:
    if not text:
        return []
    # JSON free-proxy dumps (proxifly data.json style)
    stripped = text.lstrip()
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            data = json.loads(text)
            found = _extract_from_json(data, default_scheme=default_scheme)
            if found:
                return found
        except Exception:
            pass
    # HTML pages: still try share links + bare ports in markup
    return extract_candidates_from_text(text, default_scheme=default_scheme)


def _extract_from_json(data, *, default_scheme: str = "http") -> list[str]:
    out: list[str] = []

    def walk(node):
        if isinstance(node, str):
            if "://" in node:
                out.extend(extract_nodes_from_text(node))
            elif re.fullmatch(r"(?:\d{1,3}\.){3}\d{1,3}:\d{2,5}", node.strip()):
                out.append(f"{default_scheme}://{node.strip()}")
            return
        if isinstance(node, dict):
            # common shapes: {ip,port,protocol} / {proxy,url}
            ip = node.get("ip") or node.get("host") or node.get("address")
            port = node.get("port")
            proto = (node.get("protocol") or node.get("type") or node.get("scheme") or default_scheme)
            if ip and port:
                try:
                    out.append(f"{str(proto).lower()}://{ip}:{int(port)}")
                except Exception:
                    pass
            for key in ("proxy", "url", "link", "uri"):
                val = node.get(key)
                if isinstance(val, str) and "://" in val:
                    out.extend(extract_nodes_from_text(val))
            for val in node.values():
                walk(val)
            return
        if isinstance(node, list):
            for item in node:
                walk(item)

    walk(data)
    # de-dupe
    seen = set()
    unique = []
    for item in out:
        if item in seen:
            continue
        seen.add(item)
        unique.append(item)
    return unique


def scrape_sources(
    sources: Iterable[str],
    *,
    workers: int = 12,
    timeout: int = 15,
    bootstrap_proxies: Iterable[str] = (),
    logger: Callable[[str], None] | None = None,
) -> tuple[list[str], list[SourceFetch]]:
    log = logger or (lambda _m: None)
    jobs = [j for j in (parse_source_job(s) for s in sources) if j]
    boot = [p for p in bootstrap_proxies if p]
    boot_idx = 0
    results: list[SourceFetch] = []
    all_candidates: list[str] = []
    lock = __import__("threading").Lock()

    def pick_proxy():
        nonlocal boot_idx
        if not boot:
            return None
        with lock:
            proxy = boot[boot_idx % len(boot)]
            boot_idx += 1
            return proxy

    packed: list[tuple[SourceFetch, list[str]]] = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futs = {pool.submit(_run_job, job, timeout, pick_proxy): job for job in jobs}
        for fut in as_completed(futs):
            try:
                item = fut.result()
                packed.append(item)
            except Exception as exc:
                job = futs[fut]
                packed.append(
                    (
                        SourceFetch(job.url, False, None, 0, error=str(exc)),
                        [],
                    )
                )

    for fetch, cands in packed:
        results.append(fetch)
        all_candidates.extend(cands)
        if fetch.ok:
            log(f"[scraper] ok {fetch.candidates:4d} {fetch.elapsed_ms:6.0f}ms {fetch.url[:90]}")
        else:
            log(f"[scraper] fail {fetch.error} {fetch.url[:90]}")

    # unique preserve order
    seen = set()
    unique = []
    for item in all_candidates:
        if not item or item in seen:
            continue
        seen.add(item)
        unique.append(item)
    return unique, results


def _run_job(job: SourceJob, timeout: int, pick_proxy) -> tuple[SourceFetch, list[str]]:
    started = time.time()
    proxy = pick_proxy() if callable(pick_proxy) else None
    status, text, err = fetch_url(job.url, timeout=timeout, proxy=proxy)
    if err and proxy:
        status, text, err = fetch_url(job.url, timeout=timeout, proxy=None)
    elapsed = (time.time() - started) * 1000
    if err:
        return SourceFetch(job.url, False, status, 0, error=err, elapsed_ms=elapsed), []
    cands = extract_from_body(text, default_scheme=job.default_scheme)
    return (
        SourceFetch(
            job.url,
            True,
            status,
            len(cands),
            elapsed_ms=elapsed,
            sample=tuple(cands[:3]),
        ),
        cands,
    )


def github_discover_raw_urls(
    *,
    token: str | None = None,
    queries: Iterable[str] = GITHUB_SEARCH_QUERIES,
    per_query: int = 10,
    timeout: int = 20,
    logger: Callable[[str], None] | None = None,
) -> list[str]:
    """Optional GitHub code search → raw.githubusercontent.com URLs.

    Requires network. Token (GITHUB_TOKEN) raises rate limits. Without a token
    unauthenticated search is heavily rate-limited; failures are soft.
    """
    log = logger or (lambda _m: None)
    token = token if token is not None else (os.environ.get("GITHUB_TOKEN") or "").strip()
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/vnd.github+json",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    found: list[str] = []
    session = requests.Session()
    session.trust_env = False
    try:
        for query in queries:
            url = (
                "https://api.github.com/search/code?q="
                + quote(query)
                + f"&per_page={max(1, min(30, per_query))}"
            )
            try:
                resp = session.get(url, headers=headers, timeout=timeout)
                if resp.status_code == 403:
                    log("[scraper] github search rate-limited; skip remaining queries")
                    break
                if resp.status_code >= 400:
                    log(f"[scraper] github search HTTP {resp.status_code} for {query[:40]}")
                    continue
                data = resp.json()
            except Exception as exc:
                log(f"[scraper] github search error: {exc}")
                continue
            for item in data.get("items") or []:
                html_url = item.get("html_url") or ""
                # https://github.com/owner/repo/blob/branch/path → raw
                m = re.match(
                    r"https://github\.com/([^/]+)/([^/]+)/blob/([^/]+)/(.*)",
                    html_url,
                )
                if not m:
                    # repository_url + path fallback
                    repo = (item.get("repository") or {}).get("full_name")
                    path = item.get("path")
                    if repo and path:
                        found.append(
                            f"https://raw.githubusercontent.com/{repo}/master/{path}"
                        )
                        found.append(
                            f"https://raw.githubusercontent.com/{repo}/main/{path}"
                        )
                    continue
                owner, repo, branch, path = m.groups()
                found.append(f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{path}")
            time.sleep(0.8 if not token else 0.2)
    finally:
        session.close()
    # de-dupe
    seen = set()
    out = []
    for u in found:
        if u in seen:
            continue
        seen.add(u)
        out.append(u)
    log(f"[scraper] github discovered {len(out)} raw urls")
    return out


def write_candidates(path: Path, candidates: Iterable[str]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    seen = set()
    for item in candidates:
        item = (item or "").strip()
        if not item or item in seen:
            continue
        seen.add(item)
        lines.append(item)
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return len(lines)


def write_report(path: Path, *, fetches: list[SourceFetch], candidates: int, extra: dict | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "candidates": candidates,
        "sources_ok": sum(1 for f in fetches if f.ok),
        "sources_fail": sum(1 for f in fetches if not f.ok),
        "fetches": [
            {
                "url": f.url,
                "ok": f.ok,
                "status": f.status,
                "candidates": f.candidates,
                "error": f.error,
                "elapsed_ms": round(f.elapsed_ms, 1),
                "sample": list(f.sample),
            }
            for f in sorted(fetches, key=lambda x: (-x.candidates, x.url))
        ],
    }
    if extra:
        payload.update(extra)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def maybe_auto_test_after_scrape(
    *,
    logger: Callable[[str], None] | None = None,
    force: bool | None = None,
) -> dict | None:
    """
    After scrape, optionally start Go/Python batch x.ai test automatically.
    Default ON (PROXY_SCRAPER_AUTO_TEST=1). Set 0 to disable.
    """
    log = logger or print
    enabled = _env_bool("PROXY_SCRAPER_AUTO_TEST", True) if force is None else bool(force)
    if not enabled:
        log("[scraper] auto-test disabled (PROXY_SCRAPER_AUTO_TEST=0)")
        return None
    try:
        from grok_register.proxy.batch_test import start_batch_job
    except Exception as exc:
        log(f"[scraper] auto-test import failed: {exc}")
        return {"ok": False, "message": str(exc)}

    max_c = max(1, min(_env_int("PROXY_SCRAPER_AUTO_TEST_MAX", 2000), 40000))
    workers = max(1, min(_env_int("PROXY_SCRAPER_AUTO_TEST_WORKERS", 128), 2048))
    timeout = max(2, min(_env_int("PROXY_SCRAPER_AUTO_TEST_TIMEOUT", 5), 60))
    max_relay = max(8, min(_env_int("PROXY_SCRAPER_AUTO_TEST_RELAY", 200), 500))
    use_manual = _env_bool("PROXY_SCRAPER_AUTO_TEST_MANUAL", True)
    use_public = _env_bool("PROXY_SCRAPER_AUTO_TEST_PUBLIC", True)

    log(
        f"[scraper] auto-test start · public={int(use_public)} manual={int(use_manual)} "
        f"max={max_c} workers={workers} timeout={timeout}s relay_cap={max_relay}"
    )
    result = start_batch_job(
        use_public=use_public,
        use_manual=use_manual,
        use_active=True,
        max_candidates=max_c,
        workers=workers,
        timeout=timeout,
        use_relay=True,
        max_relay=max_relay,
    )
    msg = result.get("message") or ""
    log(f"[scraper] auto-test: {msg}")
    return result


def scrape_to_files(
    *,
    catalog_file: Path | None = None,
    out_file: Path | None = None,
    report_file: Path | None = None,
    workers: int | None = None,
    timeout: int | None = None,
    use_github: bool | None = None,
    bootstrap_proxies: Iterable[str] = (),
    logger: Callable[[str], None] | None = None,
    auto_test: bool | None = None,
) -> dict:
    log = logger or print
    sources = load_catalog_lines(catalog_file)
    if use_github if use_github is not None else _env_bool("PROXY_SCRAPER_GITHUB", False):
        discovered = github_discover_raw_urls(logger=log)
        sources = list(dict.fromkeys(list(sources) + discovered))
    workers = workers if workers is not None else max(1, _env_int("PROXY_SCRAPER_WORKERS", 12))
    timeout = timeout if timeout is not None else max(3, _env_int("PROXY_SCRAPER_TIMEOUT", 15))
    log(f"[scraper] fetching {len(sources)} sources workers={workers}")
    candidates, fetches = scrape_sources(
        sources,
        workers=workers,
        timeout=timeout,
        bootstrap_proxies=bootstrap_proxies,
        logger=log,
    )
    out = out_file or Path(os.environ.get("PROXY_SCRAPER_OUT") or DEFAULT_OUT)
    if not out.is_absolute():
        out = PROJECT_ROOT / out
    report = report_file or Path(os.environ.get("PROXY_SCRAPER_REPORT") or DEFAULT_REPORT)
    if not report.is_absolute():
        report = PROJECT_ROOT / report
    n = write_candidates(out, candidates)
    write_report(report, fetches=fetches, candidates=n)
    log(f"[scraper] wrote {n} candidates → {out}")
    log(f"[scraper] report → {report}")
    auto = maybe_auto_test_after_scrape(logger=log, force=auto_test)
    return {
        "candidates": n,
        "out": str(out),
        "report": str(report),
        "sources_ok": sum(1 for f in fetches if f.ok),
        "sources_fail": sum(1 for f in fetches if not f.ok),
        "auto_test": auto,
    }


def merge_candidates_into_proxy_sources(candidates_file: Path | None = None) -> int:
    """Append high-signal raw subscription URLs into proxy-sources.txt (not every host:port)."""
    # We only merge *source URLs* that look like subscription feeds, not individual proxies.
    # Individual proxies go to scraper candidates and are tested by proxy_auto.
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scrape public free proxies / subscription nodes")
    sub = parser.add_subparsers(dest="cmd")

    p_scrape = sub.add_parser("scrape", help="Fetch catalog (+ optional GitHub) and write candidates")
    p_scrape.add_argument("--github", action="store_true", help="Also search GitHub code for raw lists")
    p_scrape.add_argument("--workers", type=int, default=None)
    p_scrape.add_argument("--timeout", type=int, default=None)
    p_scrape.add_argument("--out", type=str, default=None)
    p_scrape.add_argument("--catalog", type=str, default=None)
    p_scrape.add_argument(
        "--no-auto-test",
        action="store_true",
        help="Do not auto-start x.ai batch test after scrape",
    )
    p_scrape.add_argument(
        "--auto-test",
        action="store_true",
        help="Force auto-test after scrape (default when PROXY_SCRAPER_AUTO_TEST=1)",
    )

    sub.add_parser("sources", help="List built-in + local catalog URLs")

    p_test = sub.add_parser("extract-file", help="Extract candidates from a local file (debug)")
    p_test.add_argument("path")
    p_test.add_argument("--scheme", default="http")

    args = parser.parse_args(argv)
    cmd = args.cmd or "scrape"

    if cmd == "sources":
        for line in load_catalog_lines():
            print(line)
        return 0

    if cmd == "extract-file":
        text = Path(args.path).read_text(encoding="utf-8", errors="replace")
        cands = extract_from_body(text, default_scheme=args.scheme)
        for c in cands:
            print(c)
        print(f"# {len(cands)} candidates", file=sys.stderr)
        return 0

    # scrape
    out = Path(args.out) if getattr(args, "out", None) else None
    catalog = Path(args.catalog) if getattr(args, "catalog", None) else None
    auto_test = None
    if getattr(args, "no_auto_test", False):
        auto_test = False
    elif getattr(args, "auto_test", False):
        auto_test = True
    result = scrape_to_files(
        catalog_file=catalog,
        out_file=out,
        workers=getattr(args, "workers", None),
        timeout=getattr(args, "timeout", None),
        use_github=bool(getattr(args, "github", False)),
        logger=print,
        auto_test=auto_test,
    )
    print(
        f"[✓] sources_ok={result['sources_ok']} fail={result['sources_fail']} "
        f"candidates={result['candidates']}"
    )
    auto = result.get("auto_test") or {}
    if auto:
        print(f"[✓] auto-test: {auto.get('message') or auto}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
