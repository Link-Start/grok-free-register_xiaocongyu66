#!/usr/bin/env python3
"""通过 SSH 导出不含密码的注册会话；兼容历史裸 SSO 记录。"""

import argparse
import json
import os
import sys
import time
from pathlib import Path


COOKIE_FIELDS = frozenset(
    {
        "name",
        "value",
        "url",
        "domain",
        "path",
        "expires",
        "httpOnly",
        "secure",
        "sameSite",
    }
)
FINGERPRINTS_FILENAME = "browser-fingerprints.json"
MAX_RECORD_BYTES = 256 * 1024
LEGACY_COOKIE_SCOPE = {
    "name": "sso",
    "domain": "accounts.x.ai",
    "path": "/",
    "secure": True,
    "httpOnly": True,
    "sameSite": "Lax",
}


def _decode_json_line(raw, label):
    if len(raw) > MAX_RECORD_BYTES:
        raise ValueError(f"invalid {label} record")
    try:
        document = json.loads(raw.decode("utf-8"))
        email = document["email"]
        cookies = document["cookies"]
    except (UnicodeDecodeError, TypeError, ValueError, KeyError) as exc:
        raise ValueError(f"invalid {label} record") from exc
    if not isinstance(email, str) or not email or not isinstance(cookies, list) or not cookies:
        raise ValueError(f"invalid {label} record")
    try:
        email.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"invalid {label} record") from exc
    normalized = []
    for cookie in cookies:
        if not isinstance(cookie, dict):
            raise ValueError(f"invalid {label} record")
        filtered = {key: cookie[key] for key in COOKIE_FIELDS if key in cookie}
        if not all(
            isinstance(filtered.get(key), str) and filtered[key]
            for key in ("name", "value")
        ):
            raise ValueError(f"invalid {label} record")
        scope = filtered.get("domain") or filtered.get("url")
        if not isinstance(scope, str) or not scope:
            raise ValueError(f"invalid {label} record")
        try:
            filtered["name"].encode("utf-8")
            filtered["value"].encode("utf-8")
            scope.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError(f"invalid {label} record") from exc
        normalized.append(filtered)
    result = {"email": email, "cookies": normalized}
    browser_fingerprint_id = document.get("browser_fingerprint_id")
    if browser_fingerprint_id is not None:
        if not isinstance(browser_fingerprint_id, str) or not browser_fingerprint_id:
            raise ValueError(f"invalid {label} record")
        try:
            browser_fingerprint_id.encode("ascii")
        except UnicodeEncodeError as exc:
            raise ValueError(f"invalid {label} record") from exc
        result["browser_fingerprint_id"] = browser_fingerprint_id
    return result


def _load_browser_fingerprints(path):
    if not path.exists():
        return {}
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    accounts = document.get("accounts") if isinstance(document, dict) else None
    if not isinstance(accounts, dict):
        return {}
    fingerprints = {}
    for email, record in accounts.items():
        if not isinstance(email, str):
            continue
        if isinstance(record, dict):
            browser_fingerprint_id = record.get("browser_fingerprint_id")
        else:
            browser_fingerprint_id = record
        if isinstance(browser_fingerprint_id, str) and browser_fingerprint_id:
            fingerprints[email.strip().lower()] = browser_fingerprint_id
    return fingerprints


def _attach_browser_fingerprint(document, fingerprints):
    if document.get("browser_fingerprint_id"):
        return document
    browser_fingerprint_id = fingerprints.get(document["email"].strip().lower())
    if not browser_fingerprint_id:
        return document
    return {**document, "browser_fingerprint_id": browser_fingerprint_id}


def _complete_lines(data):
    """Return newline-terminated records and the unconsumed trailing bytes."""
    parts = data.split(b"\n")
    return parts[:-1], parts[-1]


def _read_complete_file(path):
    if not path.exists():
        return []
    lines, _incomplete = _complete_lines(path.read_bytes())
    return [line for line in lines if line]


