"""SSO → Build OAuth (grok2api sso_build / ZhuCe vault_oauth).

Pure HTTP with curl_cffi Chrome TLS fingerprint — no browser.
Writes keys/cpa/xai-*.json (CLIProxyAPI type=xai).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
SCOPE = (
    "openid profile email offline_access "
    "grok-cli:access api:access conversations:read conversations:write"
)
TOKEN_URL = "https://auth.x.ai/oauth2/token"
DEVICE_CODE_URL = "https://auth.x.ai/oauth2/device/code"
DEVICE_VERIFY = "https://auth.x.ai/oauth2/device/verify"
DEVICE_APPROVE = "https://auth.x.ai/oauth2/device/approve"
ACCOUNTS_HOME = "https://accounts.x.ai/"
CLI_BASE = "https://cli-chat-proxy.grok.com/v1"

ProgressCB = Callable[..., None] | None


def _session(proxy: str | None = None):
    try:
        from curl_cffi import requests as creq

        s = creq.Session(impersonate="chrome131")
    except Exception:
        import requests as creq  # type: ignore

        s = creq.Session()
    if proxy:
        s.proxies = {"http": proxy, "https": proxy}
    return s


def _apply_sso_cookies(session, sso: str) -> None:
    sso = (sso or "").strip()
    if sso.lower().startswith("sso="):
        sso = sso[4:].strip()
    if ";" in sso:
        sso = sso.split(";", 1)[0].strip()
    for domain in (".x.ai", "accounts.x.ai", "auth.x.ai", ".accounts.x.ai"):
        for name in ("sso", "sso-rw"):
            try:
                session.cookies.set(name, sso, domain=domain)
            except Exception:
                try:
                    session.cookies.set(name, sso)
                except Exception:
                    pass


def _jwt_claim(token: str, key: str) -> str:
    try:
        import base64

        parts = (token or "").split(".")
        if len(parts) < 2:
            return ""
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        raw = base64.urlsafe_b64decode(payload.encode())
        data = json.loads(raw.decode("utf-8", errors="replace"))
        v = data.get(key)
        return str(v) if v is not None else ""
    except Exception:
        return ""


def _load_salt(root: Path) -> bytes:
    env = (os.environ.get("XAI_ENROLLER_SOURCE_SALT") or "").strip()
    if env:
        return env.encode()
    path = root / ".xai-enroller-salt"
    try:
        if path.is_file():
            val = path.read_text(encoding="utf-8").strip()
            if val:
                return val.encode()
    except OSError:
        pass
    import secrets

    val = secrets.token_urlsafe(32)
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_text(val + "\n", encoding="utf-8")
        os.chmod(path, 0o600)
    except OSError:
        pass
    return val.encode()


def _cpa_path(root: Path, email: str, sub: str, salt: bytes) -> Path:
    dig = hmac.new(salt, (sub or email).encode(), hashlib.sha256).hexdigest()[:16]
    return root / "cpa" / f"xai-{dig}.json"


def _atomic_write_json(path: Path, doc: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def sso_to_oauth(
    email: str,
    sso: str,
    *,
    proxy: str | None = None,
    poll_timeout: float = 75.0,
) -> dict[str, Any]:
    """Run device_code + verify + approve + poll. Returns CPA document fields."""
    session = _session(proxy)
    _apply_sso_cookies(session, sso)

    # ZhuCe: warm session; soft-fail on GET (CF may 403 plain Go but curl_cffi ok)
    try:
        session.get(ACCOUNTS_HOME, timeout=25, allow_redirects=True)
    except Exception:
        pass

    r = session.post(
        DEVICE_CODE_URL,
        data={"client_id": CLIENT_ID, "scope": SCOPE},
        timeout=30,
    )
    if r.status_code < 200 or r.status_code >= 300:
        raise RuntimeError(f"device_code HTTP {r.status_code}: {(r.text or '')[:160]}")
    try:
        dev = r.json()
    except Exception as exc:
        raise RuntimeError(f"device_code parse: {exc}") from exc
    device_code = str(dev.get("device_code") or "")
    user_code = str(dev.get("user_code") or "")
    if not device_code or not user_code:
        raise RuntimeError("device_code incomplete")
    ver = str(
        dev.get("verification_uri_complete")
        or (
            (dev.get("verification_uri") or "https://accounts.x.ai/oauth2/device")
            + f"?user_code={user_code}"
        )
    )
    interval = max(1, int(dev.get("interval") or 5))

    r = session.get(ver, timeout=30, allow_redirects=True)
    if r.status_code >= 400:
        raise RuntimeError(f"open device page HTTP {r.status_code}")

    headers = {
        "content-type": "application/x-www-form-urlencoded",
        "origin": "https://accounts.x.ai",
        "referer": ver,
        "accept": "application/json, text/html;q=0.9, */*;q=0.8",
    }
    r = session.post(
        DEVICE_VERIFY,
        data={"user_code": user_code, "client_id": CLIENT_ID},
        headers=headers,
        timeout=30,
        allow_redirects=True,
    )
    final = str(getattr(r, "url", "") or "")
    if "sign-in" in final or "sign-up" in final:
        raise RuntimeError("sso unauthorized (verify redirected to sign-in)")
    if r.status_code >= 400:
        raise RuntimeError(f"device/verify HTTP {r.status_code}")

    r = session.post(
        DEVICE_APPROVE,
        data={
            "user_code": user_code,
            "action": "allow",
            "principal_type": "User",
            "principal_id": "",
            "client_id": CLIENT_ID,
        },
        headers={**headers, "referer": final or ver},
        timeout=30,
        allow_redirects=True,
    )
    final_a = str(getattr(r, "url", "") or "")
    if r.status_code >= 400:
        raise RuntimeError(f"device/approve HTTP {r.status_code}")
    # soft: accept if token poll succeeds even without /done in URL

    deadline = time.time() + max(15.0, min(float(poll_timeout), 120.0))
    last_err = "authorization_pending"
    while time.time() < deadline:
        time.sleep(interval)
        tr = session.post(
            TOKEN_URL,
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "client_id": CLIENT_ID,
                "device_code": device_code,
            },
            timeout=30,
        )
        try:
            payload = tr.json()
        except Exception as exc:
            last_err = f"token parse: {exc}"
            continue
        if tr.status_code < 300 and payload.get("access_token"):
            at = str(payload["access_token"])
            rt = str(payload.get("refresh_token") or "")
            idt = str(payload.get("id_token") or "")
            exp_in = int(payload.get("expires_in") or 3600)
            now = datetime.now(timezone.utc)
            expired_at = datetime.fromtimestamp(
                now.timestamp() + exp_in, tz=timezone.utc
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
            sub = _jwt_claim(idt, "sub") or _jwt_claim(at, "sub") or email
            em = email or _jwt_claim(idt, "email")
            return {
                "type": "xai",
                "access_token": at,
                "refresh_token": rt,
                "id_token": idt,
                "token_type": str(payload.get("token_type") or "Bearer"),
                "expires_in": exp_in,
                "expired": expired_at,
                "last_refresh": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "sub": sub,
                "base_url": CLI_BASE,
                "token_endpoint": TOKEN_URL,
                "auth_kind": "oauth",
                "email": em,
                "headers": {
                    "X-XAI-Token-Auth": "xai-grok-cli",
                    "x-grok-client-version": "0.2.93",
                    "x-grok-client-identifier": "grok-shell",
                },
                "_approve_url": final_a,
            }
        err = str(payload.get("error") or "")
        if err == "authorization_pending":
            last_err = err
            continue
        if err == "slow_down":
            interval += 5
            last_err = err
            continue
        if err in ("access_denied", "expired_token"):
            raise RuntimeError(err)
        if tr.status_code >= 400:
            raise RuntimeError(
                f"oauth token HTTP {tr.status_code}: {payload.get('error_description') or err}"
            )
        last_err = err or f"http {tr.status_code}"
    raise RuntimeError(f"oauth_expired: poll timeout ({last_err})")


def load_pending_sso_newest(root: Path, *, limit: int = 50) -> list[dict[str, str]]:
    """Newest-first pending from accounts.txt (bottom lines = latest register)."""
    accounts = root / "accounts.txt"
    if not accounts.is_file():
        return []
    cpa_emails: set[str] = set()
    cpa_dir = root / "cpa"
    if cpa_dir.is_dir():
        for p in cpa_dir.glob("xai-*.json"):
            try:
                doc = json.loads(p.read_text(encoding="utf-8"))
                em = str(doc.get("email") or "").strip().lower()
                if em:
                    cpa_emails.add(em)
            except Exception:
                continue
    lines = accounts.read_text(encoding="utf-8", errors="replace").splitlines()
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for line in reversed(lines):
        line = line.strip()
        if not line or line.startswith("#") or line.count(":") < 2:
            continue
        email, password, sso = line.split(":", 2)
        email = email.strip()
        sso = sso.strip()
        if not email or not sso:
            continue
        key = email.lower()
        if key in seen or key in cpa_emails:
            continue
        seen.add(key)
        out.append({"email": email, "password": password, "sso": sso})
        if len(out) >= limit:
            break
    return out


def convert_pending_curl(
    root: Path | None = None,
    *,
    limit: int = 20,
    workers: int = 1,
    proxy: str | None = None,
    progress_cb: ProgressCB = None,
) -> dict[str, Any]:
    """Batch SSO→CPA via curl_cffi (preferred over Go net/http on CF-hard networks)."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    root = root or Path((os.environ.get("KEY_EXPORT_DIR") or "keys").strip() or "keys")
    if not root.is_absolute():
        root = Path(__file__).resolve().parents[2] / root
    rows = load_pending_sso_newest(root, limit=limit)
    if not rows:
        return {
            "ok": True,
            "ok_n": 0,
            "fail_n": 0,
            "skip_n": 0,
            "total": 0,
            "results": [],
            "engine": "curl_cffi",
            "message": "no pending SSO",
        }
    salt = _load_salt(root)
    proxy = (proxy or os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY") or "").strip() or None
    workers = max(1, min(int(workers or 1), 8))
    results: list[dict[str, Any]] = []
    ok_n = fail_n = 0
    t0 = time.time()

    def one(row: dict[str, str]) -> dict[str, Any]:
        email = row["email"]
        try:
            doc = sso_to_oauth(email, row["sso"], proxy=proxy)
            sub = str(doc.get("sub") or email)
            path = _cpa_path(root, email, sub, salt)
            payload = {k: v for k, v in doc.items() if not str(k).startswith("_")}
            payload["email"] = email
            _atomic_write_json(path, payload)
            return {
                "ok": True,
                "email": email,
                "method": "protocol_enroll_curl",
                "written": [f"cpa:{path.name}"],
            }
        except Exception as exc:
            return {
                "ok": False,
                "email": email,
                "method": "protocol_enroll_curl",
                "error": str(exc)[:300],
            }

    # Sequential by default under low RAM; parallel if workers>1
    if workers == 1:
        for i, row in enumerate(rows):
            res = one(row)
            results.append(res)
            if res.get("ok"):
                ok_n += 1
            else:
                fail_n += 1
            if progress_cb:
                try:
                    progress_cb(
                        done=i + 1,
                        total=len(rows),
                        ok=ok_n,
                        fail=fail_n,
                        skipped=0,
                        message=f"curl_cffi {i+1}/{len(rows)}",
                    )
                except Exception:
                    pass
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {pool.submit(one, r): r for r in rows}
            done = 0
            for fut in as_completed(futs):
                res = fut.result()
                results.append(res)
                done += 1
                if res.get("ok"):
                    ok_n += 1
                else:
                    fail_n += 1
                if progress_cb:
                    try:
                        progress_cb(
                            done=done,
                            total=len(rows),
                            ok=ok_n,
                            fail=fail_n,
                            skipped=0,
                            message=f"curl_cffi {done}/{len(rows)}",
                        )
                    except Exception:
                        pass

    elapsed = round(time.time() - t0, 2)
    return {
        "ok": fail_n == 0 and ok_n > 0,
        "ok_n": ok_n,
        "fail_n": fail_n,
        "skip_n": 0,
        "total": len(rows),
        "results": results,
        "engine": "curl_cffi",
        "protocol": "device_code+verify+approve",
        "elapsed_sec": elapsed,
        "message": (
            f"SSO→cpa（curl_cffi/grok2api）：成功 {ok_n} · 失败 {fail_n} · {elapsed}s"
        ),
    }
