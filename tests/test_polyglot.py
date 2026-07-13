"""Mandatory Python + Go + Rust stack gate."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from grok_register import polyglot


ROOT = Path(__file__).resolve().parents[1]


def test_stack_status_shape():
    status = polyglot.stack_status()
    assert "ok" in status
    assert set(status["components"]) >= {
        "python",
        "go_proxy_worker",
        "go_register_worker",
        "rust_inventory_worker",
    }


def test_require_polyglot_soft_mode(monkeypatch):
    monkeypatch.setenv("POLYGLOT_REQUIRED", "0")
    # Soft mode never raises even if missing
    st = polyglot.require_polyglot_stack(hard=False)
    assert isinstance(st, dict)


@pytest.mark.skipif(
    not (ROOT / "native" / "inventory-worker" / "inventory-worker").is_file()
    and not (ROOT / "native" / "inventory-worker" / "target" / "release" / "inventory-worker").is_file(),
    reason="rust inventory-worker not built",
)
def test_rust_inventory_scan_and_rebuild(tmp_path):
    keys = tmp_path / "keys"
    keys.mkdir()
    (keys / "accounts.txt").write_text("a@ex.com:pw:sso\n", encoding="utf-8")
    sub = keys / "sub2api"
    sub.mkdir()
    (sub / "xai-test.sub2api.json").write_text(
        """{
  "exported_at": "2026-01-01T00:00:00Z",
  "proxies": [],
  "accounts": [{
    "name": "a@ex.com",
    "platform": "grok",
    "type": "oauth",
    "credentials": {"access_token": "at", "refresh_token": "rt", "email": "a@ex.com"},
    "extra": {"email": "a@ex.com", "subject": "sub"}
  }]
}
""",
        encoding="utf-8",
    )
    cpa = keys / "cpa"
    cpa.mkdir()
    (cpa / "xai-test.json").write_text(
        '{"type":"xai","email":"a@ex.com","access_token":"at","refresh_token":"rt","sub":"sub"}',
        encoding="utf-8",
    )

    data = polyglot.rust_scan_accounts(keys)
    assert data.get("ok") is True
    assert data.get("engine") == "rust"
    assert data["summary"]["total"] >= 1

    rebuilt = polyglot.rust_rebuild_bundles(keys)
    assert rebuilt.get("ok") is True
    assert (keys / "sub2api" / "accounts.sub2api.json").is_file()
    assert not (keys / "cpa" / "accounts.cpa.json").is_file()
    assert (keys / "cpa" / "xai-test.json").is_file()


def test_polyglot_gate_shell_script():
    script = ROOT / "scripts" / "polyglot_gate.sh"
    assert script.is_file()
    completed = subprocess.run(
        ["bash", str(script), "json"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0
    assert "python" in completed.stdout
