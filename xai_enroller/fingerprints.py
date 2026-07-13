import hashlib
import json
import os
import re
import secrets
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path


BROWSER_FINGERPRINT_FILENAME = "browser-fingerprints.json"

_FINGERPRINT_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{8,128}$")
_store_lock = threading.Lock()

_LOCALE_PROFILES = (
    ("en-US", "en-US,en;q=0.9"),
    ("en-GB", "en-GB,en;q=0.9"),
    ("en-CA", "en-CA,en-US;q=0.8,en;q=0.7"),
    ("en-AU", "en-AU,en;q=0.9"),
    ("zh-CN", "zh-CN,zh;q=0.9,en;q=0.7"),
    ("zh-TW", "zh-TW,zh;q=0.9,en;q=0.7"),
)
_TIMEZONES = (
    "America/Los_Angeles",
    "America/New_York",
    "America/Toronto",
    "Europe/London",
    "Europe/Berlin",
    "Asia/Singapore",
    "Asia/Tokyo",
    "Asia/Shanghai",
    "Australia/Sydney",
)
_VIEWPORTS = (
    (1280, 720),
    (1366, 768),
    (1440, 900),
    (1536, 864),
    (1600, 900),
    (1680, 1050),
    (1920, 1080),
)
_DEVICE_SCALE_FACTORS = (1, 1.25, 1.5, 2)


def normalize_account_key(source_id: str) -> str:
    return str(source_id or "").strip().lower()


def valid_browser_fingerprint_id(value) -> bool:
    return isinstance(value, str) and bool(_FINGERPRINT_ID_RE.fullmatch(value))


def new_browser_fingerprint_id() -> str:
    return "bf_" + secrets.token_urlsafe(24).rstrip("=")


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


def _empty_document():
    return {"version": 1, "accounts": {}}


def _coerce_document(raw):
    if not isinstance(raw, dict):
        return _empty_document()
    accounts = raw.get("accounts")
    if isinstance(accounts, dict):
        normalized = {}
        for email, record in accounts.items():
            key = normalize_account_key(email)
            if not key:
                continue
            if isinstance(record, str):
                fingerprint_id = record
                record = {"browser_fingerprint_id": fingerprint_id}
            elif isinstance(record, dict):
                fingerprint_id = record.get("browser_fingerprint_id")
            else:
                continue
            if not valid_browser_fingerprint_id(fingerprint_id):
                continue
            normalized[key] = {
                **record,
                "browser_fingerprint_id": fingerprint_id,
            }
        return {"version": 1, "accounts": normalized}

    normalized = {}
    for email, fingerprint_id in raw.items():
        key = normalize_account_key(email)
        if key and valid_browser_fingerprint_id(fingerprint_id):
            normalized[key] = {"browser_fingerprint_id": fingerprint_id}
    return {"version": 1, "accounts": normalized}


def _read_document(path):
    try:
        return _coerce_document(json.loads(Path(path).read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        return _empty_document()


def _atomic_write_document(path, document):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    payload = json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, text=True
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
            os.unlink(temporary_name)


def load_browser_fingerprint_map(path):
    document = _read_document(path)
    return {
        email: record["browser_fingerprint_id"]
        for email, record in document.get("accounts", {}).items()
        if valid_browser_fingerprint_id(record.get("browser_fingerprint_id"))
    }


def get_browser_fingerprint(path, source_id):
    key = normalize_account_key(source_id)
    if not key:
        return None
    return load_browser_fingerprint_map(path).get(key)


def get_or_create_browser_fingerprint(path, source_id, browser_fingerprint_id=None):
    key = normalize_account_key(source_id)
    if not key:
        raise ValueError("source id is required")
    with _store_lock:
        document = _read_document(path)
        accounts = document.setdefault("accounts", {})
        existing = accounts.get(key)
        if isinstance(existing, dict) and valid_browser_fingerprint_id(
            existing.get("browser_fingerprint_id")
        ):
            return existing["browser_fingerprint_id"]

        fingerprint_id = (
            browser_fingerprint_id
            if valid_browser_fingerprint_id(browser_fingerprint_id)
            else new_browser_fingerprint_id()
        )
        now = _utc_now()
        accounts[key] = {
            "browser_fingerprint_id": fingerprint_id,
            "created_at": now,
            "updated_at": now,
        }
        _atomic_write_document(path, document)
        return fingerprint_id


def browser_context_options(browser_fingerprint_id=None, *, proxy=None):
    options = {}
    if valid_browser_fingerprint_id(browser_fingerprint_id):
        digest = hashlib.sha256(browser_fingerprint_id.encode("utf-8")).digest()
        locale, accept_language = _LOCALE_PROFILES[digest[0] % len(_LOCALE_PROFILES)]
        width, height = _VIEWPORTS[digest[1] % len(_VIEWPORTS)]
        width += (digest[2] % 5) * 8
        height += (digest[3] % 5) * 6
        options.update(
            {
                "viewport": {"width": width, "height": height},
                "screen": {"width": width, "height": height},
                "locale": locale,
                "timezone_id": _TIMEZONES[digest[4] % len(_TIMEZONES)],
                "color_scheme": "dark" if digest[5] % 5 == 0 else "light",
                "device_scale_factor": _DEVICE_SCALE_FACTORS[
                    digest[6] % len(_DEVICE_SCALE_FACTORS)
                ],
                "is_mobile": False,
                "has_touch": False,
                "extra_http_headers": {"Accept-Language": accept_language},
            }
        )
    if proxy:
        options["proxy"] = proxy
    return options
