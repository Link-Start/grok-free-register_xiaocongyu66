"""CLIProxyAPI single-file import + token refresh unit tests."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from grok_register import cliproxyapi as cpa


def _write(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")


def _sample_doc(**overrides):
    doc = {
        "type": "xai",
        "access_token": "old-access",
        "refresh_token": "rt-1",
        "id_token": "id-1",
        "token_type": "Bearer",
        "expires_in": 21600,
        "expired": (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "last_refresh": "2026-01-01T00:00:00Z",
        "sub": "sub-1",
        "base_url": "https://api.x.ai/v1",
        "token_endpoint": "https://auth.x.ai/oauth2/token",
        "auth_kind": "oauth",
        "email": "a@example.com",
    }
    doc.update(overrides)
    return doc


def test_list_cpa_singles_skips_bundle(tmp_path):
    _write(tmp_path / "xai-aaa.json", _sample_doc())
    _write(
        tmp_path / "accounts.cpa.json",
        {"type": "cpa-auth-bundle", "accounts": []},
    )
    paths = cpa.list_cpa_singles(tmp_path)
    assert len(paths) == 1
    assert paths[0].name == "xai-aaa.json"


def test_needs_refresh_expired_and_lead():
    past = _sample_doc(
        expired=(datetime.now(timezone.utc) - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    future = _sample_doc(
        expired=(datetime.now(timezone.utc) + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    near = _sample_doc(
        expired=(datetime.now(timezone.utc) + timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    assert cpa.needs_refresh(past, lead_sec=300) is True
    assert cpa.needs_refresh(future, lead_sec=300) is False
    assert cpa.needs_refresh(near, lead_sec=300) is True


def test_apply_token_prefers_grok_cli_base():
    doc = _sample_doc()
    token = {
        "access_token": "new-at",
        "refresh_token": "new-rt",
        "id_token": "new-id",
        "token_type": "Bearer",
        "expires_in": 3600,
        "expires_at": int(datetime.now(timezone.utc).timestamp()) + 3600,
    }
    out = cpa.apply_token_to_document(doc, token, prefer_grok_cli_base=True)
    assert out["access_token"] == "new-at"
    assert out["refresh_token"] == "new-rt"
    assert out["base_url"] == cpa.GROK_CLI_BASE_URL
    assert out["headers"]["X-XAI-Token-Auth"] == "xai-grok-cli"
    assert out["type"] == "xai"


def test_import_file_copy_and_remove_bundle(tmp_path):
    src = tmp_path / "src"
    dst = tmp_path / "auths"
    _write(src / "xai-bbb.json", _sample_doc(email="b@example.com"))
    _write(dst / "accounts.cpa.json", {"type": "cpa-auth-bundle", "accounts": []})
    path = cpa.import_file_copy(src / "xai-bbb.json", dst)
    assert path.is_file()
    assert path.name == "xai-bbb.json"
    removed = cpa.remove_bundle_artifacts(dst)
    assert "accounts.cpa.json" in removed
    assert not (dst / "accounts.cpa.json").exists()


def test_run_once_refresh_and_import(tmp_path, monkeypatch):
    src = tmp_path / "cpa"
    auth = tmp_path / "auths"
    _write(src / "xai-ccc.json", _sample_doc())
    monkeypatch.setenv("CLIPROXYAPI_ENABLED", "1")
    monkeypatch.setenv("CLIPROXYAPI_SOURCE_DIR", str(src))
    monkeypatch.setenv("CLIPROXYAPI_AUTH_DIR", str(auth))
    monkeypatch.setenv("CLIPROXYAPI_AUTO_IMPORT", "1")
    monkeypatch.setenv("CLIPROXYAPI_AUTO_REFRESH", "1")
    monkeypatch.setenv("CLIPROXYAPI_PREFER_GROK_CLI_BASE", "1")

    fake_token = {
        "access_token": "fresh-at",
        "refresh_token": "fresh-rt",
        "id_token": "fresh-id",
        "token_type": "Bearer",
        "expires_in": 7200,
    }

    with patch.object(cpa, "refresh_access_token", return_value=fake_token):
        result = cpa.run_once(force_refresh=True)

    assert result["refreshed"] == 1
    assert result["imported"] == 1
    dest = auth / "xai-ccc.json"
    assert dest.is_file()
    loaded = json.loads(dest.read_text(encoding="utf-8"))
    assert loaded["access_token"] == "fresh-at"
    assert loaded["base_url"] == cpa.GROK_CLI_BASE_URL
    # source rewritten too
    src_doc = json.loads((src / "xai-ccc.json").read_text(encoding="utf-8"))
    assert src_doc["refresh_token"] == "fresh-rt"


def test_run_once_marks_revoked(tmp_path, monkeypatch):
    src = tmp_path / "cpa"
    auth = tmp_path / "auths"
    _write(src / "xai-ddd.json", _sample_doc())
    monkeypatch.setenv("CLIPROXYAPI_SOURCE_DIR", str(src))
    monkeypatch.setenv("CLIPROXYAPI_AUTH_DIR", str(auth))

    with patch.object(
        cpa,
        "refresh_access_token",
        side_effect=RuntimeError('refresh HTTP 400: {"error":"invalid_grant","error_description":"Refresh token has been revoked"}'),
    ):
        result = cpa.run_once(force_refresh=True, import_files=True)

    assert result["refresh_failed"] == 1
    assert result["revoked"] == 1
    # revoked should not be imported
    assert result["imported"] == 0
    assert not (auth / "xai-ddd.json").exists()


def test_load_document_rejects_bundle(tmp_path):
    p = tmp_path / "accounts.cpa.json"
    _write(p, {"type": "cpa-auth-bundle", "accounts": [{"type": "xai"}]})
    assert cpa.load_document(p) is None


def test_protocol_capabilities():
    from grok_register import xai_protocol_oauth as proto

    caps = proto.protocol_capabilities()
    assert caps["client_id"]
    assert "auth.x.ai" in caps["token_endpoint"]
    assert isinstance(caps["notes"], list)


def test_build_cliproxyapi_document_from_token():
    from grok_register import xai_protocol_oauth as proto

    token = {
        "access_token": "at",
        "refresh_token": "rt",
        "id_token": "id",
        "expires_in": 100,
        "expires_at": int(datetime.now(timezone.utc).timestamp()) + 100,
    }
    doc = proto.build_cliproxyapi_document(token, email="z@test.com")
    assert doc["type"] == "xai"
    assert doc["email"] == "z@test.com"
    assert doc["access_token"] == "at"
