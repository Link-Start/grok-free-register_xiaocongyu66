"""
内置 Turnstile Solver 进程管理。

把 vendor/turnstile-solver/{theyka,d3vin} 作为子进程启动，对外提供
与 Theyka / D3-vin 兼容的 REST API（/turnstile + /result）。

环境变量见 README / .env.example。
"""
from __future__ import annotations

import atexit
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

try:
    import requests
except ImportError:  # optional for health_check only
    requests = None  # type: ignore

PROJECT_ROOT = Path(__file__).resolve().parents[1]
VENDOR_ROOT = PROJECT_ROOT / "vendor" / "turnstile-solver"
DEFAULT_WORK_DIR = PROJECT_ROOT / "logs" / "turnstile-solver"

# Chrome-like UA for Theyka headless mode (Theyka requires UA when headless).
DEFAULT_HEADLESS_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

_managed_proc: Optional[subprocess.Popen] = None
_managed_meta: dict = {}
_atexit_registered = False


def _env_int(key: str, default: int) -> int:
    raw = str(os.environ.get(key, "")).strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_bool(key: str, default: bool = False) -> bool:
    raw = os.environ.get(key)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def resolve_solver_mode(raw: Optional[str] = None) -> str:
    """Normalize TURNSTILE_SOLVER → local | d3vin | theyka | hybrid | api.

    Default is d3vin (vendored D3-vin Turnstile-Solver-NEW).
    hybrid = Go gateway + Rust watchdog + C++ util + Python browser workers.
    """
    value = (raw if raw is not None else os.environ.get("TURNSTILE_SOLVER") or "hybrid")
    value = str(value).strip().lower()
    aliases = {
        "local": "local",
        "builtin": "local",
        "playwright": "local",
        "d3vin": "d3vin",
        "d3-vin": "d3vin",
        "new": "d3vin",
        "theyka": "theyka",
        "hybrid": "hybrid",
        "polyglot": "hybrid",
        "native": "hybrid",
        "gateway": "hybrid",
        "api": "api",
        "external": "api",
    }
    # unknown values fall back to d3vin (project default), not silent local
    return aliases.get(value, "d3vin")


def is_api_backend(mode: Optional[str] = None) -> bool:
    return resolve_solver_mode(mode) in ("api", "d3vin", "theyka", "hybrid")


def resolve_engine(mode: Optional[str] = None) -> str:
    """Which engine to run: hybrid | d3vin | theyka | external."""
    mode = resolve_solver_mode(mode)
    if mode in ("d3vin", "theyka", "hybrid"):
        return mode
    if mode != "api":
        return "external"
    engine = (os.environ.get("TURNSTILE_SOLVER_ENGINE") or "d3vin").strip().lower()
    if engine in ("hybrid", "polyglot", "native", "gateway"):
        return "hybrid"
    if engine in ("d3vin", "d3-vin", "new"):
        return "d3vin"
    if engine in ("theyka",):
        return "theyka"
    if engine in ("external", "none", "off"):
        return "external"
    return "d3vin"


def default_port_for_engine(engine: str) -> int:
    if engine == "theyka":
        return 5000
    if engine == "hybrid":
        return _env_int("SOLVER_GATEWAY_PORT", 5080)
    return 5072


def resolve_api_url(mode: Optional[str] = None) -> str:
    explicit = (os.environ.get("TURNSTILE_API_URL") or "").strip().rstrip("/")
    if explicit:
        return explicit
    engine = resolve_engine(mode)
    port = _env_int("TURNSTILE_SOLVER_PORT", default_port_for_engine(engine))
    host = (os.environ.get("TURNSTILE_SOLVER_HOST") or "127.0.0.1").strip()
    if host in ("0.0.0.0", "::"):
        host = "127.0.0.1"
    return f"http://{host}:{port}"


