"""
One-click convert accounts to CPA / sub2api product formats.

Paths:
  1) Already has OAuth (cpa or sub2api) → transform to the other format (no browser)
  2) Only SSO (accounts.txt / auth-sessions) → xai_enroller OAuth enrollment
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

from grok_register.account_inventory import (
    ensure_bundles,
    key_export_dir,
    scan_accounts,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
JOB_STATE_PATH = PROJECT_ROOT / "logs" / "account-convert-job.json"

_job_lock = threading.Lock()
_job_state: dict[str, Any] = {
    "running": False,
    "started_at": 0,
    "finished_at": 0,
    "formats": [],
    "total": 0,
    "done": 0,
    "ok": 0,
    "fail": 0,
    "skipped": 0,
    "message": "",
    "results": [],
    "error": "",
    "updated_at": 0,
}


def _job_path() -> Path:
    raw = (os.environ.get("ACCOUNT_CONVERT_JOB_FILE") or "").strip()
    if raw:
        p = Path(raw).expanduser()
        return p if p.is_absolute() else PROJECT_ROOT / p
    return JOB_STATE_PATH


def _persist_job_unlocked() -> None:
    path = _job_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = dict(_job_state)
        payload["updated_at"] = time.time()
        # don't dump huge result lists forever
        results = payload.get("results") or []
        if isinstance(results, list) and len(results) > 100:
            payload["results"] = results[:100]
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass


def _hydrate_job() -> None:
    with _job_lock:
        if _job_state.get("running"):
            return
        path = _job_path()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return
        if not isinstance(data, dict):
            return
        mem_has = bool(_job_state.get("started_at") or _job_state.get("message"))
        if data.get("running") and not mem_has:
            data = dict(data)
            data["running"] = False
            data["message"] = (data.get("message") or "") + " · 已恢复上次进度"
            data.setdefault("finished_at", time.time())
        if not mem_has or float(data.get("updated_at") or 0) >= float(_job_state.get("updated_at") or 0):
            _job_state.update({k: data.get(k, _job_state.get(k)) for k in _job_state.keys()})
            if "results" in data:
                _job_state["results"] = data.get("results") or []


def job_status() -> dict[str, Any]:
    _hydrate_job()
    with _job_lock:
        out = dict(_job_state)
        out["state_file"] = str(_job_path())
        return out


def _set_job(**kwargs) -> None:
    with _job_lock:
        _job_state.update(kwargs)
        _job_state["updated_at"] = time.time()
        _persist_job_unlocked()


def _atomic_write_private_json(path: Path, document: Any) -> None:
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    payload = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent), text=True
    )
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
        os.chmod(path, 0o600)
    finally:
        if os.path.exists(temporary_name):
            try:
                os.unlink(temporary_name)
            except OSError:
                pass


def _load_or_create_salt(root: Path) -> bytes:
    configured = os.environ.get("XAI_ENROLLER_SOURCE_SALT")
    if configured:
        return configured.encode()
    path = root / ".xai-enroller-salt"
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        value = path.read_text(encoding="utf-8").strip()
        if value:
            return value.encode()
    except OSError:
        pass
    value = secrets.token_urlsafe(32)
    fd, tmp = tempfile.mkstemp(prefix=".salt.", suffix=".tmp", dir=str(path.parent), text=True)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(value + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
    return value.encode()


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _load_auth_sessions(root: Path) -> dict[str, dict]:
    """email -> {cookies, browser_fingerprint_id, sso}"""
    out: dict[str, dict] = {}
    path = root / "auth-sessions.jsonl"
    if not path.is_file():
        return out
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                doc = json.loads(line)
            except json.JSONDecodeError:
                continue
            email = str(doc.get("email") or "").strip()
            if not email:
                continue
            cookies = doc.get("cookies") if isinstance(doc.get("cookies"), list) else []
            sso = ""
            for c in cookies:
                if isinstance(c, dict) and c.get("name") == "sso" and c.get("value"):
                    sso = str(c["value"])
                    break
            out[email] = {
                "cookies": cookies,
                "browser_fingerprint_id": doc.get("browser_fingerprint_id"),
                "sso": sso,
            }
    except OSError:
        pass
    return out


def _load_legacy_accounts(root: Path) -> dict[str, dict]:
    """email -> {password, sso}"""
    out: dict[str, dict] = {}
    path = root / "accounts.txt"
    if not path.is_file():
        return out
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(":")
            if len(parts) < 3:
                continue
            email, password, sso = parts[0].strip(), parts[1].strip(), parts[2].strip()
            if email and sso:
                out[email] = {"password": password, "sso": sso}
    except OSError:
        pass
    return out


def _load_cpa_doc(root: Path, email: str) -> dict | None:
    cpa_dir = root / "cpa"
    if not cpa_dir.is_dir():
        return None
    for path in cpa_dir.glob("xai-*.json"):
        doc = _read_json(path)
        if not isinstance(doc, dict):
            continue
        if str(doc.get("email") or "").strip().lower() == email.lower():
            return doc
    return None


def _load_sub2api_item(root: Path, email: str) -> dict | None:
    sub_dir = root / "sub2api"
    if not sub_dir.is_dir():
        return None
    for path in sorted(sub_dir.glob("*.sub2api.json")):
        if path.name == "accounts.sub2api.json":
            continue
        doc = _read_json(path)
        if not isinstance(doc, dict):
            continue
        for item in doc.get("accounts") or []:
            if not isinstance(item, dict):
                continue
            creds = item.get("credentials") or {}
            extra = item.get("extra") or {}
            e = str(
                creds.get("email") or extra.get("email") or item.get("name") or ""
            ).strip()
            if e.lower() == email.lower():
                return item
    return None


def _cpa_from_sub2api_item(item: dict) -> dict:
    creds = item.get("credentials") or {}
    extra = item.get("extra") or {}
    email = str(creds.get("email") or extra.get("email") or item.get("name") or "")
    return {
        "type": "xai",
        "access_token": creds.get("access_token"),
        "refresh_token": creds.get("refresh_token"),
        "id_token": creds.get("id_token"),
        "token_type": creds.get("token_type") or "Bearer",
        "expires_in": creds.get("expires_in"),
        "expired": creds.get("expires_at") or creds.get("expired"),
        "last_refresh": extra.get("last_refresh") or creds.get("last_refresh"),
        "sub": extra.get("subject") or creds.get("sub"),
        "base_url": creds.get("base_url") or "https://api.x.ai/v1",
        "token_endpoint": creds.get("token_endpoint") or "https://auth.x.ai/oauth2/token",
        "auth_kind": "oauth",
        "email": email,
    }


def _sub2api_from_cpa_doc(doc: dict) -> dict:
    email = str(doc.get("email") or doc.get("name") or "")
    from xai_enroller.protocol import XAIProfile

    profile = XAIProfile.default()
    credentials = {
        "access_token": doc.get("access_token"),
        "refresh_token": doc.get("refresh_token"),
        "expires_at": doc.get("expired") or doc.get("expires_at"),
        "client_id": profile.client_id,
        "scope": profile.scope,
        "email": email,
        "base_url": doc.get("base_url") or "https://api.x.ai/v1",
    }
    if doc.get("id_token"):
        credentials["id_token"] = doc.get("id_token")
    if doc.get("token_type"):
        credentials["token_type"] = doc.get("token_type")
    return {
        "name": email or "grok-account",
        "platform": "grok",
        "type": "oauth",
        "concurrency": 10,
        "priority": 1,
        "credentials": credentials,
        "extra": {
            "email": email,
            "subject": doc.get("sub"),
            "last_refresh": doc.get("last_refresh"),
        },
    }


def _filename_for_email(email: str, name_secret: bytes) -> str:
    import hashlib
    import hmac

    digest = hmac.new(name_secret, email.encode(), hashlib.sha256).hexdigest()[:16]
    return f"xai-{digest}"


def _write_cpa(root: Path, email: str, doc: dict, name_secret: bytes) -> Path:
    """Write single-account xai-*.json only (never accounts.cpa.json)."""
    directory = root / "cpa"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        from xai_enroller.sinks import purge_cpa_bundles

        purge_cpa_bundles(directory)
    except Exception:
        for bad in ("accounts.cpa.json", "accounts.cpa.zip"):
            p = directory / bad
            try:
                if p.is_file():
                    p.unlink()
            except OSError:
                pass
    sub = str(doc.get("sub") or email)
    import hashlib
    import hmac

    digest = hmac.new(name_secret, sub.encode(), hashlib.sha256).hexdigest()[:16]
    path = directory / f"xai-{digest}.json"
    payload = dict(doc)
    payload["email"] = email
    _atomic_write_private_json(path, payload)
    return path


def _write_sub2api(root: Path, email: str, account: dict, name_secret: bytes) -> Path:
    directory = root / "sub2api"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    creds = account.get("credentials") or {}
    subject = str((account.get("extra") or {}).get("subject") or email)
    import hashlib
    import hmac

    digest = hmac.new(name_secret, subject.encode(), hashlib.sha256).hexdigest()[:16]
    path = directory / f"xai-{digest}.sub2api.json"
    doc = {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "proxies": [],
        "accounts": [account],
    }
    _atomic_write_private_json(path, doc)
    # refresh bundle
    accounts = []
    seen = set()
    for p in sorted(directory.glob("*.sub2api.json")):
        if p.name == "accounts.sub2api.json":
            continue
        d = _read_json(p)
        if not isinstance(d, dict):
            continue
        for item in d.get("accounts") or []:
            if not isinstance(item, dict):
                continue
            c = item.get("credentials") or {}
            key = (c.get("refresh_token"), c.get("access_token"), item.get("name"))
            if key in seen:
                continue
            seen.add(key)
            accounts.append(item)
    _atomic_write_private_json(
        directory / "accounts.sub2api.json",
        {
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "proxies": [],
            "accounts": accounts,
        },
    )
    return path


def convert_oauth_copy(email: str, formats: Iterable[str], root: Path | None = None) -> dict:
    """Convert using existing OAuth files only (no browser)."""
    root = root or key_export_dir()
    formats = {f.strip().lower() for f in formats if f}
    formats &= {"cpa", "sub2api"}
    if not formats:
        return {"ok": False, "email": email, "error": "no formats"}

    name_secret = _load_or_create_salt(root)
    written = []
    cpa_doc = _load_cpa_doc(root, email)
    sub_item = _load_sub2api_item(root, email)

    if "cpa" in formats:
        if cpa_doc:
            written.append("cpa:already")
        elif sub_item:
            doc = _cpa_from_sub2api_item(sub_item)
            if not doc.get("access_token") and not doc.get("refresh_token"):
                return {"ok": False, "email": email, "error": "sub2api missing tokens"}
            path = _write_cpa(root, email, doc, name_secret)
            written.append(f"cpa:{path.name}")
        else:
            return {"ok": False, "email": email, "error": "no oauth source for cpa", "need_sso": True}

    if "sub2api" in formats:
        if sub_item:
            written.append("sub2api:already")
        elif cpa_doc:
            item = _sub2api_from_cpa_doc(cpa_doc)
            path = _write_sub2api(root, email, item, name_secret)
            written.append(f"sub2api:{path.name}")
        else:
            # maybe we just wrote cpa above
            cpa_doc2 = _load_cpa_doc(root, email)
            if cpa_doc2:
                item = _sub2api_from_cpa_doc(cpa_doc2)
                path = _write_sub2api(root, email, item, name_secret)
                written.append(f"sub2api:{path.name}")
            else:
                return {"ok": False, "email": email, "error": "no oauth source for sub2api", "need_sso": True}

    return {"ok": True, "email": email, "method": "oauth_copy", "written": written}


async def _enroll_one(
    email: str,
    sso: str,
    cookies: list,
    browser_fingerprint_id: str | None,
    formats: set[str],
    root: Path,
    name_secret: bytes,
) -> dict:
    import httpx
    from xai_enroller.coordinator import EnrollmentCoordinator
    from xai_enroller.executors import PlaywrightExecutor
    from xai_enroller.models import SourceRecord, SinkReceipt
    from xai_enroller.protocol import XAIProfile, XAIProtocol
    from xai_enroller.sinks import LocalAuthFileSink

    # local helpers mirrored from register export sink
    class _Sink:
        def __init__(self):
            self.email = email
            self.formats = formats
            self.name_secret = name_secret

        async def store(self, credential):
            fingerprints = []
            if "cpa" in self.formats:
                receipt = await LocalAuthFileSink(
                    root / "cpa",
                    name_secret=self.name_secret,
                    email=self.email,
                ).store(credential)
                fingerprints.append(receipt.fingerprint)
            if "sub2api" in self.formats:
                # reuse register helper via dynamic import of storage shape
                from grok_register.register import (
                    _sub2api_account_from_credential,
                    _sub2api_document,
                )
                from xai_enroller.sinks import credential_filename

                account = _sub2api_account_from_credential(self.email, credential)
                filename = credential_filename(credential, self.name_secret).removesuffix(".json")
                directory = root / "sub2api"
                directory.mkdir(mode=0o700, parents=True, exist_ok=True)
                account_path = directory / f"{filename}.sub2api.json"
                _atomic_write_private_json(account_path, _sub2api_document([account]))
                fingerprints.append(filename)
            return SinkReceipt(",".join(fingerprints) or "no-output")

    class _Source:
        def __init__(self, record):
            self.record = record

        def records(self):
            yield self.record

    # chrome path
    try:
        from grok_register.register import find_chrome, _pick_grok_proxy, _playwright_proxy

        chrome = find_chrome()
        proxy = _pick_grok_proxy()
        pw_proxy = _playwright_proxy(proxy)
    except Exception:
        chrome = None
        proxy = None
        pw_proxy = None

    record = SourceRecord(
        email,
        sso,
        tuple(cookies or ()),
        browser_fingerprint_id,
    )
    timeout = max(30, int(os.environ.get("KEY_EXPORT_ENROLLER_TIMEOUT") or "1800"))
    poll = max(1, int(os.environ.get("KEY_EXPORT_ENROLLER_POLL_SEC") or "5"))
    retries = min(3, max(0, int(os.environ.get("KEY_EXPORT_ENROLLER_RETRY_ATTEMPTS") or "0")))

    client_kwargs = {}
    if proxy and urlparse(proxy).scheme.lower() in {"http", "https"}:
        client_kwargs["proxy"] = proxy
    client = httpx.AsyncClient(**client_kwargs)
    try:
        coordinator = EnrollmentCoordinator(
            source=_Source(record),
            protocol=XAIProtocol(
                client,
                XAIProfile.default(),
                default_poll_interval=poll,
            ),
            executor=PlaywrightExecutor(
                concurrency=1,
                executable_path=chrome,
                proxy=pw_proxy,
            ),
            sink=_Sink(),
            ledger_path=root / "xai-enroller-ledger.db",
            ledger_salt=name_secret,
            concurrency=1,
            timeout=timeout,
            retry_attempts=retries,
        )
        results = await coordinator.run(target=1)
        result = results[0] if results else None
        if result is not None and getattr(result.status, "value", None) == "imported":
            return {
                "ok": True,
                "email": email,
                "method": "enroller",
                "status": "imported",
            }
        status = getattr(getattr(result, "status", None), "value", None) if result else "empty"
        reason = getattr(result, "reason_code", None) if result else None
        return {
            "ok": False,
            "email": email,
            "method": "enroller",
            "status": status,
            "error": reason or status or "enroll failed",
        }
    finally:
        await client.aclose()


def convert_account(
    email: str,
    formats: Iterable[str],
    *,
    root: Path | None = None,
    allow_enroll: bool = True,
) -> dict:
    """Convert one account to requested formats."""
    root = root or key_export_dir()
    formats = {f.strip().lower() for f in formats if f}
    formats &= {"cpa", "sub2api"}
    if not formats:
        return {"ok": False, "email": email, "error": "formats must include cpa and/or sub2api"}

    email = email.strip()
    # 1) try pure OAuth copy/transform first
    copy_result = convert_oauth_copy(email, formats, root)
    if copy_result.get("ok"):
        return copy_result
    if not copy_result.get("need_sso") and not allow_enroll:
        return copy_result

    if not allow_enroll:
        return {
            "ok": False,
            "email": email,
            "error": copy_result.get("error") or "need sso enrollment",
        }

    # 2) SSO enrollment
    sessions = _load_auth_sessions(root)
    legacy = _load_legacy_accounts(root)
    sso = ""
    cookies: list = []
    fp = None
    if email in sessions:
        sso = sessions[email].get("sso") or ""
        cookies = sessions[email].get("cookies") or []
        fp = sessions[email].get("browser_fingerprint_id")
    if not sso and email in legacy:
        sso = legacy[email].get("sso") or ""
    if not sso:
        return {
            "ok": False,
            "email": email,
            "error": "no SSO session for enrollment (accounts.txt / auth-sessions.jsonl)",
        }
    if not cookies and sso:
        cookies = [
            {
                "name": "sso",
                "value": sso,
                "domain": "accounts.x.ai",
                "path": "/",
                "secure": True,
                "httpOnly": True,
                "sameSite": "Lax",
            }
        ]

    name_secret = _load_or_create_salt(root)
    try:
        return asyncio.run(
            _enroll_one(email, sso, cookies, fp, formats, root, name_secret)
        )
    except RuntimeError:
        # nested event loop
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(
                _enroll_one(email, sso, cookies, fp, formats, root, name_secret)
            )
        finally:
            loop.close()
    except Exception as exc:
        return {"ok": False, "email": email, "error": str(exc)[:300]}


def convert_accounts(
    emails: list[str] | None,
    formats: Iterable[str],
    *,
    only_pending: bool = False,
    allow_enroll: bool = True,
    rebuild: bool = True,
    limit: int = 50,
) -> dict:
    """
    Convert many accounts.
    emails=None → all accounts (optionally only oauth_pending).
    """
    root = key_export_dir()
    formats = [f.strip().lower() for f in formats if f and f.strip().lower() in {"cpa", "sub2api"}]
    if not formats:
        return {"ok": False, "message": "formats must include cpa and/or sub2api", "results": []}

    records = scan_accounts(root)
    if emails:
        want = {e.strip().lower() for e in emails if e and e.strip()}
        records = [r for r in records if r.email.lower() in want]
    elif only_pending:
        records = [r for r in records if r.status in {"oauth_pending", "legacy_sso"}]
    else:
        # prefer accounts missing at least one requested format
        filtered = []
        for r in records:
            missing = [f for f in formats if f not in r.formats]
            if missing:
                filtered.append(r)
        records = filtered

    records = records[: max(1, min(int(limit or 50), 200))]
    results = []
    ok = fail = skipped = 0
    for rec in records:
        missing = [f for f in formats if f not in rec.formats]
        if not missing and emails is None:
            skipped += 1
            results.append({"ok": True, "email": rec.email, "skipped": True, "reason": "already has formats"})
            continue
        target_formats = missing if (emails is None and missing) else formats
        # if user selected specific emails, convert all requested formats
        if emails:
            target_formats = formats
        r = convert_account(rec.email, target_formats, root=root, allow_enroll=allow_enroll)
        results.append(r)
        if r.get("ok"):
            ok += 1
        else:
            fail += 1

    bundles = {}
    if rebuild:
        try:
            bundles = ensure_bundles(rebuild=True)
        except Exception as exc:
            bundles = {"error": str(exc)}

    need_sso = sum(1 for r in results if r.get("need_sso") or "SSO" in str(r.get("error") or ""))
    msg = f"转换完成：成功 {ok} · 失败 {fail} · 跳过 {skipped}"
    if need_sso and fail:
        msg += f"（其中 {need_sso} 个需 SSO→OAuth，请保持 allow_enroll 或点「仅转换待 OAuth」）"
    return {
        "ok": fail == 0 and (ok + skipped) > 0,
        "message": msg,
        "ok_count": ok,
        "fail_count": fail,
        "skipped": skipped,
        "total": len(results),
        "formats": formats,
        "results": results,
        "bundles": bundles,
    }


def start_convert_job(
    emails: list[str] | None,
    formats: Iterable[str],
    *,
    only_pending: bool = False,
    allow_enroll: bool = True,
    limit: int = 50,
) -> dict:
    """Background convert for dashboard (non-blocking)."""
    with _job_lock:
        if _job_state.get("running"):
            return {"ok": False, "message": "转换任务已在进行中", "job": job_status()}

    def worker():
        _set_job(
            running=True,
            started_at=time.time(),
            finished_at=0,
            formats=list(formats),
            total=0,
            done=0,
            ok=0,
            fail=0,
            skipped=0,
            message="running",
            results=[],
            error="",
        )
        try:
            out = convert_accounts(
                emails,
                formats,
                only_pending=only_pending,
                allow_enroll=allow_enroll,
                rebuild=True,
                limit=limit,
            )
            _set_job(
                running=False,
                finished_at=time.time(),
                total=out.get("total") or 0,
                done=out.get("total") or 0,
                ok=out.get("ok_count") or 0,
                fail=out.get("fail_count") or 0,
                skipped=out.get("skipped") or 0,
                message=out.get("message") or "",
                results=out.get("results") or [],
                error="",
                bundles=out.get("bundles") or {},
            )
        except Exception as exc:
            _set_job(
                running=False,
                finished_at=time.time(),
                message="转换失败",
                error=str(exc)[:400],
            )

    t = threading.Thread(target=worker, name="account-convert", daemon=True)
    t.start()
    return {"ok": True, "message": "转换任务已启动", "job": job_status()}
