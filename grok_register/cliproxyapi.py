"""
CLIProxyAPI auto-import + xAI OAuth token refresh.

CLIProxyAPI only recognizes single-account files (type=xai), e.g. xai-*.json.
Do NOT import accounts.cpa.json (type=cpa-auth-bundle) — it is not xAI auth.

Design:
  - Source of truth: keys/cpa/xai-*.json (local CPA singles)
  - Import mode A: copy into CLIPROXYAPI_AUTH_DIR (hot-load by file watcher)
  - Import mode B: POST /v0/management/auth-files when management secret is set
  - Refresh: POST token_endpoint grant_type=refresh_token + client_id (Grok CLI)
  - Background worker rewrites local files then re-imports after refresh

Reference: grok-build-auth xai_oauth.py (refresh + cliproxyapi record shape),
CLIProxyAPI internal/auth/xai (TokenStorage fields).
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from grok_register import job_store

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
DEFAULT_TOKEN_ENDPOINT = "https://auth.x.ai/oauth2/token"
DEFAULT_API_BASE = "https://api.x.ai/v1"
# Grok CLI / Build channel (preferred for grok-cli:access tokens)
GROK_CLI_BASE_URL = "https://cli-chat-proxy.grok.com/v1"
GROK_CLI_HEADERS = {
    "X-XAI-Token-Auth": "xai-grok-cli",
    "x-grok-client-version": "0.2.93",
    "x-grok-client-identifier": "grok-shell",
}

CLIPROXY_JOB = job_store.LOGS / "cliproxyapi-job.json"

_worker_lock = threading.Lock()
_worker_thread: threading.Thread | None = None
_worker_stop = threading.Event()
_last_run: dict[str, Any] = {}


def _env(key: str, default: str = "") -> str:
    return (os.environ.get(key) or default).strip()


def _env_bool(key: str, default: bool = False) -> bool:
    raw = _env(key)
    if not raw:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


def _env_int(key: str, default: int) -> int:
    raw = _env(key)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _project_path(raw: str, default: Path) -> Path:
    text = (raw or "").strip()
    if not text:
        return default
    p = Path(text).expanduser()
    return p if p.is_absolute() else PROJECT_ROOT / p


@dataclass
class CliproxyConfig:
    enabled: bool = True
    auto_import: bool = True
    auto_refresh: bool = True
    source_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "keys" / "cpa")
    auth_dir: Path = field(default_factory=lambda: Path("/root/CLIProxyAPI/auths"))
    base_url: str = "http://127.0.0.1:8317"
    management_secret: str = ""
    interval_sec: int = 300
    refresh_lead_sec: int = 300  # match CLIProxyAPI RefreshLead (5m)
    client_id: str = DEFAULT_CLIENT_ID
    prefer_grok_cli_base: bool = True
    timeout_sec: float = 25.0
    remove_bundle_files: bool = True
    import_via_api: bool = False  # True → management API; False → file copy

    @classmethod
    def from_env(cls) -> "CliproxyConfig":
        return cls(
            enabled=_env_bool("CLIPROXYAPI_ENABLED", True),
            auto_import=_env_bool("CLIPROXYAPI_AUTO_IMPORT", True),
            auto_refresh=_env_bool("CLIPROXYAPI_AUTO_REFRESH", True),
            source_dir=_project_path(
                _env("CLIPROXYAPI_SOURCE_DIR") or _env("CPA_SOURCE_DIR"),
                PROJECT_ROOT / "keys" / "cpa",
            ),
            auth_dir=_project_path(
                _env("CLIPROXYAPI_AUTH_DIR"),
                Path("/root/CLIProxyAPI/auths"),
            ),
            base_url=_env("CLIPROXYAPI_BASE_URL") or _env("XAI_ENROLLER_CPA_BASE_URL") or "http://127.0.0.1:8317",
            management_secret=_env("CLIPROXYAPI_MANAGEMENT_SECRET")
            or _env("XAI_ENROLLER_CPA_MANAGEMENT_SECRET")
            or _env("MANAGEMENT_PASSWORD")
            or "",
            interval_sec=max(30, _env_int("CLIPROXYAPI_INTERVAL_SEC", 300)),
            refresh_lead_sec=max(30, _env_int("CLIPROXYAPI_REFRESH_LEAD_SEC", 300)),
            client_id=_env("CLIPROXYAPI_CLIENT_ID") or DEFAULT_CLIENT_ID,
            prefer_grok_cli_base=_env_bool("CLIPROXYAPI_PREFER_GROK_CLI_BASE", True),
            timeout_sec=float(_env("CLIPROXYAPI_TIMEOUT_SEC") or "25"),
            remove_bundle_files=_env_bool("CLIPROXYAPI_REMOVE_BUNDLES", True),
            import_via_api=_env_bool("CLIPROXYAPI_IMPORT_VIA_API", False),
        )


def list_cpa_singles(directory: Path | None = None) -> list[Path]:
    """Return single-account xai-*.json files only (never accounts.cpa*.json)."""
    root = Path(directory or CliproxyConfig.from_env().source_dir).expanduser()
    if not root.is_dir():
        return []
    out: list[Path] = []
    for path in sorted(root.glob("xai-*.json")):
        if not path.is_file() or path.name.startswith("."):
            continue
        # Skip accidental bundle names
        if path.name.startswith("accounts."):
            continue
        out.append(path)
    return out


def _parse_expired(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if not text:
        return None
    # unix as string
    if text.isdigit():
        try:
            return datetime.fromtimestamp(int(text), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    # ISO-8601
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def needs_refresh(document: dict[str, Any], *, lead_sec: int = 300, now: datetime | None = None) -> bool:
    """True if access token is missing, past expired, or within lead window."""
    if not document.get("refresh_token"):
        return False
    now = now or datetime.now(timezone.utc)
    exp = _parse_expired(document.get("expired") or document.get("expires_at"))
    if exp is None:
        # No expiry recorded — refresh if no access_token
        return not bool(document.get("access_token"))
    return now >= (exp - timedelta(seconds=max(0, lead_sec)))


def _iso_utc(dt: datetime | None = None) -> str:
    dt = dt or datetime.now(timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _iso_from_unix(ts: Any) -> str:
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return _iso_utc()


def refresh_access_token(
    refresh_token: str,
    *,
    client_id: str = DEFAULT_CLIENT_ID,
    token_endpoint: str = DEFAULT_TOKEN_ENDPOINT,
    timeout: float = 25.0,
    proxy: str = "",
) -> dict[str, Any]:
    """Exchange refresh_token at auth.x.ai (form body, public client_id)."""
    if not refresh_token:
        raise ValueError("empty refresh_token")
    endpoint = (token_endpoint or DEFAULT_TOKEN_ENDPOINT).strip()
    if not endpoint.startswith("https://"):
        raise ValueError("token_endpoint must be https")
    body = urllib.parse.urlencode(
        {
            "grant_type": "refresh_token",
            "client_id": client_id or DEFAULT_CLIENT_ID,
            "refresh_token": refresh_token,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        endpoint,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        method="POST",
    )
    openers: list[Any] = []
    if proxy:
        # http(s) proxy only for urllib; socks needs external handler
        if proxy.startswith("http://") or proxy.startswith("https://"):
            openers.append(
                urllib.request.ProxyHandler({"http": proxy, "https": proxy})
            )
    opener = urllib.request.build_opener(*openers) if openers else urllib.request.build_opener()
    try:
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read()
            status = getattr(resp, "status", 200) or 200
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"refresh HTTP {exc.code}: {err_body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"refresh network error: {exc}") from exc
    if status // 100 != 2:
        raise RuntimeError(f"refresh HTTP {status}: {raw[:300]!r}")
    try:
        token = json.loads(raw.decode("utf-8"))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("refresh returned non-JSON") from exc
    if not isinstance(token, dict) or not token.get("access_token"):
        raise RuntimeError("refresh missing access_token")
    now = int(time.time())
    if "expires_in" in token and "expires_at" not in token:
        try:
            token["expires_at"] = now + int(token["expires_in"])
        except (TypeError, ValueError):
            pass
    if not token.get("refresh_token"):
        token["refresh_token"] = refresh_token
    return token


def apply_token_to_document(
    document: dict[str, Any],
    token: dict[str, Any],
    *,
    prefer_grok_cli_base: bool = True,
) -> dict[str, Any]:
    """Merge token response into a CLIProxyAPI/CPA single-account document."""
    out = dict(document)
    out["type"] = "xai"
    out["auth_kind"] = out.get("auth_kind") or "oauth"
    out["access_token"] = token.get("access_token") or out.get("access_token") or ""
    out["refresh_token"] = token.get("refresh_token") or out.get("refresh_token") or ""
    if token.get("id_token"):
        out["id_token"] = token["id_token"]
    out["token_type"] = token.get("token_type") or out.get("token_type") or "Bearer"
    if token.get("expires_in") is not None:
        try:
            out["expires_in"] = int(token["expires_in"])
        except (TypeError, ValueError):
            pass
    exp_at = token.get("expires_at")
    if exp_at is not None:
        out["expired"] = _iso_from_unix(exp_at)
    elif token.get("expires_in") is not None:
        try:
            out["expired"] = _iso_from_unix(int(time.time()) + int(token["expires_in"]))
        except (TypeError, ValueError):
            pass
    out["last_refresh"] = _iso_utc()
    out.setdefault("token_endpoint", DEFAULT_TOKEN_ENDPOINT)
    if prefer_grok_cli_base:
        # Prefer Grok CLI chat proxy for grok-cli:access (avoids api.x.ai 402)
        base = out.get("base_url") or ""
        if not base or "api.x.ai" in base:
            out["base_url"] = GROK_CLI_BASE_URL
            headers = dict(out.get("headers") or {})
            headers.update(GROK_CLI_HEADERS)
            out["headers"] = headers
    else:
        out.setdefault("base_url", DEFAULT_API_BASE)
    # drop revoked flag if present
    out.pop("disabled", None)
    out.pop("revoked", None)
    out.pop("refresh_error", None)
    return out


def _atomic_write_json(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(document, ensure_ascii=False, indent=2) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
        os.chmod(path, 0o600)
    finally:
        if os.path.exists(tmp_name):
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


def load_document(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    # Reject bundle documents
    if str(data.get("type") or "") in {"cpa-auth-bundle", "cpa_accounts"}:
        return None
    return data


def import_file_copy(src: Path, auth_dir: Path) -> Path:
    """Copy one xai-*.json into CLIProxyAPI auth-dir (file-watcher hot load)."""
    auth_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    dest = auth_dir / src.name
    # Skip if identical mtime+size
    if dest.is_file():
        try:
            s1, s2 = src.stat(), dest.stat()
            if s1.st_size == s2.st_size and abs(s1.st_mtime - s2.st_mtime) < 0.5:
                # still copy if content hash differs? keep simple: copy if newer
                if s1.st_mtime <= s2.st_mtime + 0.01:
                    return dest
        except OSError:
            pass
    shutil.copy2(src, dest)
    try:
        os.chmod(dest, 0o600)
    except OSError:
        pass
    return dest


def import_via_management_api(
    document: dict[str, Any],
    *,
    filename: str,
    base_url: str,
    management_secret: str,
    timeout: float = 25.0,
) -> None:
    """POST single auth document to CLIProxyAPI management API."""
    if not management_secret:
        raise ValueError("management secret required for API import")
    url = f"{base_url.rstrip('/')}/v0/management/auth-files?{urllib.parse.urlencode({'name': filename})}"
    raw = json.dumps(document, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=raw,
        headers={
            "Authorization": f"Bearer {management_secret}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if (getattr(resp, "status", 200) or 200) // 100 != 2:
                body = resp.read()[:300]
                raise RuntimeError(f"auth-files upload rejected: {body!r}")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:400]
        raise RuntimeError(f"auth-files HTTP {exc.code}: {body}") from exc


def remove_bundle_artifacts(auth_dir: Path) -> list[str]:
    """Delete accounts.cpa*.json from auth-dir so CLIProxyAPI never loads them."""
    removed: list[str] = []
    if not auth_dir.is_dir():
        return removed
    for path in auth_dir.glob("accounts.cpa*"):
        try:
            path.unlink()
            removed.append(path.name)
        except OSError:
            pass
    return removed


def refresh_document_file(
    path: Path,
    *,
    config: CliproxyConfig | None = None,
    force: bool = False,
    proxy: str = "",
) -> dict[str, Any]:
    """Refresh one local CPA file if needed; rewrite in place. Returns status dict."""
    cfg = config or CliproxyConfig.from_env()
    doc = load_document(path)
    if not doc:
        return {"ok": False, "path": str(path), "error": "unreadable_or_bundle", "refreshed": False}
    if not force and not needs_refresh(doc, lead_sec=cfg.refresh_lead_sec):
        return {
            "ok": True,
            "path": str(path),
            "refreshed": False,
            "skipped": True,
            "reason": "not_due",
            "email": doc.get("email"),
        }
    rt = str(doc.get("refresh_token") or "")
    if not rt:
        return {"ok": False, "path": str(path), "error": "no_refresh_token", "refreshed": False}
    try:
        token = refresh_access_token(
            rt,
            client_id=cfg.client_id,
            token_endpoint=str(doc.get("token_endpoint") or DEFAULT_TOKEN_ENDPOINT),
            timeout=cfg.timeout_sec,
            proxy=proxy,
        )
    except Exception as exc:
        # mark soft failure on disk for inventory
        failed = dict(doc)
        failed["refresh_error"] = str(exc)[:300]
        failed["last_refresh_attempt"] = _iso_utc()
        try:
            _atomic_write_json(path, failed)
        except OSError:
            pass
        return {
            "ok": False,
            "path": str(path),
            "error": str(exc)[:400],
            "refreshed": False,
            "email": doc.get("email"),
            "revoked": "revoked" in str(exc).lower() or "invalid_grant" in str(exc).lower(),
        }
    updated = apply_token_to_document(doc, token, prefer_grok_cli_base=cfg.prefer_grok_cli_base)
    _atomic_write_json(path, updated)
    return {
        "ok": True,
        "path": str(path),
        "refreshed": True,
        "email": updated.get("email"),
        "expired": updated.get("expired"),
    }


def import_one(
    path: Path,
    *,
    config: CliproxyConfig | None = None,
) -> dict[str, Any]:
    cfg = config or CliproxyConfig.from_env()
    doc = load_document(path)
    if not doc:
        return {"ok": False, "path": str(path), "error": "unreadable_or_bundle"}
    try:
        if cfg.import_via_api and cfg.management_secret:
            import_via_management_api(
                doc,
                filename=path.name,
                base_url=cfg.base_url,
                management_secret=cfg.management_secret,
                timeout=cfg.timeout_sec,
            )
            mode = "api"
            dest = f"{cfg.base_url}/v0/management/auth-files?name={path.name}"
        else:
            dest_path = import_file_copy(path, cfg.auth_dir)
            mode = "file"
            dest = str(dest_path)
        return {"ok": True, "path": str(path), "mode": mode, "dest": dest, "email": doc.get("email")}
    except Exception as exc:
        return {"ok": False, "path": str(path), "error": str(exc)[:400]}


def run_once(
    *,
    config: CliproxyConfig | None = None,
    refresh: bool | None = None,
    import_files: bool | None = None,
    force_refresh: bool = False,
    limit: int | None = None,
    proxy: str = "",
    logger: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """
    One full cycle:
      1) optional refresh of due/expired accounts
      2) import singles into CLIProxyAPI (file copy or management API)
      3) strip any accounts.cpa* bundle from auth-dir
    """
    cfg = config or CliproxyConfig.from_env()
    log = logger or (lambda _m: None)
    do_refresh = cfg.auto_refresh if refresh is None else refresh
    do_import = cfg.auto_import if import_files is None else import_files

    result: dict[str, Any] = {
        "ok": True,
        "started_at": time.time(),
        "source_dir": str(cfg.source_dir),
        "auth_dir": str(cfg.auth_dir),
        "refreshed": 0,
        "refresh_skipped": 0,
        "refresh_failed": 0,
        "imported": 0,
        "import_failed": 0,
        "revoked": 0,
        "removed_bundles": [],
        "files": 0,
        "details": [],
        "message": "",
    }
    if not cfg.enabled and refresh is None and import_files is None:
        result["ok"] = False
        result["message"] = "CLIPROXYAPI_ENABLED=0"
        _write_job_status(result, running=False)
        return result

    paths = list_cpa_singles(cfg.source_dir)
    if limit is not None and limit > 0:
        paths = paths[: int(limit)]
    result["files"] = len(paths)
    log(f"[cliproxyapi] cycle files={len(paths)} refresh={do_refresh} import={do_import}")

    for path in paths:
        item: dict[str, Any] = {"file": path.name}
        if do_refresh:
            rr = refresh_document_file(path, config=cfg, force=force_refresh, proxy=proxy)
            item["refresh"] = rr
            if rr.get("refreshed"):
                result["refreshed"] += 1
            elif rr.get("skipped"):
                result["refresh_skipped"] += 1
            elif not rr.get("ok"):
                result["refresh_failed"] += 1
                if rr.get("revoked"):
                    result["revoked"] += 1
        if do_import:
            # only import if refresh ok or skipped / not attempted
            can_import = True
            if do_refresh and item.get("refresh") and not item["refresh"].get("ok") and not item["refresh"].get("skipped"):
                # still try import of old file if not revoked hard? skip revoked
                if item["refresh"].get("revoked"):
                    can_import = False
            if can_import:
                ir = import_one(path, config=cfg)
                item["import"] = ir
                if ir.get("ok"):
                    result["imported"] += 1
                else:
                    result["import_failed"] += 1
            else:
                item["import"] = {"ok": False, "skipped": True, "reason": "revoked"}
        result["details"].append(item)

    if cfg.remove_bundle_files:
        result["removed_bundles"] = remove_bundle_artifacts(cfg.auth_dir)

    result["finished_at"] = time.time()
    result["message"] = (
        f"files={result['files']} refreshed={result['refreshed']} "
        f"import={result['imported']} rev={result['revoked']} "
        f"rfail={result['refresh_failed']}"
    )
    if result["refresh_failed"] and not result["refreshed"] and not result["imported"]:
        result["ok"] = result["files"] == 0 or result["refresh_skipped"] > 0
    global _last_run
    _last_run = dict(result)
    _write_job_status(result, running=False)
    log(f"[cliproxyapi] {result['message']}")
    return result


def _write_job_status(result: dict[str, Any], *, running: bool) -> None:
    try:
        job_store.atomic_write_json(
            CLIPROXY_JOB,
            {
                "kind": "cliproxyapi",
                "running": running,
                "ok": result.get("ok"),
                "message": result.get("message") or "",
                "files": result.get("files") or 0,
                "refreshed": result.get("refreshed") or 0,
                "imported": result.get("imported") or 0,
                "refresh_failed": result.get("refresh_failed") or 0,
                "revoked": result.get("revoked") or 0,
                "auth_dir": result.get("auth_dir") or "",
                "source_dir": result.get("source_dir") or "",
                "started_at": result.get("started_at") or 0,
                "finished_at": result.get("finished_at") or 0,
                "updated_at": time.time(),
            },
        )
    except Exception:
        pass


def job_status() -> dict[str, Any]:
    raw = job_store.read_json(CLIPROXY_JOB)
    running = bool(raw.get("running"))
    # worker thread alive?
    global _worker_thread
    worker_alive = bool(_worker_thread and _worker_thread.is_alive())
    if running and not worker_alive and not raw.get("pid"):
        running = False
    return {
        "kind": "cliproxyapi",
        "running": running or worker_alive,
        "worker_alive": worker_alive,
        "enabled": CliproxyConfig.from_env().enabled,
        "ok": raw.get("ok"),
        "message": raw.get("message") or "",
        "files": raw.get("files") or 0,
        "refreshed": raw.get("refreshed") or 0,
        "imported": raw.get("imported") or 0,
        "refresh_failed": raw.get("refresh_failed") or 0,
        "revoked": raw.get("revoked") or 0,
        "auth_dir": raw.get("auth_dir") or str(CliproxyConfig.from_env().auth_dir),
        "source_dir": raw.get("source_dir") or str(CliproxyConfig.from_env().source_dir),
        "updated_at": raw.get("updated_at") or 0,
        "last_run": _last_run if _last_run else None,
    }


def start_worker(*, config: CliproxyConfig | None = None) -> dict[str, Any]:
    """Start background daemon: periodic refresh + import."""
    global _worker_thread
    cfg = config or CliproxyConfig.from_env()
    if not cfg.enabled:
        return {"ok": False, "message": "CLIPROXYAPI_ENABLED=0"}
    with _worker_lock:
        if _worker_thread and _worker_thread.is_alive():
            return {"ok": True, "message": "worker already running", "running": True}
        _worker_stop.clear()

        def _loop():
            while not _worker_stop.is_set():
                try:
                    run_once(config=CliproxyConfig.from_env())
                except Exception as exc:
                    _write_job_status(
                        {
                            "ok": False,
                            "message": f"worker error: {exc}",
                            "started_at": time.time(),
                            "finished_at": time.time(),
                        },
                        running=True,
                    )
                interval = max(30, CliproxyConfig.from_env().interval_sec)
                if _worker_stop.wait(interval):
                    break

        _worker_thread = threading.Thread(target=_loop, name="cliproxyapi-sync", daemon=True)
        _worker_thread.start()
        job_store.atomic_write_json(
            CLIPROXY_JOB,
            {
                "kind": "cliproxyapi",
                "running": True,
                "message": "worker started",
                "auth_dir": str(cfg.auth_dir),
                "source_dir": str(cfg.source_dir),
                "started_at": time.time(),
                "updated_at": time.time(),
            },
        )
    return {"ok": True, "message": f"worker started interval={cfg.interval_sec}s", "running": True}


def stop_worker() -> dict[str, Any]:
    global _worker_thread
    _worker_stop.set()
    t = _worker_thread
    if t and t.is_alive():
        t.join(timeout=5)
    _worker_thread = None
    job_store.atomic_write_json(
        CLIPROXY_JOB,
        {
            "kind": "cliproxyapi",
            "running": False,
            "message": "worker stopped",
            "finished_at": time.time(),
            "updated_at": time.time(),
        },
    )
    return {"ok": True, "message": "worker stopped", "running": False}


def ensure_worker_if_enabled() -> dict[str, Any]:
    """Called from dashboard startup — start background sync when enabled."""
    cfg = CliproxyConfig.from_env()
    if not cfg.enabled:
        return {"ok": True, "message": "disabled", "running": False}
    if not cfg.auto_import and not cfg.auto_refresh:
        return {"ok": True, "message": "auto import/refresh off", "running": False}
    return start_worker(config=cfg)


def main(argv: list[str] | None = None) -> int:
    import argparse

    p = argparse.ArgumentParser(description="CLIProxyAPI import + xAI token refresh")
    p.add_argument("--once", action="store_true", help="run one cycle and exit")
    p.add_argument("--worker", action="store_true", help="start background worker")
    p.add_argument("--refresh-only", action="store_true")
    p.add_argument("--import-only", action="store_true")
    p.add_argument("--force-refresh", action="store_true")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--proxy", default="")
    args = p.parse_args(argv)
    if args.worker:
        print(json.dumps(start_worker(), ensure_ascii=False))
        # keep process alive
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            print(json.dumps(stop_worker(), ensure_ascii=False))
        return 0
    refresh = True
    import_files = True
    if args.refresh_only:
        import_files = False
    if args.import_only:
        refresh = False
    result = run_once(
        refresh=refresh,
        import_files=import_files,
        force_refresh=args.force_refresh,
        limit=args.limit or None,
        proxy=args.proxy,
        logger=print,
    )
    print(json.dumps({k: v for k, v in result.items() if k != "details"}, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") or result.get("imported") or result.get("refreshed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