def should_manage_process(mode: Optional[str] = None) -> bool:
    mode = resolve_solver_mode(mode)
    if mode in ("d3vin", "theyka", "hybrid"):
        return _env_bool("TURNSTILE_API_MANAGED", True)
    if mode != "api":
        return False
    if resolve_engine(mode) == "external":
        return False
    # api + embedded engine: manage unless URL points off-box
    if not _env_bool("TURNSTILE_API_MANAGED", True):
        return False
    api_url = resolve_api_url(mode)
    host = (urlparse(api_url).hostname or "").lower()
    return host in ("127.0.0.1", "localhost", "::1")


def vendor_dir(engine: str) -> Path:
    return VENDOR_ROOT / engine


def hybrid_gateway_bin() -> Path:
    raw = (os.environ.get("SOLVER_GATEWAY_BIN") or "").strip()
    if raw:
        p = Path(raw).expanduser()
        return p if p.is_absolute() else PROJECT_ROOT / p
    return PROJECT_ROOT / "native" / "solver-gateway" / "solver-gateway"


def hybrid_worker_script() -> Path:
    raw = (os.environ.get("SOLVER_WORKER_SCRIPT") or "").strip()
    if raw:
        p = Path(raw).expanduser()
        return p if p.is_absolute() else PROJECT_ROOT / p
    return PROJECT_ROOT / "native" / "solver-hybrid" / "browser_worker.py"


def work_dir(engine: str) -> Path:
    base = Path(os.environ.get("TURNSTILE_SOLVER_WORK_DIR") or DEFAULT_WORK_DIR)
    if not base.is_absolute():
        base = PROJECT_ROOT / base
    path = base / engine
    path.mkdir(parents=True, exist_ok=True)
    return path


def required_python_modules(engine: str) -> tuple[str, ...]:
    common = ("quart", "patchright")
    if engine == "d3vin":
        return common + ("aiosqlite", "rich")
    return common


def missing_python_modules(engine: str) -> list[str]:
    missing = []
    for name in required_python_modules(engine):
        try:
            __import__(name)
        except Exception:
            missing.append(name)
    return missing


def install_python_deps(*, python: Optional[str] = None, quiet: bool = True) -> None:
    py = python or sys.executable
    req = VENDOR_ROOT / "requirements.txt"
    if not req.is_file():
        raise FileNotFoundError(f"missing {req}")
    cmd = [py, "-m", "pip", "install"]
    if quiet:
        cmd.append("-q")
    cmd.extend(["-r", str(req)])
    subprocess.check_call(cmd)


def ensure_patchright_browser(browser_type: str = "chromium", *, python: Optional[str] = None) -> None:
    py = python or sys.executable
    if browser_type == "camoufox":
        subprocess.check_call([py, "-m", "camoufox", "fetch"])
        return
    # patchright install chromium / msedge
    channel = "chromium" if browser_type in ("chromium", "chrome") else browser_type
    subprocess.check_call([py, "-m", "patchright", "install", channel])


