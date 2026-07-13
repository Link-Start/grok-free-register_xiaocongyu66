"""One-click CPA / sub2api conversion helpers."""
from __future__ import annotations

import json
from pathlib import Path

from grok_register import account_convert as ac


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_oauth_copy_sub2api_to_cpa(tmp_path, monkeypatch):
    monkeypatch.setenv("KEY_EXPORT_DIR", str(tmp_path))
    email = "a@example.com"
    sub = {
        "exported_at": "2026-01-01T00:00:00Z",
        "proxies": [],
        "accounts": [
            {
                "name": email,
                "platform": "grok",
                "type": "oauth",
                "credentials": {
                    "access_token": "at",
                    "refresh_token": "rt",
                    "email": email,
                    "expires_at": "2026-01-02T00:00:00Z",
                },
                "extra": {"email": email, "subject": "sub-1"},
            }
        ],
    }
    _write(tmp_path / "sub2api" / "xai-aaa.sub2api.json", json.dumps(sub))
    _write(tmp_path / "accounts.txt", f"{email}:pw:sso-token\n")

    out = ac.convert_account(email, ["cpa"], allow_enroll=False)
    assert out["ok"] is True
    assert out["method"] == "oauth_copy"
    cpa_files = list((tmp_path / "cpa").glob("xai-*.json"))
    assert cpa_files
    doc = json.loads(cpa_files[0].read_text(encoding="utf-8"))
    assert doc["email"] == email
    assert doc["refresh_token"] == "rt"


def test_oauth_copy_cpa_to_sub2api(tmp_path, monkeypatch):
    monkeypatch.setenv("KEY_EXPORT_DIR", str(tmp_path))
    email = "b@example.com"
    cpa = {
        "type": "xai",
        "access_token": "at2",
        "refresh_token": "rt2",
        "email": email,
        "sub": "sub-2",
        "auth_kind": "oauth",
        "base_url": "https://api.x.ai/v1",
        "expired": "2026-01-02T00:00:00Z",
    }
    _write(tmp_path / "cpa" / "xai-bbb.json", json.dumps(cpa))

    out = ac.convert_account(email, ["sub2api"], allow_enroll=False)
    assert out["ok"] is True
    files = list((tmp_path / "sub2api").glob("*.sub2api.json"))
    assert any(p.name != "accounts.sub2api.json" for p in files)
    assert (tmp_path / "sub2api" / "accounts.sub2api.json").is_file()


def test_convert_accounts_skips_complete(tmp_path, monkeypatch):
    monkeypatch.setenv("KEY_EXPORT_DIR", str(tmp_path))
    email = "c@example.com"
    _write(
        tmp_path / "sub2api" / "xai-ccc.sub2api.json",
        json.dumps(
            {
                "exported_at": "2026-01-01T00:00:00Z",
                "proxies": [],
                "accounts": [
                    {
                        "name": email,
                        "credentials": {
                            "access_token": "at",
                            "refresh_token": "rt",
                            "email": email,
                        },
                        "extra": {"email": email, "subject": "s"},
                    }
                ],
            }
        ),
    )
    _write(
        tmp_path / "cpa" / "xai-ccc.json",
        json.dumps(
            {
                "type": "xai",
                "email": email,
                "access_token": "at",
                "refresh_token": "rt",
                "sub": "s",
            }
        ),
    )
    out = ac.convert_accounts(None, ["cpa", "sub2api"], allow_enroll=False, rebuild=False)
    assert out["fail_count"] == 0
