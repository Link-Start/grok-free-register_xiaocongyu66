"""Re-login with email+password to mint fresh SSO cookies.

Reads:
  keys/accounts.txt              email:password:sso (password required)
  keys/auth-sessions.jsonl       optional cookie jar / fingerprint hints
  keys/browser-fingerprints.json optional per-email fingerprint id (kept on write)
  keys/grok.txt                  rewritten from new SSO list

Writes (atomic-ish):
  keys/accounts.txt              updated sso field for successful emails
  keys/grok.txt                  all SSO tokens (one per line, same order as accounts)
  keys/auth-sessions.jsonl       append new session lines for successes

Does NOT convert to CPA — run auth-service / sso.export convert after.

Flow (ZhuCe-compatible):
  Turnstile (sign-in page) → POST accounts.x.ai/api/rpc createSession
  → follow cookieSetterUrl → Cookie sso / sso-rw
"""
from __future__ import annotations

import json
import os
import random
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SIGNIN_URL = "https://accounts.x.ai/sign-in"
ACCOUNTS = "https://accounts.x.ai"
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


def key_dir() -> Path:
    raw = _env("KEY_EXPORT_DIR", "keys") or "keys"
    p = Path(raw).expanduser()
    return p if p.is_absolute() else PROJECT_ROOT / p


def load_accounts(path: Path) -> list[dict[str, str]]:
    """Parse accounts.txt → [{email, password, sso, line_index}, ...] unique by email (last wins)."""
    if not path.is_file():
        return []
    by: dict[str, dict[str, str]] = {}
    order: list[str] = []
    for i, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines()):
        raw = line.strip()
        if not raw or raw.startswith("#"):
            continue
        parts = raw.split(":", 2)
        if len(parts) < 2:
            continue
        email = parts[0].strip()
        if len(parts) == 2:
            password, sso = parts[1], ""
        else:
            password, sso = parts[1], parts[2]
        if not email or "@" not in email or not password:
            continue
        key = email.lower()
        if key not in by:
            order.append(key)
        by[key] = {
            "email": email,
            "password": password,
            "sso": sso.strip(),
            "line_index": str(i),
        }
    return [by[k] for k in order]