def load_snapshots(path, *, raw_lines=None, fingerprints=None):
    snapshots = {}
    scopes = {}
    fingerprints = fingerprints or {}
    lines = _read_complete_file(path) if raw_lines is None else raw_lines
    for line in lines:
        document = _decode_json_line(line, "session")
        document = _attach_browser_fingerprint(document, fingerprints)
        email = document["email"]
        cookies = document["cookies"]
        if email in snapshots:
            continue
        snapshots[email] = document
        for cookie in cookies:
            name = cookie.get("name")
            domain = cookie.get("domain")
            if name in {"sso", "sso-rw"} and domain:
                scopes[(name, domain)] = {
                    "name": name,
                    "domain": domain,
                    "path": cookie.get("path", "/"),
                    "secure": bool(cookie.get("secure", True)),
                    "httpOnly": bool(cookie.get("httpOnly", True)),
                    "sameSite": cookie.get("sameSite", "Lax"),
                }
    return snapshots, scopes


def export_sessions(sessions_path, accounts_path, *, session_lines=None):
    fingerprints = _load_browser_fingerprints(sessions_path.parent / FINGERPRINTS_FILENAME)
    snapshots, scopes = load_snapshots(
        sessions_path,
        raw_lines=session_lines,
        fingerprints=fingerprints,
    )
    for document in snapshots.values():
        yield document
    if not accounts_path.exists():
        return
    legacy_scopes = list(scopes.values()) or [LEGACY_COOKIE_SCOPE]
    for raw in _read_complete_file(accounts_path):
        try:
            email, _password, sso = raw.decode("utf-8").rsplit(":", 2)
        except (UnicodeDecodeError, ValueError) as exc:
            raise ValueError("invalid account record") from exc
        if not email or not sso or email in snapshots:
            continue
        cookies = [{**scope, "value": sso} for scope in legacy_scopes]
        yield _attach_browser_fingerprint(
            {"email": email, "cookies": cookies},
            fingerprints,
        )


def _write_document(document):
    payload = json.dumps(document, separators=(",", ":"))
    if len(payload.encode("utf-8")) > MAX_RECORD_BYTES:
        raise ValueError("invalid session record")
    print(payload, flush=False)


def export_and_follow(sessions_path, accounts_path, *, poll_seconds=0.25):
    """Emit a complete snapshot, then losslessly follow the same JSONL fd."""
    sessions_path.parent.mkdir(parents=True, exist_ok=True)
    if not sessions_path.exists():
        fd = os.open(sessions_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
    os.chmod(sessions_path, 0o600)
    stream = sessions_path.open("rb")

    with stream:
        opened = os.fstat(stream.fileno())
        initial_lines, pending = _complete_lines(stream.read())
        if len(pending) > MAX_RECORD_BYTES:
            raise ValueError("invalid session record")
        for document in export_sessions(
            sessions_path,
            accounts_path,
            session_lines=[line for line in initial_lines if line],
        ):
            _write_document(document)
        sys.stdout.flush()

        while True:
            chunk = stream.read()
            if chunk:
                complete, pending = _complete_lines(pending + chunk)
                if len(pending) > MAX_RECORD_BYTES:
                    raise ValueError("invalid session record")
                for raw in complete:
                    if raw:
                        document = _decode_json_line(raw, "session")
                        fingerprints = _load_browser_fingerprints(
                            sessions_path.parent / FINGERPRINTS_FILENAME
                        )
                        _write_document(
                            _attach_browser_fingerprint(document, fingerprints)
                        )
                sys.stdout.flush()
                continue

            try:
                current = sessions_path.stat()
            except FileNotFoundError:
                return 3
            if (
                current.st_dev != opened.st_dev
                or current.st_ino != opened.st_ino
                or current.st_size < stream.tell()
            ):
                return 3
            time.sleep(poll_seconds)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--follow", action="store_true")
    parser.add_argument("sessions_path", type=Path)
    parser.add_argument("accounts_path", type=Path)
    args = parser.parse_args()
    try:
        if args.follow:
            raise SystemExit(export_and_follow(args.sessions_path, args.accounts_path))
        for document in export_sessions(args.sessions_path, args.accounts_path):
            _write_document(document)
    except ValueError:
        raise SystemExit(4) from None
    except BrokenPipeError:
        try:
            sys.stdout.close()
        finally:
            raise SystemExit(0)


if __name__ == "__main__":
    main()
