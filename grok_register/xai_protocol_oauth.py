"""
Protocol OAuth login for xAI (no full browser) — reverse-engineered path.

Primary reference: github.com/dongguatanglinux/grok-build-auth
  - xconsole_client/oauth_protocol.py  (CreateSession + cookie-setter + consent)
  - xconsole_client/xai_oauth.py       (PKCE + token exchange + CLIProxyAPI export)

KiroX / aBaiAutoplus are protocol *register machines* for other products
(AWS Builder ID / similar). Patterns reused here: concurrent task control,
credential JSON export, refresh lifecycle. xAI wire format comes from
grok-build-auth + CLIProxyAPI internal/auth/xai.

This module provides:
  1) PKCE helpers + authorization-code token exchange
  2) refresh_access_token (re-export from cliproxyapi)
  3) build_cliproxyapi_document for single-file export (type=xai)
  4) Optional protocol login when curl_cffi + YesCaptcha available

Full CreateSession gRPC-web path is implemented when dependencies exist;
otherwise callers keep using browser register + device-flow enroller.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlencode

from grok_register.cliproxyapi import (
    DEFAULT_CLIENT_ID,
    DEFAULT_TOKEN_ENDPOINT,
    GROK_CLI_BASE_URL,
    GROK_CLI_HEADERS,
    apply_token_to_document,
    refresh_access_token,
)

ISSUER = "https://auth.x.ai"
AUTHORIZATION_ENDPOINT = f"{ISSUER}/oauth2/authorize"
TOKEN_ENDPOINT = DEFAULT_TOKEN_ENDPOINT
USERINFO_ENDPOINT = f"{ISSUER}/oauth2/userinfo"

DEFAULT_SCOPES = [
    "openid",
    "profile",
    "email",
    "offline_access",
    "grok-cli:access",
    "api:access",
]

# Observed public client id (Grok CLI / same as device flow)
CLIENT_ID = DEFAULT_CLIENT_ID

# accounts.x.ai protocol endpoints (from grok-build-auth)
TURNSTILE_SITEKEY = "0x4AAAAAAAhr9JGVDZbrZOo0"
CREATE_SESSION_RPC = "https://accounts.x.ai/auth_mgmt.AuthManagement/CreateSession"
CREATE_COOKIE_SETTER_RPC = "https://accounts.x.ai/auth_mgmt.AuthManagement/CreateCookieSetterLink"
ACCOUNTS_ORIGIN = "https://accounts.x.ai"


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def generate_code_verifier() -> str:
    return _b64url(secrets.token_bytes(48))


def code_challenge_s256(code_verifier: str) -> str:
    return _b64url(hashlib.sha256(code_verifier.encode("ascii")).digest())


def parse_jwt_payload(jwt_token: str) -> Optional[dict[str, Any]]:
    try:
        parts = jwt_token.split(".")
        if len(parts) < 2:
            return None
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return None


def build_authorization_url(
    *,
    client_id: str = CLIENT_ID,
    redirect_uri: str,
    state: str,
    nonce: str,
    code_challenge: str,
    scopes: list[str] | None = None,
) -> str:
    scopes = scopes or list(DEFAULT_SCOPES)
    q = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": " ".join(scopes),
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "nonce": nonce,
    }
    return f"{AUTHORIZATION_ENDPOINT}?{urlencode(q)}"


def exchange_authorization_code(
    *,
    code: str,
    code_verifier: str,
    redirect_uri: str,
    client_id: str = CLIENT_ID,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """POST auth.x.ai/oauth2/token with authorization_code + PKCE verifier."""
    import urllib.error
    import urllib.parse
    import urllib.request

    body = urllib.parse.urlencode(
        {
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        TOKEN_ENDPOINT,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            token = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"code exchange HTTP {exc.code}: {exc.read()[:400]!r}") from exc
    if not isinstance(token, dict) or not token.get("access_token"):
        raise RuntimeError("token response missing access_token")
    now = int(time.time())
    if "expires_in" in token and "expires_at" not in token:
        try:
            token["expires_at"] = now + int(token["expires_in"])
        except (TypeError, ValueError):
            pass
    return token


def build_cliproxyapi_document(
    token: dict[str, Any],
    *,
    email: str = "",
    prefer_grok_cli: bool = True,
) -> dict[str, Any]:
    """Single-file auth document for CLIProxyAPI (type=xai)."""
    id_payload = parse_jwt_payload(str(token.get("id_token") or "")) or {}
    if not email:
        email = str(id_payload.get("email") or "")
    base: dict[str, Any] = {
        "type": "xai",
        "auth_kind": "oauth",
        "email": email,
        "sub": str(id_payload.get("sub") or ""),
        "token_endpoint": TOKEN_ENDPOINT,
    }
    if prefer_grok_cli:
        base["base_url"] = GROK_CLI_BASE_URL
        base["headers"] = dict(GROK_CLI_HEADERS)
    else:
        base["base_url"] = "https://api.x.ai/v1"
    return apply_token_to_document(base, token, prefer_grok_cli_base=prefer_grok_cli)


def save_cliproxyapi_document(
    document: dict[str, Any],
    auth_dir: str | Path,
    *,
    filename: str | None = None,
) -> Path:
    """Write xai-*.json into auth dir (CLIProxyAPI hot-load unit)."""
    target = Path(auth_dir)
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not filename:
        email = str(document.get("email") or document.get("sub") or "unknown")
        safe = "".join(ch if ch.isalnum() or ch in "._@-" else "-" for ch in email)
        if not safe.lower().startswith("xai"):
            safe = f"xai-{safe}"
        filename = f"{safe}.json"
    path = target / filename
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path


def try_protocol_login(
    email: str,
    password: str,
    *,
    yescaptcha_key: str = "",
    proxy: str = "",
    cliproxyapi_auth_dir: str | Path | None = None,
    debug: bool = False,
) -> dict[str, Any]:
    """
    Attempt full protocol OAuth via vendored/reference grok-build-auth if present.

    Looks for /tmp/grok-build-auth or GROK_BUILD_AUTH_ROOT. Returns a result dict:
      {ok, document?, path?, error?}
    """
    import os
    import sys

    roots = []
    env_root = (os.environ.get("GROK_BUILD_AUTH_ROOT") or "").strip()
    if env_root:
        roots.append(Path(env_root))
    roots.extend(
        [
            Path("/tmp/grok-build-auth"),
            Path.home() / "grok-build-auth",
            Path(__file__).resolve().parents[1] / "vendor" / "grok-build-auth",
        ]
    )
    root = next((r for r in roots if (r / "xconsole_client").is_dir()), None)
    if root is None:
        return {
            "ok": False,
            "error": "grok-build-auth not found; clone https://github.com/dongguatanglinux/grok-build-auth",
            "protocol": "unavailable",
        }
    root_s = str(root)
    if root_s not in sys.path:
        sys.path.insert(0, root_s)
    try:
        from xconsole_client.oauth_protocol import login_with_protocol  # type: ignore
    except Exception as exc:
        return {"ok": False, "error": f"import oauth_protocol failed: {exc}", "protocol": "import_error"}

    try:
        result = login_with_protocol(
            email,
            password,
            yescaptcha_key=yescaptcha_key or os.environ.get("YESCAPTCHA_KEY") or "",
            proxy=proxy,
            debug=debug,
            cliproxyapi_auth_dir=str(cliproxyapi_auth_dir) if cliproxyapi_auth_dir else None,
        )
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:500], "protocol": "login_failed"}

    token = getattr(result, "token", None) or {}
    if not token.get("access_token"):
        return {"ok": False, "error": "protocol login returned no access_token", "protocol": "empty"}

    doc = build_cliproxyapi_document(token, email=email)
    path = None
    if cliproxyapi_auth_dir:
        path = save_cliproxyapi_document(doc, cliproxyapi_auth_dir)
    elif getattr(result, "cliproxyapi_path", None):
        path = Path(str(result.cliproxyapi_path))
    return {
        "ok": True,
        "document": doc,
        "path": str(path) if path else None,
        "protocol": "grok-build-auth",
        "email": email,
    }


def protocol_capabilities() -> dict[str, Any]:
    """Report what protocol pieces are available on this host."""
    import os
    import sys
    from pathlib import Path

    root = Path(os.environ.get("GROK_BUILD_AUTH_ROOT") or "/tmp/grok-build-auth")
    has_ref = (root / "xconsole_client" / "oauth_protocol.py").is_file()
    has_curl = False
    try:
        import curl_cffi  # noqa: F401

        has_curl = True
    except Exception:
        pass
    return {
        "reference_root": str(root),
        "reference_present": has_ref,
        "curl_cffi": has_curl,
        "client_id": CLIENT_ID,
        "token_endpoint": TOKEN_ENDPOINT,
        "authorization_endpoint": AUTHORIZATION_ENDPOINT,
        "turnstile_sitekey": TURNSTILE_SITEKEY,
        "create_session_rpc": CREATE_SESSION_RPC,
        "notes": [
            "KiroX is AWS Builder ID — patterns only, not xAI wire format",
            "aBaiAutoplus clone may fail on network; use grok-build-auth for xAI",
            "CLIProxyAPI expects single xai-*.json (not accounts.cpa.json bundle)",
        ],
    }


# re-export refresh for callers
__all__ = [
    "CLIENT_ID",
    "DEFAULT_SCOPES",
    "TOKEN_ENDPOINT",
    "build_authorization_url",
    "build_cliproxyapi_document",
    "code_challenge_s256",
    "exchange_authorization_code",
    "generate_code_verifier",
    "parse_jwt_payload",
    "protocol_capabilities",
    "refresh_access_token",
    "save_cliproxyapi_document",
    "try_protocol_login",
]
