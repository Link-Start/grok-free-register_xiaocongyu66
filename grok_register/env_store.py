"""
Read / write project .env for the control plane.
Preserves unknown keys and comment blocks as much as practical.
"""
from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Any

from grok_register.config_catalog import CATALOG_BY_KEY, SECRET_KEYS, mask_secret

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV = PROJECT_ROOT / ".env"


def env_path() -> Path:
    raw = (os.environ.get("GROK_ENV_FILE") or "").strip()
    if raw:
        p = Path(raw).expanduser()
        return p if p.is_absolute() else PROJECT_ROOT / p
    return DEFAULT_ENV


_LINE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


def parse_env_file(path: Path | None = None) -> dict[str, str]:
    path = path or env_path()
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _LINE_RE.match(line)
        if not m:
            continue
        key, val = m.group(1), m.group(2)
        # strip optional surrounding quotes
        if len(val) >= 2 and val[0] == val[-1] and val[0] in {"'", '"'}:
            val = val[1:-1]
        values[key] = val
    return values


def load_config_view(*, reveal_secrets: bool = False) -> dict[str, Any]:
    """Merge catalog defaults + .env + process env for dashboard display."""
    file_vals = parse_env_file()
    items = []
    for meta in CATALOG_BY_KEY.values():
        key = meta["key"]
        # priority: process env (if set) then file then default
        if key in os.environ and os.environ.get(key) is not None and str(os.environ.get(key)).strip() != "":
            # only prefer process env when explicitly present and non-empty for secrets display?
            value = os.environ.get(key, "")
            source = "process"
        elif key in file_vals:
            value = file_vals[key]
            source = "file"
        else:
            value = str(meta.get("default") or "")
            source = "default"
        display = value
        if meta.get("type") == "secret" and not reveal_secrets:
            display = mask_secret(value)
        items.append(
            {
                **meta,
                "value": display,
                "has_value": bool(value),
                "source": source,
                "label": meta.get("label") or meta["key"],
                "simple": bool(meta.get("simple")),
                "options": meta.get("options") or [],
                "placeholder": meta.get("placeholder") or "",
            }
        )
    # also surface unknown keys present only in .env
    known = set(CATALOG_BY_KEY)
    extras = []
    for key, value in sorted(file_vals.items()):
        if key in known:
            continue
        extras.append(
            {
                "key": key,
                "group": "extra",
                "type": "secret" if "KEY" in key or "TOKEN" in key or "PASS" in key else "str",
                "default": "",
                "desc": "未在目录中登记的 .env 键",
                "restart": True,
                "value": mask_secret(value) if ("KEY" in key or "TOKEN" in key or "PASS" in key) and not reveal_secrets else value,
                "has_value": bool(value),
                "source": "file",
            }
        )
    return {
        "path": str(env_path()),
        "items": items,
        "extras": extras,
        "groups": sorted({i["group"] for i in items} | ({"extra"} if extras else set())),
    }


def update_env_values(updates: dict[str, Any], *, allow_unknown: bool = True) -> dict[str, Any]:
    """Patch .env keys. Empty string deletes override (writes empty).

    Secret fields: value of '***' or masked placeholder is ignored (keep old).
    """
    path = env_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    existing_lines: list[str] = []
    if path.is_file():
        existing_lines = path.read_text(encoding="utf-8", errors="replace").splitlines()

    current = parse_env_file(path)
    changed = []
    skipped = []

    normalized: dict[str, str] = {}
    for key, raw in (updates or {}).items():
        key = str(key).strip()
        if not key or not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
            skipped.append({"key": key, "reason": "invalid key"})
            continue
        if key not in CATALOG_BY_KEY and not allow_unknown:
            skipped.append({"key": key, "reason": "unknown key"})
            continue
        if raw is None:
            continue
        value = str(raw)
        # ignore unchanged masked secrets
        if key in SECRET_KEYS or key in current and key in SECRET_KEYS:
            if value == mask_secret(current.get(key, "")) or set(value) <= {"*"} and "*" in value:
                skipped.append({"key": key, "reason": "masked secret unchanged"})
                continue
        if key in CATALOG_BY_KEY and CATALOG_BY_KEY[key].get("type") == "bool":
            value = "1" if value.strip().lower() in {"1", "true", "yes", "on"} else (
                "0" if value.strip().lower() in {"0", "false", "no", "off"} else value.strip()
            )
        normalized[key] = value

    # rebuild file: update existing assignment lines, append new keys
    seen = set()
    out_lines: list[str] = []
    for line in existing_lines:
        m = _LINE_RE.match(line.strip()) if line.strip() and not line.strip().startswith("#") else None
        if not m:
            out_lines.append(line)
            continue
        key = m.group(1)
        if key in normalized:
            out_lines.append(f"{key}={_escape_env(normalized[key])}")
            seen.add(key)
            if current.get(key) != normalized[key]:
                changed.append(key)
        else:
            out_lines.append(line)
            seen.add(key)

    for key, value in normalized.items():
        if key in seen:
            continue
        out_lines.append(f"{key}={_escape_env(value)}")
        changed.append(key)

    text = "\n".join(out_lines) + ("\n" if out_lines else "")
    # atomic write
    fd, tmp = tempfile.mkstemp(prefix=".env.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        Path(tmp).replace(path)
    finally:
        try:
            Path(tmp).unlink(missing_ok=True)
        except Exception:
            pass

    # refresh process env for dashboard itself (register still needs restart)
    for key, value in normalized.items():
        os.environ[key] = value

    needs_restart = any(
        CATALOG_BY_KEY.get(k, {}).get("restart", True) for k in changed
    )
    return {
        "ok": True,
        "path": str(path),
        "changed": changed,
        "skipped": skipped,
        "needs_restart": needs_restart,
    }


def _escape_env(value: str) -> str:
    if value == "":
        return ""
    if re.search(r'[\s#"\']', value):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return value


# re-export pattern for line matching
_LINE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