def load_fingerprint_map(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    accounts = doc.get("accounts") or {}
    out: dict[str, str] = {}
    if isinstance(accounts, dict):
        for em, meta in accounts.items():
            if isinstance(meta, dict):
                fid = str(meta.get("browser_fingerprint_id") or "").strip()
            else:
                fid = str(meta or "").strip()
            if fid:
                out[str(em).lower()] = fid
    return out


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


def _extract_cookies(session) -> dict[str, str]:
    try:
        cookies = getattr(session, "cookies", None)
        if cookies is None:
            return {}
        if hasattr(cookies, "get_dict"):
            return {str(k): str(v) for k, v in dict(cookies.get_dict()).items()}
        if hasattr(cookies, "items"):
            return {str(k): str(v) for k, v in cookies.items()}
    except Exception:
        pass
    return {}


def _pick_sso(cookies: dict[str, str]) -> str:
    for name in ("sso", "sso-rw", "SSO", "SSO-RW"):
        v = (cookies.get(name) or "").strip()
        if v.startswith("eyJ") or len(v) > 40:
            return v
    # case-insensitive
    for k, v in cookies.items():
        if k.lower() in {"sso", "sso-rw"} and v:
            return str(v).strip()
    return ""


def _solve_turnstile_url(page_url: str, sitekey: str) -> str:
    import urllib.parse
    import urllib.request

    api_key = _env("CAPSOLVER_API_KEY") or _env("CAPSOLVER_KEY")
    if api_key:
        try:
            from grok_register.protocol_register import _capsolver_turnstile

            tok = _capsolver_turnstile(api_key, sitekey)
            if tok:
                return tok
        except Exception:
            pass
    api_key = _env("TWOCAPTCHA_API_KEY") or _env("CAPTCHA_API_KEY")
    if api_key:
        try:
            from grok_register.protocol_register import _twocaptcha_turnstile

            tok = _twocaptcha_turnstile(api_key, sitekey)
            if tok:
                return tok
        except Exception:
            pass

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

    q = urllib.parse.urlencode({"url": page_url, "sitekey": sitekey})
    create_url = f"{api}/turnstile?{q}"
    timeout_sec = _env_int("TURNSTILE_API_TIMEOUT", 100)
    api_token = (
        _env("TURNSTILE_API_TOKEN")
        or _env("SOLVER_API_TOKEN")
        or _env("TURNSTILE_SOLVER_TOKEN")
        or ""
    ).strip()

    def _auth_req(url: str) -> urllib.request.Request:
        r = urllib.request.Request(url, method="GET")
        r.add_header("User-Agent", USER_AGENT)
        if api_token:
            r.add_header("Authorization", f"Bearer {api_token}")
            r.add_header("X-API-Key", api_token)
        return r

    with urllib.request.urlopen(_auth_req(create_url), timeout=30) as resp:
        created = json.loads(resp.read().decode())
    task_id = created.get("task_id") or created.get("taskId") or ""
    if not task_id:
        raise RuntimeError(f"turnstile no task id: {created}")
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        time.sleep(0.8)
        try:
            with urllib.request.urlopen(
                _auth_req(f"{api}/result?id={urllib.parse.quote(str(task_id), safe='')}"),
                timeout=20,
            ) as rresp:
                raw = rresp.read().decode("utf-8", errors="replace")
        except Exception:
            continue
        raw_s = raw.strip().strip('"')
        if raw_s in {"CAPTCHA_NOT_READY", "processing"}:
            continue
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


def login_once(
    email: str,
    password: str,
    *,
    proxy: str | None = None,
) -> dict[str, Any]:
    """createSession → cookieSetter → sso. Returns {ok, sso, cookies, error}."""
    session = _session(proxy)
    try:
        try:
            ts = _solve_turnstile_url(SIGNIN_URL, TURNSTILE_SITEKEY)
        except Exception as exc:
            return {"ok": False, "email": email, "error": f"turnstile: {exc}"[:240]}

        if not ts:
            return {"ok": False, "email": email, "error": "empty turnstile token"}

        try:
            session.get(SIGNIN_URL, timeout=25, allow_redirects=True)
        except Exception:
            pass

        req_body = {
            "rpc": "createSession",
            "req": {
                "createSessionRequest": {
                    "credentials": {
                        "case": "emailAndPassword",
                        "value": {
                            "email": email,
                            "clearTextPassword": password,
                        },
                    }
                },
                "turnstileToken": ts,
                "castleRequestToken": "",
            },
        }
        r = session.post(
            f"{ACCOUNTS}/api/rpc",
            json=req_body,
            headers={
                "content-type": "application/json",
                "accept": "application/json",
                "origin": ACCOUNTS,
                "referer": SIGNIN_URL,
                "user-agent": USER_AGENT,
            },
            timeout=40,
        )
        try:
            data = r.json() if hasattr(r, "json") else {}
        except Exception:
            data = {}
        if not isinstance(data, dict):
            data = {}

        # error field
        err = data.get("error") or data.get("message") or ""
        if err and r.status_code >= 400:
            return {
                "ok": False,
                "email": email,
                "error": f"createSession HTTP {r.status_code}: {str(err)[:160]}",
            }

        cookie_url = (
            data.get("cookieSetterUrl")
            or data.get("cookie_setter_url")
            or data.get("url")
            or data.get("redirectUrl")
            or ""
        )
        if not cookie_url:
            for v in data.values():
                if isinstance(v, dict):
                    cookie_url = (
                        v.get("cookieSetterUrl")
                        or v.get("cookie_setter_url")
                        or v.get("url")
                        or cookie_url
                    )
                if cookie_url:
                    break

        if cookie_url:
            cur = str(cookie_url)
            for _ in range(12):
                try:
                    resp = session.get(cur, timeout=30, allow_redirects=False)
                except Exception:
                    break
                loc = (
                    resp.headers.get("location")
                    or resp.headers.get("Location")
                    or ""
                )
                if not loc:
                    break
                cur = urljoin(cur, loc)

        cookies = _extract_cookies(session)
        sso = _pick_sso(cookies)
        if not sso:
            # sometimes token in body
            blob = json.dumps(data, ensure_ascii=False)
            m = re.search(
                r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}",
                blob,
            )
            if m:
                sso = m.group(0)
        if not sso:
            return {
                "ok": False,
                "email": email,
                "error": (
                    f"no sso cookie after createSession "
                    f"status={getattr(r, 'status_code', 0)} "
                    f"body={str(data)[:120]}"
                ),
            }
        return {
            "ok": True,
            "email": email,
            "password": password,
            "sso": sso,
            "cookies": cookies,
        }
    finally:
        try:
            session.close()
        except Exception:
            pass


