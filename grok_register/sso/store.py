"""Canonical SSO store: keys/sso.txt as email:sso (one line per email).

Rules:
  - One email → at most one line (case-insensitive email key)
  - upsert: remove any old line for that email, append new email:sso
  - convert / inventory-worker should read this file first
  - accounts.txt keeps email:password for relogin (no SSO required there)
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

_lock = threading.Lock()

SSO_FILENAME = "sso.txt"
ACCOUNTS_FILENAME = "accounts.txt"
GROK_FILENAME = "grok.txt"


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def key_export_dir() -> Path:
    raw = (os.environ.get("KEY_EXPORT_DIR") or "keys").strip() or "keys"
    p = Path(raw).expanduser()
    return p if p.is_absolute() else _project_root() / p


def sso_file_path(root: Path | None = None) -> Path:
    root = root or key_export_dir()
    return root / SSO_FILENAME


def normalize_sso(value: str) -> str:
    value = (value or "").strip()
    if value.lower().startswith("sso="):
        value = value[4:].strip()
    if ";" in value:
        value = value.split(";", 1)[0].strip()
    return value.replace("\r", "").replace("\n", "").replace("\x00", "")


def parse_sso_line(line: str) -> tuple[str, str] | None:
    """Return (email, sso) or None. Accepts email:sso or email:password:sso."""
    line = (line or "").strip()
    if not line or line.startswith("#"):
        return None
    if ":" not in line:
        return None
    # email:sso  — sso is JWT and may contain no extra colons in practice,
    # but password:sso form has 2+ colons. Prefer last segment as sso when 3+ parts.
    parts = line.split(":")
    email = parts[0].strip()
    if not email or "@" not in email:
        return None
    if len(parts) == 2:
        sso = normalize_sso(parts[1])
    else:
        # email:password:sso...  (sso may theoretically contain ':')
        sso = normalize_sso(":".join(parts[2:]))
    if not sso:
        return None
    return email, sso


def load_sso_map(root: Path | None = None) -> dict[str, tuple[str, str]]:
    """email_lower → (email_display, sso). Last line wins if duplicates exist."""
    path = sso_file_path(root)
    out: dict[str, tuple[str, str]] = {}
    if not path.is_file():
        return out
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in text.splitlines():
        parsed = parse_sso_line(line)
        if not parsed:
            continue
        email, sso = parsed
        out[email.lower()] = (email, sso)
    return out


def load_sso_list(root: Path | None = None) -> list[dict[str, str]]:
    """Ordered unique list from sso.txt (file order, last occurrence kept position-wise)."""
    path = sso_file_path(root)
    if not path.is_file():
        return []
    order: list[str] = []
    by: dict[str, dict[str, str]] = {}
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    for line in lines:
        parsed = parse_sso_line(line)
        if not parsed:
            continue
        email, sso = parsed
        key = email.lower()
        if key not in by:
            order.append(key)
        by[key] = {"email": email, "sso": sso}
    return [by[k] for k in order]


def upsert_sso(email: str, sso: str, *, root: Path | None = None) -> Path:
    """Replace any existing line for email; append email:sso. Returns path."""
    email = (email or "").strip()
    sso = normalize_sso(sso)
    if not email or "@" not in email or not sso:
        raise ValueError("email and sso required")
    root = root or key_export_dir()
    root.mkdir(parents=True, exist_ok=True)
    path = sso_file_path(root)
    key = email.lower()
    with _lock:
        kept: list[str] = []
        if path.is_file():
            try:
                for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                    parsed = parse_sso_line(line)
                    if parsed and parsed[0].lower() == key:
                        continue  # drop old
                    if line.strip():
                        kept.append(line.rstrip("\n"))
            except OSError:
                pass
        kept.append(f"{email}:{sso}")
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text("\n".join(kept) + "\n", encoding="utf-8")
        tmp.replace(path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    return path


def remove_sso(email: str, *, root: Path | None = None) -> bool:
    """Delete email line from sso.txt. Returns True if something was removed."""
    email = (email or "").strip()
    if not email:
        return False
    root = root or key_export_dir()
    path = sso_file_path(root)
    if not path.is_file():
        return False
    key = email.lower()
    removed = False
    with _lock:
        kept: list[str] = []
        try:
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                parsed = parse_sso_line(line)
                if parsed and parsed[0].lower() == key:
                    removed = True
                    continue
                if line.strip():
                    kept.append(line.rstrip("\n"))
        except OSError:
            return False
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(("\n".join(kept) + "\n") if kept else "", encoding="utf-8")
        tmp.replace(path)
    return removed


def rewrite_grok_from_sso(root: Path | None = None) -> Path:
    """Regenerate grok.txt from sso.txt (SSO tokens only, same order)."""
    root = root or key_export_dir()
    rows = load_sso_list(root)
    path = root / GROK_FILENAME
    tokens = [r["sso"] for r in rows if r.get("sso")]
    tmp = path.with_suffix(".txt.tmp")
    tmp.write_text("\n".join(tokens) + ("\n" if tokens else ""), encoding="utf-8")
    tmp.replace(path)
    return path


def upsert_account_password(
    email: str,
    password: str,
    *,
    root: Path | None = None,
) -> Path:
    """Keep accounts.txt as email:password only (for relogin). Upsert by email."""
    email = (email or "").strip()
    password = (password or "").strip()
    if not email or "@" not in email:
        raise ValueError("email required")
    root = root or key_export_dir()
    root.mkdir(parents=True, exist_ok=True)
    path = root / ACCOUNTS_FILENAME
    key = email.lower()
    with _lock:
        kept: list[str] = []
        if path.is_file():
            try:
                for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                    raw = line.strip()
                    if not raw or raw.startswith("#"):
                        continue
                    parts = raw.split(":", 2)
                    em = parts[0].strip()
                    if em.lower() == key:
                        continue
                    # normalize legacy email:password:sso → email:password
                    if len(parts) >= 2 and "@" in em:
                        kept.append(f"{em}:{parts[1]}")
                    elif raw:
                        kept.append(raw)
            except OSError:
                pass
        if password:
            kept.append(f"{email}:{password}")
        else:
            # keep email-only marker only if we must — skip empty password
            pass
        tmp = path.with_suffix(".txt.tmp")
        tmp.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
        tmp.replace(path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    return path


def migrate_from_accounts_txt(root: Path | None = None, *, force: bool = False) -> dict[str, int]:
    """If sso.txt missing (or force), extract email:sso from accounts.txt / auth-sessions.

    Does not delete accounts.txt; also normalizes accounts to email:password.
    """
    root = root or key_export_dir()
    path = sso_file_path(root)
    if path.is_file() and path.stat().st_size > 0 and not force:
        n = len(load_sso_map(root))
        return {"migrated": 0, "existing": n, "skipped": 1}

    count = 0
    accounts = root / ACCOUNTS_FILENAME
    if accounts.is_file():
        for line in accounts.read_text(encoding="utf-8", errors="replace").splitlines():
            parts = line.strip().split(":", 2)
            if len(parts) >= 3 and "@" in parts[0]:
                email, password, sso = parts[0].strip(), parts[1], normalize_sso(parts[2])
                if email and sso:
                    upsert_sso(email, sso, root=root)
                    if password:
                        upsert_account_password(email, password, root=root)
                    count += 1
            elif len(parts) == 2 and "@" in parts[0]:
                # email:password only
                upsert_account_password(parts[0].strip(), parts[1], root=root)

    # auth-sessions: email + sso cookie
    sessions = root / "auth-sessions.jsonl"
    if sessions.is_file():
        import json

        for line in sessions.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                doc = json.loads(line)
            except Exception:
                continue
            email = str(doc.get("email") or "").strip()
            if not email or "@" not in email:
                continue
            sso = ""
            for c in doc.get("cookies") or []:
                if not isinstance(c, dict):
                    continue
                if str(c.get("name") or "").lower() in {"sso", "sso-rw"}:
                    sso = normalize_sso(str(c.get("value") or ""))
                    if sso:
                        break
            if sso:
                # only fill if missing
                if email.lower() not in load_sso_map(root):
                    upsert_sso(email, sso, root=root)
                    count += 1

    rewrite_grok_from_sso(root)
    return {"migrated": count, "existing": len(load_sso_map(root)), "skipped": 0}


def default_sso_file_for_convert(root: Path | None = None) -> str:
    """Path string for convert --sso-file; migrates once if needed."""
    root = root or key_export_dir()
    path = sso_file_path(root)
    if not path.is_file() or path.stat().st_size == 0:
        migrate_from_accounts_txt(root)
    return str(path)