def ensure_runtime_ready(engine: str, *, browser_type: str = "chromium", install: bool = True) -> None:
    if engine == "external":
        return
    if engine == "hybrid":
        bin_path = hybrid_gateway_bin()
        if not bin_path.is_file() or not os.access(bin_path, os.X_OK):
            raise FileNotFoundError(
                f"hybrid gateway missing: {bin_path}. Run: bash scripts/build-native.sh"
            )
        worker = hybrid_worker_script()
        if not worker.is_file():
            raise FileNotFoundError(f"hybrid browser worker missing: {worker}")
        # browser deps: patchright or playwright
        has_browser = False
        for mod in ("patchright", "playwright"):
            try:
                __import__(mod)
                has_browser = True
                break
            except Exception:
                pass
        if not has_browser:
            if not install:
                raise RuntimeError(
                    "hybrid solver needs patchright or playwright. "
                    "pip install patchright && python -m patchright install chromium"
                )
            try:
                install_python_deps()
            except Exception:
                pass
        marker = work_dir(engine) / f".browser-{browser_type}.ok"
        if not marker.is_file():
            try:
                ensure_patchright_browser(browser_type)
                marker.write_text("ok\n", encoding="utf-8")
            except Exception as exc:
                (work_dir(engine) / "browser-install-error.txt").write_text(
                    str(exc), encoding="utf-8"
                )
        return
    src = vendor_dir(engine)
    if not src.is_dir() or not (src / "api_solver.py").is_file():
        raise FileNotFoundError(f"vendored solver missing: {src}")
    missing = missing_python_modules(engine)
    if missing:
        if not install:
            raise RuntimeError(
                f"Turnstile solver deps missing: {', '.join(missing)}. "
                f"Run: pip install -r vendor/turnstile-solver/requirements.txt"
            )
        install_python_deps()
        missing = missing_python_modules(engine)
        if missing:
            raise RuntimeError(f"still missing after install: {', '.join(missing)}")
    if install and _env_bool("TURNSTILE_SOLVER_AUTO_BROWSER", True):
        marker = work_dir(engine) / f".browser-{browser_type}.ok"
        if not marker.is_file():
            try:
                ensure_patchright_browser(browser_type)
                marker.write_text(f"installed {time.strftime('%Y-%m-%d %H:%M:%S')}\n", encoding="utf-8")
            except Exception as exc:
                # Browser may already exist system-wide; continue and let solver fail loudly if not.
                (work_dir(engine) / "browser-install-error.txt").write_text(
                    f"{type(exc).__name__}: {exc}\n", encoding="utf-8"
                )


def _chromium_safe_proxy_lines(text: str) -> list[str]:
    """Keep only proxies Chromium/patchright can use (no SOCKS5 username/password)."""
    from urllib.parse import urlparse

    out = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            parsed = urlparse(line if "://" in line else f"http://{line}")
        except Exception:
            continue
        scheme = (parsed.scheme or "http").lower()
        # Chromium: HTTP/HTTPS ok; SOCKS5 only without auth
        if scheme in {"http", "https"}:
            out.append(line if "://" in line else f"http://{line}")
            continue
        if scheme in {"socks5", "socks5h", "socks4"} and not (parsed.username or parsed.password):
            out.append(line)
    # de-dupe
    seen = set()
    unique = []
    for item in out:
        if item in seen:
            continue
        seen.add(item)
        unique.append(item)
    return unique


def _sync_work_tree(engine: str) -> Path:
    """Copy vendor sources into a writable work dir (cwd for relative proxies/results)."""
    src = vendor_dir(engine)
    dst = work_dir(engine)
    for name in os.listdir(src):
        if name.startswith(".") or name in ("__pycache__", "results.json", "results.db", "proxies.txt"):
            continue
        s = src / name
        d = dst / name
        if s.is_dir():
            if d.exists():
                shutil.rmtree(d)
            shutil.copytree(s, d)
        else:
            shutil.copy2(s, d)
    # Always rebuild proxies.txt from project override (Chromium-safe only).
    # Never keep SOCKS5+auth lines — patchright raises:
    # "Browser does not support socks5 proxy authentication".
    project_proxies = PROJECT_ROOT / "turnstile-proxies.txt"
    candidates = []
    if project_proxies.is_file():
        candidates = _chromium_safe_proxy_lines(project_proxies.read_text(encoding="utf-8", errors="replace"))
    if not candidates:
        vendor_proxies = src / "proxies.txt"
        if vendor_proxies.is_file():
            candidates = _chromium_safe_proxy_lines(
                vendor_proxies.read_text(encoding="utf-8", errors="replace")
            )
    (dst / "proxies.txt").write_text(
        ("\n".join(candidates) + ("\n" if candidates else "")),
        encoding="utf-8",
    )
    return dst