def _load_proxy_pool() -> list[str]:
    raw: list[str] = []
    files = []
    for name in (
        _env("SSO_CONVERT_PROXY_FILE"),
        _env("PROXY_POOL_FILE"),
        str(PROJECT_ROOT / "代理.txt"),
    ):
        if name:
            files.append(name)
    for f in files:
        p = Path(f)
        if not p.is_file():
            continue
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            low = line.lower()
            if low.startswith(("vmess://", "vless://", "trojan://", "ss://")):
                continue
            if "://" not in line:
                line = "http://" + line
            raw.append(line)
    env_pool = (
        _env("SSO_CONVERT_PROXY")
        or _env("PROXY_POOL")
        or _env("PROXY_POOL_LIST")
        or _env("PROXIES")
    )
    if env_pool:
        for sep in ("\n", ",", ";", "|"):
            env_pool = env_pool.replace(sep, "\n")
        for line in env_pool.split("\n"):
            line = line.strip()
            if line:
                if "://" not in line:
                    line = "http://" + line
                raw.append(line)
    # de-dupe
    seen: set[str] = set()
    out: list[str] = []
    for p in raw:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


class _ProxyRR:
    def __init__(self, pool: list[str]):
        self.pool = pool
        self._i = 0
        self._lock = threading.Lock()

    def next(self) -> str | None:
        if not self.pool:
            return None
        with self._lock:
            p = self.pool[self._i % len(self.pool)]
            self._i += 1
            return p


def write_back(
    root: Path,
    accounts: list[dict[str, str]],
    results_by_email: dict[str, dict[str, Any]],
    fingerprints: dict[str, str],
) -> dict[str, str]:
    """Rewrite accounts.txt / grok.txt; append auth-sessions for successes."""
    root.mkdir(parents=True, exist_ok=True)
    accounts_path = root / "accounts.txt"
    grok_path = root / "grok.txt"
    sessions_path = root / "auth-sessions.jsonl"

    lines: list[str] = []
    grok_lines: list[str] = []
    for acc in accounts:
        em = acc["email"]
        key = em.lower()
        res = results_by_email.get(key)
        if res and res.get("ok") and res.get("sso"):
            sso = str(res["sso"])
            pw = acc.get("password") or ""
            lines.append(f"{em}:{pw}:{sso}")
            grok_lines.append(sso)
        else:
            # keep old line
            lines.append(f"{em}:{acc.get('password') or ''}:{acc.get('sso') or ''}")
            if acc.get("sso"):
                grok_lines.append(acc["sso"])

    tmp_a = accounts_path.with_suffix(".txt.tmp")
    tmp_a.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    tmp_a.replace(accounts_path)

    tmp_g = grok_path.with_suffix(".txt.tmp")
    tmp_g.write_text("\n".join(grok_lines) + ("\n" if grok_lines else ""), encoding="utf-8")
    tmp_g.replace(grok_path)

    # append sessions
    with sessions_path.open("a", encoding="utf-8") as f:
        for key, res in results_by_email.items():
            if not res.get("ok"):
                continue
            email = res.get("email") or key
            sso = res.get("sso") or ""
            cookies = res.get("cookies") or {}
            jar = []
            if isinstance(cookies, dict) and cookies:
                for name, val in cookies.items():
                    jar.append(
                        {
                            "name": name,
                            "value": val,
                            "domain": "accounts.x.ai",
                            "path": "/",
                            "secure": True,
                            "httpOnly": True,
                            "sameSite": "Lax",
                        }
                    )
            if not jar and sso:
                for name in ("sso", "sso-rw"):
                    jar.append(
                        {
                            "name": name,
                            "value": sso,
                            "domain": "accounts.x.ai",
                            "path": "/",
                            "secure": True,
                            "httpOnly": True,
                            "sameSite": "Lax",
                        }
                    )
            doc = {
                "email": email,
                "cookies": jar,
                "browser_fingerprint_id": fingerprints.get(key),
                "exported_at": datetime.now(timezone.utc).isoformat(),
                "source": "sso_relogin",
            }
            f.write(json.dumps(doc, ensure_ascii=False, separators=(",", ":")) + "\n")

    return {
        "accounts": str(accounts_path),
        "grok": str(grok_path),
        "auth_sessions": str(sessions_path),
    }



