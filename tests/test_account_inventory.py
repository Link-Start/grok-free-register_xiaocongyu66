"""Account inventory scan, bundle rebuild, and dashboard product APIs."""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

from grok_register import account_inventory as inv
from grok_register import dashboard


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _sample_sub2api(email: str) -> dict:
    return {
        "exported_at": "2026-07-12T00:00:00+00:00",
        "proxies": [],
        "accounts": [
            {
                "name": email,
                "platform": "grok",
                "type": "oauth",
                "credentials": {
                    "access_token": "at-1",
                    "refresh_token": "rt-1",
                    "email": email,
                },
                "extra": {"email": email, "subject": "sub-1"},
            }
        ],
    }


def _sample_cpa(email: str) -> dict:
    return {
        "type": "xai",
        "access_token": "at-1",
        "refresh_token": "rt-1",
        "email": email,
        "sub": "sub-1",
        "auth_kind": "oauth",
        "base_url": "https://api.x.ai/v1",
    }


def test_scan_accounts_merges_formats(tmp_path, monkeypatch):
    monkeypatch.setenv("KEY_EXPORT_DIR", str(tmp_path))
    _write(
        tmp_path / "accounts.txt",
        "a@example.com:pass:sso-token\nb@example.com:pass:sso-b\n",
    )
    _write(
        tmp_path / "sub2api" / "xai-aaa.sub2api.json",
        json.dumps(_sample_sub2api("a@example.com")),
    )
    _write(
        tmp_path / "cpa" / "xai-aaa.json",
        json.dumps(_sample_cpa("a@example.com")),
    )

    records = inv.scan_accounts(tmp_path)
    by_email = {r.email: r for r in records}
    assert set(by_email) == {"a@example.com", "b@example.com"}

    a = by_email["a@example.com"]
    assert a.status == "oauth_ready"
    assert set(a.formats) == {"legacy", "sub2api", "cpa"}
    assert a.has_sso and a.has_access_token and a.has_refresh_token
    assert a.fingerprint == "xai-aaa"

    b = by_email["b@example.com"]
    assert b.status == "oauth_pending"
    assert b.formats == ["legacy"]

    summary = inv.inventory_summary(records)
    assert summary["total"] == 2
    assert summary["by_status"]["oauth_ready"] == 1
    assert summary["by_status"]["oauth_pending"] == 1


def test_rebuild_bundles_and_download_spec(tmp_path, monkeypatch):
    monkeypatch.setenv("KEY_EXPORT_DIR", str(tmp_path))
    email = "z@example.com"
    _write(
        tmp_path / "sub2api" / "xai-bbb.sub2api.json",
        json.dumps(_sample_sub2api(email)),
    )
    _write(tmp_path / "cpa" / "xai-bbb.json", json.dumps(_sample_cpa(email)))
    _write(tmp_path / "accounts.txt", f"{email}:p:sso\n")

    sub = inv.rebuild_sub2api_bundle(tmp_path)
    assert sub.is_file()
    doc = json.loads(sub.read_text(encoding="utf-8"))
    assert len(doc["accounts"]) == 1
    assert doc["accounts"][0]["credentials"]["email"] == email

    # merge bundles permanently removed — only purge
    cpa_json, cpa_zip = inv.rebuild_cpa_bundle(tmp_path)
    assert not cpa_json.is_file()
    assert not cpa_zip.is_file()

    paths = inv.ensure_bundles(rebuild=True)
    assert Path(paths["sub2api_json"]).is_file()
    assert "cpa_json" not in paths
    assert "cpa_zip" not in paths
    assert int(str(paths.get("cpa_singles") or 0)) >= 1
    assert Path(paths["legacy_txt"]).is_file()

    p, media, name = inv.download_spec("sub2api")
    assert p.name == "accounts.sub2api.json"
    assert "json" in media
    assert name == "accounts.sub2api.json"

    p, media, name = inv.download_spec("cpa_zip")
    assert p.suffix == ".zip"
    assert media == "application/zip"
    assert name == "xai-singles.zip"
    assert zipfile.is_zipfile(p)


def test_dashboard_accounts_payload(tmp_path, monkeypatch):
    monkeypatch.setenv("KEY_EXPORT_DIR", str(tmp_path))
    _write(tmp_path / "accounts.txt", "only@example.com:pw:sso\n")
    payload = dashboard.build_accounts_payload()
    assert payload["ok"] is True
    assert payload["total"] == 1
    assert payload["accounts"][0]["email"] == "only@example.com"
    assert payload["accounts"][0]["status"] == "oauth_pending"
    assert "sub2api" in payload["downloads"]


def test_dashboard_overview_includes_products(monkeypatch, tmp_path):
    path = tmp_path / "runtime-status.json"
    monkeypatch.setenv("RUNTIME_STATUS_FILE", str(path))
    monkeypatch.setenv("KEY_EXPORT_DIR", str(tmp_path / "keys"))
    (tmp_path / "keys").mkdir()
    (tmp_path / "keys" / "accounts.txt").write_text("x@y.z:a:b\n", encoding="utf-8")
    from grok_register import runtime_status

    runtime_status.publish({"service": "register", "running": False, "metrics": {}})
    monkeypatch.setattr(dashboard, "process_alive", lambda pid=None: False)
    overview = dashboard.build_overview()
    assert overview["products"]["cpa_zip"].endswith("format=cpa_zip")
    assert overview["accounts"]["count"] >= 1
    assert "by_status" in overview["accounts"]