def build_command(
    engine: str,
    *,
    host: str,
    port: int,
    browser_type: str,
    thread: int,
    headless: bool,
    debug: bool,
    proxy: bool,
    useragent: Optional[str],
    python: Optional[str] = None,
) -> list[str]:
    if engine == "hybrid":
        bin_path = hybrid_gateway_bin()
        soft = _env_int("SOLVER_WATCHDOG_SOFT_MB", 700)
        hard = _env_int("SOLVER_WATCHDOG_HARD_MB", 1100)
        max_solves = _env_int("SOLVER_WORKER_MAX_SOLVES", 8)
        timeout = _env_int("SOLVER_GATEWAY_TIMEOUT", _env_int("TURNSTILE_API_TIMEOUT", 90))
        # multi-core: SOLVER_GATEWAY_WORKERS=auto|N; threads env used as override when set
        workers_raw = (os.environ.get("SOLVER_GATEWAY_WORKERS") or "").strip()
        if not workers_raw:
            # TURNSTILE_SOLVER_THREADS>0 forces fixed count; else auto scale by CPU+RAM
            workers_raw = str(thread) if thread > 0 else "auto"
        conc = _env_int("SOLVER_WORKER_CONCURRENCY", 0)
        queue = _env_int("SOLVER_GATEWAY_QUEUE", 0)
        cmd = [
            str(bin_path),
            "--host",
            host,
            "--port",
            str(port),
            "--workers",
            workers_raw,
            "--timeout",
            str(timeout),
            "--soft-mb",
            str(soft),
            "--hard-mb",
            str(hard),
            "--max-solves",
            str(max_solves),
            "--browser",
            browser_type,
            "--work-dir",
            str(work_dir("hybrid")),
        ]
        if conc > 0:
            cmd.extend(["--concurrency", str(conc)])
        if queue > 0:
            cmd.extend(["--queue", str(queue)])
        if headless:
            cmd.append("--headless")
        if _env_bool("SOLVER_WORKER_PREFETCH", True):
            cmd.append("--prefetch")
        return cmd

    py = python or sys.executable
    cmd = [py, "api_solver.py", "--host", host, "--port", str(port), "--browser_type", browser_type, "--thread", str(thread)]
    if engine == "theyka":
        # Theyka uses type=bool argparse quirks: pass True/False strings.
        cmd.extend(["--headless", "True" if headless else "False"])
        cmd.extend(["--debug", "True" if debug else "False"])
        cmd.extend(["--proxy", "True" if proxy else "False"])
        ua = useragent or (DEFAULT_HEADLESS_UA if headless and browser_type != "camoufox" else None)
        if ua:
            cmd.extend(["--useragent", ua])
    else:
        # d3vin: headless by default; --no-headless disables it
        if not headless:
            cmd.append("--no-headless")
        if debug:
            cmd.append("--debug")
        if proxy:
            cmd.append("--proxy")
        if useragent:
            cmd.extend(["--useragent", useragent])
        if _env_bool("TURNSTILE_SOLVER_RANDOM_UA", False):
            cmd.append("--random")
    return cmd


def _port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def health_check(api_url: str, timeout: float = 2.0) -> bool:
    """True if solver HTTP is accepting connections (index or any response)."""
    if requests is not None:
        try:
            resp = requests.get(
                api_url.rstrip("/") + "/",
                timeout=timeout,
                proxies={"http": None, "https": None},
            )
            return resp.status_code < 500
        except Exception:
            try:
                resp = requests.get(
                    api_url.rstrip("/") + "/result?id=healthcheck-missing",
                    timeout=timeout,
                    proxies={"http": None, "https": None},
                )
                return resp.status_code in (200, 400, 404, 422)
            except Exception:
                pass
    # stdlib fallback (no requests)
    try:
        from urllib.request import urlopen, Request

        req = Request(api_url.rstrip("/") + "/", method="GET")
        with urlopen(req, timeout=timeout) as resp:
            return 100 <= getattr(resp, "status", 200) < 500
    except Exception:
        try:
            from urllib.request import urlopen, Request

            req = Request(
                api_url.rstrip("/") + "/result?id=healthcheck-missing", method="GET"
            )
            with urlopen(req, timeout=timeout) as resp:
                return getattr(resp, "status", 0) in (200, 400, 404, 422)
        except Exception:
            return False


def _register_atexit() -> None:
    global _atexit_registered
    if _atexit_registered:
        return
    atexit.register(stop_managed_solver)
    _atexit_registered = True