def relogin_batch(
    *,
    root: Path | None = None,
    limit: int = 0,
    workers: int = 2,
    emails: list[str] | None = None,
    only_without_cpa: bool = False,
    progress: bool = True,
) -> dict[str, Any]:
    """Production entry: merge successes into full accounts.txt without dropping others."""
    root = root or key_dir()
    full_accounts = load_accounts(root / "accounts.txt")
    if not full_accounts:
        return {"ok": False, "message": "no accounts in accounts.txt", "ok_n": 0, "fail_n": 0}

    batch = list(full_accounts)
    if emails:
        want = {e.strip().lower() for e in emails if e.strip()}
        batch = [a for a in batch if a["email"].lower() in want]
    if only_without_cpa:
        cpa_emails: set[str] = set()
        cpa_dir = root / "cpa"
        if cpa_dir.is_dir():
            for p in cpa_dir.glob("xai-*.json"):
                try:
                    d = json.loads(p.read_text(encoding="utf-8"))
                    em = str(d.get("email") or "").strip().lower()
                    if em:
                        cpa_emails.add(em)
                except Exception:
                    continue
        batch = [a for a in batch if a["email"].lower() not in cpa_emails]
    if limit and limit > 0:
        batch = batch[:limit]

    fps = load_fingerprint_map(root / "browser-fingerprints.json")
    pool = _load_proxy_pool()
    rr = _ProxyRR(pool)
    workers = max(1, min(int(workers), 16))

    if progress:
        print(
            f"[*] SSO relogin batch={len(batch)} / total_file={len(full_accounts)} "
            f"workers={workers} proxies={len(pool)}",
            flush=True,
        )

    results_by: dict[str, dict[str, Any]] = {}
    ok_n = fail_n = 0
    lock = threading.Lock()
    done = 0
    total = len(batch)
    t0 = time.time()

    def one(acc: dict[str, str]) -> dict[str, Any]:
        return login_once(acc["email"], acc["password"], proxy=rr.next())

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(one, a): a for a in batch}
        for fut in as_completed(futs):
            acc = futs[fut]
            try:
                res = fut.result()
            except Exception as exc:
                res = {"ok": False, "email": acc["email"], "error": str(exc)[:240]}
            key = acc["email"].lower()
            with lock:
                results_by[key] = res
                done += 1
                if res.get("ok"):
                    ok_n += 1
                    mark = "✓"
                else:
                    fail_n += 1
                    mark = "✗"
                if progress:
                    err = f" {str(res.get('error') or '')[:70]}" if not res.get("ok") else ""
                    print(f"[{done}/{total}] {mark} {acc['email']}{err}", flush=True)

    # merge into full list
    merged: list[dict[str, str]] = []
    for acc in full_accounts:
        key = acc["email"].lower()
        res = results_by.get(key)
        if res and res.get("ok") and res.get("sso"):
            merged.append(
                {
                    "email": acc["email"],
                    "password": acc.get("password") or "",
                    "sso": str(res["sso"]),
                }
            )
        else:
            merged.append(acc)

    written = write_back(root, merged, results_by, fps)
    elapsed = round(time.time() - t0, 2)
    return {
        "ok": ok_n > 0 and fail_n == 0,
        "ok_n": ok_n,
        "fail_n": fail_n,
        "total": total,
        "elapsed_sec": elapsed,
        "written": written,
        "results": list(results_by.values()),
        "message": f"relogin SSO：成功 {ok_n} · 失败 {fail_n} · {elapsed}s",
    }


