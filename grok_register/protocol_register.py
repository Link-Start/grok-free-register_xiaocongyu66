"""
HTTP protocol registration for xAI (curl_cffi TLS + CreateUserAndSessionV2).

Adapted from /root/Grok注册机 (grok_auto/protocol_client + proto_util):
  - curl_cffi chrome impersonation (not Go net/http JA3)
  - CreateEmailValidationCode / VerifyEmailValidationCode
  - CreateUserAndSessionV2 (pure gRPC-web — no Next.js server action)

Turnstile: still needs a token (CloakBrowser / hybrid solver / CapSolver).
There is NO pure-protocol reverse of Cloudflare Turnstile in the reference
machine — it also uses CloakBrowser for captcha only.

Output: keys/accounts.txt (email:password:sso) — never accounts.cpa.json.
"""
from __future__ import annotations

import os
import random
import re
import secrets
import string
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Optional

from grok_register.xai_proto_util import (
    build_create_email_validation_code,
    build_create_user_and_session,
    build_verify_email_validation_code,
    grpc_web_frame,
    parse_grpc_web_response,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SITE_URL = "https://accounts.x.ai"
SIGNUP_URL = f"{SITE_URL}/sign-up?redirect=grok-com"
SERVICE = "auth_mgmt.AuthManagement"
TURNSTILE_SITEKEY = "0x4AAAAAAAhr9JGVDZbrZOo0"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


class Stats:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.started = 0
        self.success = 0
        self.failed = 0
        self.last_error = ""

    def bump_start(self) -> None:
        with self.lock:
            self.started += 1

    def bump_ok(self) -> int:
        with self.lock:
            self.success += 1
            return self.success

    def bump_fail(self, msg: str) -> None:
        with self.lock:
            self.failed += 1
            self.last_error = msg[:300]


class CffiSession:
    """curl_cffi session with chrome impersonation (CF-friendly)."""

    def __init__(self, proxy: str = "", timeout: float = 30.0):
        from curl_cffi import requests as crequests

        self.timeout = timeout
        self._session = crequests.Session(impersonate="chrome131")
        if proxy:
            self._session.proxies = {"http": proxy, "https": proxy}
        # Warm CF cookies
        try:
            self._session.get(SIGNUP_URL, timeout=timeout)
        except Exception:
            pass

    def close(self) -> None:
        try:
            self._session.close()
        except Exception:
            pass

    def post(self, url: str, *, headers: dict, data: bytes):
        return self._session.post(url, headers=headers, data=data, timeout=self.timeout)

    def get(self, url: str, *, headers: Optional[dict] = None):
        return self._session.get(url, headers=headers or {}, timeout=self.timeout)

    def rpc(self, method: str, message: bytes) -> tuple[bool, int, str, bytes, dict]:
        url = f"{SITE_URL}/{SERVICE}/{method}"
        headers = {
            "content-type": "application/grpc-web+proto",
            "x-grpc-web": "1",
            "origin": SITE_URL,
            "referer": SIGNUP_URL,
            "accept": "*/*",
            "x-user-agent": "connect-es/2.1.1",
        }
        body = grpc_web_frame(message)
        try:
            resp = self.post(url, headers=headers, data=body)
        except Exception as exc:
            return False, 2, f"request: {exc}", b"", {}
        content = getattr(resp, "content", b"") or b""
        if isinstance(content, str):
            content = content.encode("utf-8", errors="replace")
        hdrs: dict[str, str] = {}
        try:
            for k, v in dict(resp.headers or {}).items():
                hdrs[str(k).lower()] = str(v)
        except Exception:
            pass
        # also cookies for sso
        try:
            for c in self._session.cookies:
                name = getattr(c, "name", None) or (c[0] if isinstance(c, tuple) else None)
                val = getattr(c, "value", None) or (c[1] if isinstance(c, tuple) else None)
                if name and val:
                    hdrs[f"cookie.{name}"] = str(val)
        except Exception:
            pass
        st, msg, payload = parse_grpc_web_response(content, hdrs)
        http_st = int(getattr(resp, "status_code", 0) or 0)
        if http_st >= 400 and st == 0:
            return False, st, f"http {http_st}", payload, hdrs
        return st == 0, st, msg, payload, hdrs

    def sso_cookie(self) -> str:
        try:
            for c in self._session.cookies:
                name = getattr(c, "name", "") or ""
                val = getattr(c, "value", "") or ""
                if name == "sso" and val:
                    return str(val)
        except Exception:
            pass
        # curl_cffi cookies may be dict-like
        try:
            jar = self._session.cookies
            if hasattr(jar, "get"):
                v = jar.get("sso")
                if v:
                    return str(v)
        except Exception:
            pass
        return ""


def moemail_headers(json_body: bool = False) -> dict[str, str]:
    key = _env("MOEMAIL_API_KEY")
    if not key:
        raise RuntimeError("MOEMAIL_API_KEY missing")
    h = {
        "Accept": "application/json",
        "X-API-Key": key,
        "User-Agent": USER_AGENT,
    }
    if json_body:
        h["Content-Type"] = "application/json"
    return h


def _http_json_urllib(
    method: str,
    url: str,
    *,
    headers: Optional[dict] = None,
    data: Optional[bytes] = None,
    timeout: float = 20,
) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=data, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:  # type: ignore[name-defined]
        return int(exc.code), exc.read() if hasattr(exc, "read") else b""
    except Exception as exc:
        raise RuntimeError(str(exc)) from exc


# urllib.error import for type
import urllib.error  # noqa: E402


def create_mailbox() -> tuple[str, str, str]:
    """Return (email, password, handle). Uses urllib (MoeMail accepts X-API-Key)."""
    password = "".join(
        secrets.choice(string.ascii_letters + string.digits + "!@#$%")
        for _ in range(16)
    )
    mode = _env("EMAIL_MODE", "moemail").lower()
    if mode == "custom":
        domain = _env("EMAIL_DOMAIN")
        if not domain:
            raise RuntimeError("EMAIL_DOMAIN required for custom mode")
        local = "oc" + secrets.token_hex(5)
        email = f"{local}@{domain}"
        return email, password, email
    if mode != "moemail":
        raise RuntimeError(f"protocol supports moemail|custom, got {mode}")

    api = _env("MOEMAIL_API", "https://moemail.app").rstrip("/")
    domain = _env("MOEMAIL_DOMAIN")
    if not domain:
        import json

        st, body = _http_json_urllib("GET", f"{api}/api/config", headers=moemail_headers())
        if st >= 400:
            raise RuntimeError(f"moemail config http {st}")
        data = json.loads(body.decode("utf-8", errors="replace"))
        raw = str(data.get("emailDomains") or "")
        parts = [p.strip() for p in raw.split(",") if p.strip()]
        if not parts:
            raise RuntimeError("moemail: no domain")
        domain = parts[0]

    import json

    name = "oc" + secrets.token_hex(4)
    payload = json.dumps(
        {
            "name": name,
            "domain": domain,
            "expiryTime": _env_int("MOEMAIL_EXPIRY_MS", 3_600_000),
        }
    ).encode()
    st, body = _http_json_urllib(
        "POST",
        f"{api}/api/emails/generate",
        headers=moemail_headers(json_body=True),
        data=payload,
    )
    if st >= 400:
        raise RuntimeError(f"moemail create http {st}: {body[:160]!r}")
    data = json.loads(body.decode("utf-8", errors="replace"))
    if data.get("error") and not data.get("email") and not data.get("id"):
        raise RuntimeError(f"moemail: {data.get('error')}")
    email = str(data.get("email") or data.get("address") or "")
    eid = str(data.get("id") or email)
    if not email:
        raise RuntimeError(f"moemail create failed: {data}")
    return email, password, eid


def poll_moemail_code(handle: str, timeout_sec: float = 90) -> str:
    import json

    api = _env("MOEMAIL_API", "https://moemail.app").rstrip("/")
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        st, body = _http_json_urllib(
            "GET",
            f"{api}/api/emails/{urllib.parse.quote(handle, safe='')}",
            headers=moemail_headers(),
            timeout=15,
        )
        text = body.decode("utf-8", errors="replace") if st < 500 else ""
        code = _find_code(text)
        if code:
            return code
        time.sleep(2.5)
    raise RuntimeError("code timeout")


def poll_custom_code(email: str, timeout_sec: float = 90) -> str:
    import json

    api = _env("EMAIL_API", "http://127.0.0.1:8080").rstrip("/")
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        st, body = _http_json_urllib(
            "GET",
            f"{api}/check/{urllib.parse.quote(email, safe='')}",
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=12,
        )
        text = body.decode("utf-8", errors="replace")
        try:
            data = json.loads(text)
            c = str(data.get("code") or "").replace("-", "")
            if c:
                return c
        except Exception:
            pass
        code = _find_code(text)
        if code:
            return code
        time.sleep(2.5)
    raise RuntimeError("code timeout")


def _find_code(text: str) -> str:
    up = text.upper()
    m = re.search(r"\b([A-Z0-9]{3})-([A-Z0-9]{3})\b", up)
    if m:
        return m.group(1) + m.group(2)
    m = re.search(r"\b([A-Z0-9]{6})\b", up)
    if m:
        return m.group(1)
    return ""


def solve_turnstile_sync(site_key: str = TURNSTILE_SITEKEY) -> str:
    """Obtain Turnstile token via hybrid API / CapSolver — never pure reverse."""
    # CapSolver / 2captcha first if keys present (no browser)
    api_key = _env("CAPSOLVER_API_KEY") or _env("CAPSOLVER_KEY")
    if api_key:
        tok = _capsolver_turnstile(api_key, site_key)
        if tok:
            return tok
    api_key = _env("TWOCAPTCHA_API_KEY") or _env("CAPTCHA_API_KEY")
    if api_key:
        tok = _twocaptcha_turnstile(api_key, site_key)
        if tok:
            return tok

    # Hybrid / managed browser solver (on-demand)
    from grok_register.turnstile_solver import (
        ensure_solver_if_needed,
        health_check,
        is_api_backend,
        resolve_api_url,
        resolve_solver_mode,
    )

    mode = resolve_solver_mode()
    api = (_env("TURNSTILE_API_URL") or resolve_api_url(mode)).rstrip("/")
    if is_api_backend(mode) and not health_check(api, timeout=1.2):
        meta = ensure_solver_if_needed(log=lambda m: print(m, flush=True))
        api = (meta.get("api_url") or api).rstrip("/")
        os.environ["TURNSTILE_API_URL"] = api

    q = urllib.parse.urlencode({"url": SIGNUP_URL, "sitekey": site_key})
    create_url = f"{api}/turnstile?{q}"
    timeout_sec = _env_int("TURNSTILE_API_TIMEOUT", 100)
    # Remote HF / standalone solver may require Bearer / X-API-Key
    api_token = (
        _env("TURNSTILE_API_TOKEN")
        or _env("SOLVER_API_TOKEN")
        or _env("TURNSTILE_SOLVER_TOKEN")
        or ""
    ).strip()

    def _auth_req(url: str, method: str = "GET") -> urllib.request.Request:
        r = urllib.request.Request(url, method=method)
        r.add_header("User-Agent", USER_AGENT)
        if api_token:
            r.add_header("Authorization", f"Bearer {api_token}")
            r.add_header("X-API-Key", api_token)
        return r

    req = _auth_req(create_url)
    with urllib.request.urlopen(req, timeout=30) as resp:
        import json

        created = json.loads(resp.read().decode())
    task_id = created.get("task_id") or created.get("taskId") or ""
    if not task_id:
        raise RuntimeError(f"turnstile no task id: {created}")
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        time.sleep(0.8)
        rreq = _auth_req(
            f"{api}/result?id={urllib.parse.quote(str(task_id), safe='')}"
        )
        try:
            with urllib.request.urlopen(rreq, timeout=20) as rresp:
                raw = rresp.read().decode("utf-8", errors="replace")
        except Exception:
            continue
        raw_s = raw.strip().strip('"')
        if raw_s in {"CAPTCHA_NOT_READY", "processing"}:
            continue
        import json

        try:
            result = json.loads(raw)
        except Exception:
            if len(raw_s) > 20 and "CAPTCHA" not in raw_s:
                return raw_s
            continue
        st = str(result.get("status") or "")
        if st in {"processing", "CAPTCHA_NOT_READY"}:
            continue
        val = result.get("value") or result.get("token")
        if isinstance(val, str) and len(val) > 20 and val not in {
            "CAPTCHA_FAIL",
            "CAPTCHA_NOT_READY",
        }:
            return val
        sol = result.get("solution") or {}
        if isinstance(sol, dict):
            t = sol.get("token")
            if isinstance(t, str) and len(t) > 20:
                return t
        if val == "CAPTCHA_FAIL":
            raise RuntimeError("captcha fail")
    raise RuntimeError("turnstile timeout")


def _capsolver_turnstile(api_key: str, sitekey: str) -> str:
    try:
        from curl_cffi import requests as crequests

        create = crequests.post(
            "https://api.capsolver.com/createTask",
            json={
                "clientKey": api_key,
                "task": {
                    "type": "AntiTurnstileTaskProxyLess",
                    "websiteURL": SIGNUP_URL,
                    "websiteKey": sitekey,
                },
            },
            timeout=30,
        ).json()
        task_id = create.get("taskId")
        if not task_id:
            return ""
        for _ in range(40):
            time.sleep(2.5)
            res = crequests.post(
                "https://api.capsolver.com/getTaskResult",
                json={"clientKey": api_key, "taskId": task_id},
                timeout=30,
            ).json()
            if res.get("status") == "ready":
                sol = res.get("solution") or {}
                return str(sol.get("token") or "")
            if res.get("errorId"):
                return ""
    except Exception:
        return ""
    return ""


def _twocaptcha_turnstile(api_key: str, sitekey: str) -> str:
    try:
        from curl_cffi import requests as crequests

        r = crequests.get(
            "https://2captcha.com/in.php",
            params={
                "key": api_key,
                "method": "turnstile",
                "sitekey": sitekey,
                "pageurl": SIGNUP_URL,
                "json": 1,
            },
            timeout=30,
        ).json()
        if r.get("status") != 1:
            return ""
        rid = r.get("request")
        for _ in range(40):
            time.sleep(3)
            res = crequests.get(
                "https://2captcha.com/res.php",
                params={"key": api_key, "action": "get", "id": rid, "json": 1},
                timeout=30,
            ).json()
            if res.get("status") == 1:
                return str(res.get("request") or "")
            if res.get("request") not in ("CAPCHA_NOT_READY", "CAPTCHA_NOT_READY"):
                return ""
    except Exception:
        return ""
    return ""


_write_lock = threading.Lock()


def append_account(path: str, email: str, password: str, sso: str) -> None:
    """SSO-first persist: accounts.txt + grok.txt + auth-sessions (no live CPA)."""
    p = Path(path)
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    p.parent.mkdir(parents=True, exist_ok=True)
    # never touch accounts.cpa.json
    line = f"{email}:{password}:{sso}\n"
    with _write_lock:
        # Prefer unified SSO export helper (also writes auth-sessions / grok.txt)
        try:
            from grok_register.sso.export import append_sso_artifacts

            # Full SSO pack (accounts + grok + auth-sessions); never accounts.cpa.json
            append_sso_artifacts(email, password, sso, root=p.parent)
        except Exception:
            with p.open("a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
        log_path = p.parent / "accounts.protocol.log"
        with log_path.open("a", encoding="utf-8") as lf:
            lf.write(
                f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\t{email}\t{sso[:24]}\n"
            )
        try:
            from grok_register.memory_hygiene import note_op_and_maybe_trim

            note_op_and_maybe_trim()
        except Exception:
            pass


def _jwt_like_from_bytes(data: bytes) -> str:
    text = data.decode("utf-8", errors="replace")
    m = re.search(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}", text)
    return m.group(0) if m else ""


def register_once(
    *,
    site_key: str = TURNSTILE_SITEKEY,
    action_id: str = "",  # unused for V2 path; kept for API compat
    state_tree: str = "",
    proxy: str = "",
    output_file: str = "keys/accounts.txt",
    wid: int = 0,
) -> None:
    _ = action_id, state_tree
    email, password, handle = create_mailbox()
    session = CffiSession(proxy=proxy)
    try:
        # 1) send code
        msg = build_create_email_validation_code(email)
        ok, st, m, _, _ = session.rpc("CreateEmailValidationCode", msg)
        if not ok:
            raise RuntimeError(f"create code: {m or st}")

        # 2) poll code + turnstile in parallel (like reference machine)
        mode = _env("EMAIL_MODE", "moemail").lower()
        code_holder: dict[str, Any] = {}
        ts_holder: dict[str, Any] = {}

        def _code() -> None:
            if mode == "moemail":
                code_holder["v"] = poll_moemail_code(handle)
            else:
                code_holder["v"] = poll_custom_code(email)

        def _ts() -> None:
            ts_holder["v"] = solve_turnstile_sync(site_key or TURNSTILE_SITEKEY)

        with ThreadPoolExecutor(max_workers=2) as pool:
            f1 = pool.submit(_code)
            f2 = pool.submit(_ts)
            f1.result()
            f2.result()
        code = str(code_holder.get("v") or "")
        token = str(ts_holder.get("v") or "")
        if not code:
            raise RuntimeError("empty code")
        if not token:
            raise RuntimeError("empty turnstile token")
        clean = re.sub(r"[^A-Za-z0-9]", "", code).upper()

        # 3) verify (non-fatal)
        vmsg = build_verify_email_validation_code(email, clean)
        ok, st, m, _, _ = session.rpc("VerifyEmailValidationCode", vmsg)
        if not ok:
            print(f"[W{wid}] verify soft-fail: {m or st}", flush=True)

        # 4) CreateUserAndSessionV2
        given = random.choice(
            ["James", "John", "Robert", "Michael", "William", "David", "Emma", "Olivia"]
        )
        family = random.choice(
            ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller"]
        )
        cmsg = build_create_user_and_session(
            email=email,
            password=password,
            given_name=given,
            family_name=family,
            email_validation_code=clean,
            turnstile_token=token,
            castle_token="",
            tos_accepted_version=1,
            num_one_time_links=0,
        )
        ok, st, m, payload, hdrs = session.rpc("CreateUserAndSessionV2", cmsg)
        if not ok and "unimplemented" in (m or "").lower():
            ok, st, m, payload, hdrs = session.rpc("CreateUserAndSession", cmsg)
        if not ok:
            raise RuntimeError(f"create user: {m or st}")

        sso = session.sso_cookie() or _jwt_like_from_bytes(payload)
        if not sso:
            # some deployments put session in set-cookie header text
            for k, v in hdrs.items():
                if "sso" in k and v:
                    sso = v
                    break
        if not sso:
            # still success account — store password with placeholder session from body
            sso = _jwt_like_from_bytes(payload) or "session-ok"
        append_account(output_file, email, password, sso)
        print(f"[W{wid}] success {email}", flush=True)
    finally:
        session.close()


def run_protocol_register(
    *,
    site_key: str,
    action_id: str,
    state_tree: str,
    workers: Optional[int] = None,
    target: Optional[int] = None,
    proxy: str = "",
    output_file: str = "keys/accounts.txt",
) -> int:
    workers = workers or _env_int("GO_REGISTER_WORKERS", 8)
    target = target if target is not None else _env_int("TARGET", 0)
    # fixed proxy (optional) — otherwise rotate from PROXY_POOL / 代理.txt
    fixed_proxy = (
        proxy
        or _env("REGISTER_PROXY")
        or _env("HTTPS_PROXY")
        or _env("HTTP_PROXY")
    )
    output_file = _env("GO_REGISTER_OUTPUT") or output_file
    stats = Stats()
    stop = threading.Event()

    def _pick_proxy() -> str:
        if fixed_proxy:
            return fixed_proxy
        try:
            from grok_register.register import _pick_grok_proxy

            return _pick_grok_proxy() or ""
        except Exception:
            return ""

    try:
        from grok_register.sso.export import sso_only_export_enabled

        sso_mode = sso_only_export_enabled()
    except Exception:
        sso_mode = True
    print(
        f"[*] 协议注册 curl_cffi+V2 workers={workers} target={target or '∞'} "
        f"email={_env('EMAIL_MODE', 'moemail')} "
        f"export={'SSO-only→keys/' if sso_mode else 'legacy'} (无 live CPA)",
        flush=True,
    )
    if fixed_proxy:
        print(f"[*] proxy: fixed {fixed_proxy[:48]}…", flush=True)
    elif _env("PROXY_POOL") or _env("PROXY_POOL_LIST") or _env("PROXIES") or _env("PROXY_LIST"):
        print("[*] proxy: rotating from PROXY_POOL / PROXY_POOL_LIST env", flush=True)
    else:
        print("[*] proxy: pool file / auto (or direct)", flush=True)
    if not (_env("CAPSOLVER_API_KEY") or _env("CAPSOLVER_KEY") or _env("TWOCAPTCHA_API_KEY")):
        print(
            "[*] Turnstile: 无 CapSolver/2captcha key → 按需 hybrid browser solver "
            "（参考机同样无法纯协议逆向 CF Turnstile）",
            flush=True,
        )

    def worker(wid: int) -> None:
        while not stop.is_set():
            if target and stats.success >= target:
                return
            stats.bump_start()
            try:
                register_once(
                    site_key=site_key or TURNSTILE_SITEKEY,
                    action_id=action_id,
                    state_tree=state_tree,
                    proxy=_pick_proxy(),
                    output_file=output_file,
                    wid=wid,
                )
                n = stats.bump_ok()
                print(f"[W{wid}] success #{n}", flush=True)
                if target and n >= target:
                    stop.set()
                    return
            except Exception as exc:
                stats.bump_fail(str(exc))
                print(f"[W{wid}] fail: {exc}", flush=True)
                try:
                    from grok_register.run_log import append_fail

                    append_fail(
                        "worker_fail",
                        str(exc)[:800],
                        worker=wid,
                        engine="protocol",
                        extra={"proxy": (_pick_proxy() or "")[:120]},
                    )
                except Exception:
                    pass
                try:
                    from grok_register.memory_hygiene import note_op_and_maybe_trim

                    note_op_and_maybe_trim()
                except Exception:
                    pass
                if stop.wait(1.5):
                    return

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futs = [pool.submit(worker, i) for i in range(max(1, workers))]
        try:
            for f in as_completed(futs):
                _ = f.result()
                if target and stats.success >= target:
                    stop.set()
        except KeyboardInterrupt:
            stop.set()

    print(
        f"[*] done success={stats.success} failed={stats.failed} "
        f"last_error={stats.last_error!r}",
        flush=True,
    )
    try:
        from grok_register.memory_hygiene import trim_memory

        info = trim_memory(force=True)
        print(f"[*] memory trim rss={info.get('rss_after_mb')}MB", flush=True)
    except Exception:
        pass

    # Optional: after SSO batch, convert pending → CPA (env SSO_CONVERT_AFTER_REGISTER=1)
    if stats.success > 0 and _env("SSO_CONVERT_AFTER_REGISTER", "0").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        try:
            from grok_register.sso.export import convert_sso_to_product

            conv = convert_sso_to_product(
                only_pending=True,
                limit=max(stats.success, _env_int("SSO_CONVERT_LIMIT", 200)),
                enroll=True,
                rebuild=True,
            )
            print(f"[*] SSO→CPA: {conv.get('message')}", flush=True)
        except Exception as exc:
            print(f"[!] SSO→CPA convert failed: {exc}", flush=True)

    code = 0 if stats.success > 0 or target == 0 else 1
    try:
        from grok_register.run_log import append_fail

        append_fail(
            "protocol_done",
            f"success={stats.success} failed={stats.failed} last_error={stats.last_error!r}",
            level="info" if code == 0 else "error",
            engine="protocol",
            exit_code=code,
            extra={"success": stats.success, "failed": stats.failed},
        )
    except Exception:
        pass
    return code