def stop_managed_solver(timeout: float = 8.0) -> None:
    global _managed_proc, _managed_meta
    proc = _managed_proc
    _managed_proc = None
    meta = dict(_managed_meta)
    _managed_meta = {}
    if proc is None:
        return
    if proc.poll() is not None:
        return
    try:
        if sys.platform != "win32":
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except Exception:
                proc.terminate()
        else:
            proc.terminate()
    except Exception:
        pass
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            break
        time.sleep(0.1)
    if proc.poll() is None:
        try:
            if sys.platform != "win32":
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except Exception:
                    proc.kill()
            else:
                proc.kill()
        except Exception:
            pass
    # best-effort: remove pid file
    pid_file = meta.get("pid_file")
    if pid_file:
        try:
            Path(pid_file).unlink(missing_ok=True)
        except Exception:
            pass


def start_managed_solver(
    *,
    mode: Optional[str] = None,
    log=print,
    install: bool = True,
) -> dict:
    """Start vendored solver if needed. Returns meta dict with api_url/engine/managed."""
    global _managed_proc, _managed_meta

    mode = resolve_solver_mode(mode)
    engine = resolve_engine(mode)
    api_url = resolve_api_url(mode)
    meta = {
        "mode": mode,
        "engine": engine,
        "api_url": api_url,
        "managed": False,
        "already_running": False,
        "pid": None,
    }

    if not should_manage_process(mode):
        return meta

    parsed = urlparse(api_url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or default_port_for_engine(engine)
    bind_host = (os.environ.get("TURNSTILE_SOLVER_HOST") or "127.0.0.1").strip()
    browser_type = (os.environ.get("TURNSTILE_SOLVER_BROWSER") or "chromium").strip().lower()
    thread = max(1, _env_int("TURNSTILE_SOLVER_THREADS", 2))
    headless = _env_bool("TURNSTILE_SOLVER_HEADLESS", True)
    debug = _env_bool("TURNSTILE_SOLVER_DEBUG", False)
    proxy = _env_bool("TURNSTILE_SOLVER_PROXY", False)
    useragent = (os.environ.get("TURNSTILE_SOLVER_USERAGENT") or "").strip() or None
    ready_timeout = max(5, _env_int("TURNSTILE_SOLVER_READY_TIMEOUT", 90))

    # Reuse existing healthy listener
    if health_check(api_url, timeout=1.5) or _port_open(host if host not in ("0.0.0.0",) else "127.0.0.1", port):
        if health_check(api_url, timeout=1.5):
            meta["already_running"] = True
            meta["managed"] = False
            log(f"[*] Turnstile solver 已在监听 {api_url}，复用现有进程")
            return meta

    ensure_runtime_ready(engine, browser_type=browser_type, install=install)
    if engine == "hybrid":
        cwd = work_dir("hybrid")
        # expose project root for gateway binary path resolution
        os.environ.setdefault("PROJECT_ROOT", str(PROJECT_ROOT))
    else:
        cwd = _sync_work_tree(engine)
    # hybrid: prefer multi-core auto workers; only pin if SOLVER_GATEWAY_WORKERS is numeric
    if engine == "hybrid":
        wenv = (os.environ.get("SOLVER_GATEWAY_WORKERS") or "auto").strip().lower()
        if wenv not in ("", "auto", "0") and wenv.isdigit():
            thread = max(1, int(wenv))
        # else leave thread as catalog default; build_command passes "auto"
    cmd = build_command(
        engine,
        host=bind_host,
        port=port,
        browser_type=browser_type,
        thread=thread,
        headless=headless,
        debug=debug,
        proxy=proxy,
        useragent=useragent,
    )

    log_path = cwd / "solver.log"
    pid_file = cwd / "solver.pid"
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PROJECT_ROOT"] = str(PROJECT_ROOT)
    env["SOLVER_WORKER_SCRIPT"] = str(hybrid_worker_script())
    # Browser worker MUST use project venv (patchright), not system python3
    if engine == "hybrid":
        venv_py = PROJECT_ROOT / ".venv" / "bin" / "python"
        solver_py = (os.environ.get("SOLVER_PYTHON") or "").strip()
        if solver_py:
            env["SOLVER_PYTHON"] = solver_py
        elif venv_py.is_file():
            env["SOLVER_PYTHON"] = str(venv_py)
        else:
            env["SOLVER_PYTHON"] = sys.executable
        env["PYTHON"] = env["SOLVER_PYTHON"]
        # Same Chromium as register (cloakbrowser)
        if not env.get("SOLVER_CHROME_PATH") and not env.get("CHROME_PATH"):
            try:
                import glob as _glob

                chromes = sorted(
                    _glob.glob(str(Path.home() / ".cloakbrowser" / "chromium-*" / "chrome")),
                    reverse=True,
                )
                if chromes:
                    env["SOLVER_CHROME_PATH"] = chromes[0]
            except Exception:
                pass
        env.setdefault("SOLVER_BROWSER_AUTO_PROXY", "0")
        env.setdefault("SOLVER_SOLVE_MODE", "inject")
        env.setdefault("SOLVER_PLAYWRIGHT_MOD", "playwright,patchright")
    # Isolate from project proxies for local control plane unless user wants them
    if not proxy:
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
            env.pop(key, None)

    log(f"[*] 启动内置 Turnstile solver ({engine}) → {api_url}")
    if engine == "hybrid":
        log(
            f"[*] hybrid stack: Go gateway + Rust watchdog + C++ util + Python browser "
            f"(workers={thread}, auto memory release)"
        )
    log_file = open(log_path, "ab", buffering=0)
    popen_kwargs = {
        "cwd": str(cwd),
        "env": env,
        "stdout": log_file,
        "stderr": subprocess.STDOUT,
        "stdin": subprocess.DEVNULL,
    }
    if sys.platform != "win32":
        popen_kwargs["start_new_session"] = True

    try:
        proc = subprocess.Popen(cmd, **popen_kwargs)
    except Exception:
        log_file.close()
        raise

    _managed_proc = proc
    _managed_meta = {
        "pid": proc.pid,
        "engine": engine,
        "api_url": api_url,
        "cwd": str(cwd),
        "log_path": str(log_path),
        "pid_file": str(pid_file),
        "log_file": log_file,
    }
    _register_atexit()
    try:
        pid_file.write_text(str(proc.pid), encoding="utf-8")
    except Exception:
        pass

    deadline = time.time() + ready_timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            tail = ""
            try:
                tail = log_path.read_text(encoding="utf-8", errors="replace")[-2000:]
            except Exception:
                pass
            stop_managed_solver()
            raise RuntimeError(
                f"Turnstile solver 进程退出 (code={proc.returncode}). "
                f"日志: {log_path}\n{tail}"
            )
        if health_check(api_url, timeout=1.0):
            meta["managed"] = True
            meta["pid"] = proc.pid
            meta["log_path"] = str(log_path)
            log(f"[✓] Turnstile solver 就绪 | engine={engine} pid={proc.pid}")
            return meta
        time.sleep(0.4)

    stop_managed_solver()
    raise TimeoutError(
        f"Turnstile solver 在 {ready_timeout}s 内未就绪: {api_url} (log={log_path})"
    )


def ensure_solver_for_register(*, log=print, force: bool = False) -> dict:
    """Entry used by register.main().

    On-demand policy (default):
      - TURNSTILE_SOLVER=local → never start external solver
      - TURNSTILE_API_MANAGED=0 → never auto-start
      - TURNSTILE_SOLVER_ON_DEMAND=1 (default) → only start if health_check fails
        or force=True (e.g. after create ConnectionError)
      - Otherwise start managed process as before
    """
    mode = resolve_solver_mode()
    if not is_api_backend(mode):
        return {"mode": mode, "engine": "local", "api_url": "", "managed": False}

    api_url = resolve_api_url(mode)
    on_demand = _env_bool("TURNSTILE_SOLVER_ON_DEMAND", True)

    # Already healthy → reuse, do not spawn
    if health_check(api_url, timeout=1.5):
        log(f"[*] Turnstile solver 已在监听 {api_url}，复用（按需模式）")
        return {
            "mode": mode,
            "engine": resolve_engine(mode),
            "api_url": api_url,
            "managed": False,
            "already_running": True,
            "on_demand": on_demand,
        }

    if not should_manage_process(mode):
        log(f"[!] Turnstile API 不可达且未托管: {api_url}")
        return {
            "mode": mode,
            "engine": resolve_engine(mode),
            "api_url": api_url,
            "managed": False,
            "on_demand": on_demand,
        }

    if on_demand and not force:
        # Defer start until first solve failure / explicit force
        log(
            f"[*] Turnstile 按需模式：暂不启动（{api_url} 未就绪）。"
            "首次求解失败时会自动拉起"
        )
        return {
            "mode": mode,
            "engine": resolve_engine(mode),
            "api_url": api_url,
            "managed": False,
            "deferred": True,
            "on_demand": True,
        }

    return start_managed_solver(mode=mode, log=log, install=True)


def ensure_solver_if_needed(*, log=print) -> dict:
    """Force-start managed solver when API is down (on-demand recovery)."""
    return ensure_solver_for_register(log=log, force=True)


def main(argv: Optional[list[str]] = None) -> int:
    """CLI: python -m grok_register.turnstile_solver [start|stop|status|install]."""
    args = list(argv if argv is not None else sys.argv[1:])
    action = (args[0] if args else "start").strip().lower()

    if action in ("-h", "--help", "help"):
        print(
            "Usage: python -m grok_register.turnstile_solver "
            "[start|stop|status|install]\n"
            "Env: TURNSTILE_SOLVER=d3vin|theyka  TURNSTILE_SOLVER_PORT=5072"
        )
        return 0

    if action == "install":
        engine = resolve_engine()
        if engine == "external":
            engine = "d3vin"
        browser = (os.environ.get("TURNSTILE_SOLVER_BROWSER") or "chromium").strip()
        print(f"[*] installing deps for engine={engine}")
        install_python_deps(quiet=False)
        ensure_patchright_browser(browser)
        print("[✓] done")
        return 0

    if action == "stop":
        # stop only our managed child; also try pid file
        for eng in ("d3vin", "theyka", "hybrid"):
            pid_path = work_dir(eng) / "solver.pid"
            if not pid_path.is_file():
                continue
            try:
                pid = int(pid_path.read_text().strip())
            except Exception:
                continue
            try:
                os.kill(pid, signal.SIGTERM)
                print(f"[*] sent SIGTERM to {eng} pid={pid}")
            except ProcessLookupError:
                pid_path.unlink(missing_ok=True)
            except Exception as exc:
                print(f"[!] stop {eng}: {exc}")
        stop_managed_solver()
        return 0

    if action == "status":
        mode = resolve_solver_mode()
        if mode == "local" and not args[1:]:
            # default status checks d3vin url
            os.environ.setdefault("TURNSTILE_SOLVER", "d3vin")
            mode = "d3vin"
        api_url = resolve_api_url(mode)
        ok = health_check(api_url)
        print(f"mode={mode} engine={resolve_engine(mode)} api={api_url} healthy={ok}")
        return 0 if ok else 1

    if action != "start":
        print(f"unknown action: {action}", file=sys.stderr)
        return 2

    # force managed engines for standalone start
    if resolve_solver_mode() == "local":
        os.environ["TURNSTILE_SOLVER"] = (
            os.environ.get("TURNSTILE_SOLVER_ENGINE") or "d3vin"
        )
    meta = start_managed_solver(log=print, install=True)
    print(meta)
    # keep foreground if we own the process
    proc = _managed_proc
    if proc is None:
        return 0
    try:
        return proc.wait()
    except KeyboardInterrupt:
        stop_managed_solver()
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