def main(argv: list[str] | None = None) -> int:
    import argparse

    argv = list(sys.argv[1:] if argv is None else argv)
    ap = argparse.ArgumentParser(
        description="Re-login accounts.txt passwords → fresh SSO (write accounts/grok/auth-sessions)"
    )
    ap.add_argument("--limit", type=int, default=0, help="max accounts (0=all)")
    ap.add_argument("--workers", type=int, default=2, help="parallel logins (default 2; Turnstile-heavy)")
    ap.add_argument("--email", action="append", default=[], help="only these emails (repeatable)")
    ap.add_argument("--emails-file", default="", help="file of emails to relogin")
    ap.add_argument(
        "--only-without-cpa",
        action="store_true",
        help="skip emails that already have keys/cpa/xai-*.json",
    )
    ap.add_argument("--no-progress", action="store_true")
    ap.add_argument(
        "--convert",
        action="store_true",
        help="after relogin, run SSO→CPA convert for successes",
    )
    ap.add_argument("--convert-workers", type=int, default=0, help="convert concurrency")
    args = ap.parse_args(argv)

    emails = list(args.email or [])
    if args.emails_file:
        p = Path(args.emails_file)
        if p.is_file():
            for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if ":" in line:
                    line = line.split(":", 1)[0].strip()
                if "@" in line:
                    emails.append(line)

    out = relogin_batch(
        limit=args.limit,
        workers=max(1, args.workers),
        emails=emails or None,
        only_without_cpa=bool(args.only_without_cpa),
        progress=not args.no_progress,
    )
    print(json.dumps({k: v for k, v in out.items() if k != "results"}, ensure_ascii=False, indent=2))
    fails = [r for r in (out.get("results") or []) if not r.get("ok")]
    if fails:
        print(f"[!] fails ({len(fails)}):", file=sys.stderr, flush=True)
        for r in fails[:10]:
            print(f"    {r.get('email')}: {r.get('error')}", file=sys.stderr, flush=True)

    if args.convert and int(out.get("ok_n") or 0) > 0:
        from grok_register.sso.export import convert_sso_to_product

        cw = args.convert_workers if args.convert_workers > 0 else None
        # convert only emails that just got new sso
        ok_emails = [
            str(r.get("email"))
            for r in (out.get("results") or [])
            if r.get("ok") and r.get("email")
        ]
        # write temp emails file
        tmp = key_dir() / ".relogin-convert-emails.txt"
        tmp.write_text("\n".join(ok_emails) + "\n", encoding="utf-8")
        print(f"[*] convert {len(ok_emails)} fresh SSO → CPA …", flush=True)
        conv = convert_sso_to_product(
            formats=["cpa"],
            only_pending=True,
            limit=len(ok_emails),
            workers=cw,
            enroll=True,
            rebuild=True,
            show_progress=not args.no_progress,
            emails_file=str(tmp),
        )
        print(json.dumps({k: v for k, v in conv.items() if k != "results"}, ensure_ascii=False, indent=2))
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        return 0 if conv.get("ok") or int(conv.get("ok_count") or 0) > 0 else 1

    return 0 if int(out.get("ok_n") or 0) > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
