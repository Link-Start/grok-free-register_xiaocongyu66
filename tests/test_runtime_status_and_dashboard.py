"""Tests for runtime status publisher and dashboard overview."""
from __future__ import annotations

import json
from pathlib import Path

from grok_register.core.observer import Metrics
from grok_register import runtime_status
from grok_register import dashboard


class FakeInventory:
    def __init__(self, t=1, q=2):
        self.t_depth = t
        self.q_depth = q


class FakeSem:
    def __init__(self, value):
        self._value = value


def test_metrics_to_dict_and_snapshot_compatible():
    m = Metrics()
    m.t_produced = 3
    m.q_sent = 2
    m.record_success()
    inv = FakeInventory(4, 5)
    sems = {
        "physical": FakeSem(2),
        "t_slot": FakeSem(8),
        "q_slot": FakeSem(7),
        "q_pending": FakeSem(6),
    }
    d = m.to_dict(inv, sems)
    assert d["success_count"] == 1
    assert d["t"]["depth"] == 4
    assert d["q"]["depth"] == 5
    assert d["semaphores"]["physical"] == 2
    line = m.snapshot(inv, sems)
    assert "T:4" in line and "Q:5" in line and "#1" in line


def test_publish_and_read_status(tmp_path, monkeypatch):
    path = tmp_path / "runtime-status.json"
    monkeypatch.setenv("RUNTIME_STATUS_FILE", str(path))
    runtime_status.publish({"service": "register", "running": True, "pid": 1})
    data = runtime_status.read_status()
    assert data["service"] == "register"
    assert data["running"] is True
    assert "updated_at" in data


def test_dashboard_overview_shape(monkeypatch, tmp_path):
    path = tmp_path / "runtime-status.json"
    monkeypatch.setenv("RUNTIME_STATUS_FILE", str(path))
    runtime_status.publish(
        {
            "service": "register",
            "running": True,
            "pid": 999999,
            "metrics": {
                "success_count": 7,
                "registration_starts": 10,
                "rate_per_min": 1.5,
                "t": {"depth": 1, "produced": 9},
                "q": {"depth": 2},
                "pair": {"ok": 3, "fail": 1},
            },
            "email_mode": "tempmail",
            "turnstile_solver": "d3vin",
        }
    )
    # pid won't be alive
    monkeypatch.setattr(dashboard, "process_alive", lambda pid=None: False)
    overview = dashboard.build_overview()
    assert overview["ok"] is True
    assert overview["summary"]["success"] == 7
    assert overview["accounts"]["count"] >= 0
    assert "config" in overview


def test_start_sh_dashboard_dispatch(tmp_path):
    # covered lightly via shell entrypoints style
    from tests.test_shell_entrypoints import _entrypoint_workspace, _run_entry

    workspace = _entrypoint_workspace(tmp_path)
    (workspace / ".env").write_text("EMAIL_MODE=tempmail\n", encoding="utf-8")
    assert _run_entry(workspace, "start.sh", "--dashboard", "--port", "8799") == [
        "-m",
        "grok_register.dashboard",
        "--port",
        "8799",
    ]


def test_probe_xai_access_records_results(monkeypatch):
    class FakeResp:
        status_code = 200
        url = "https://accounts.x.ai/sign-up"

    def fake_get(*args, **kwargs):
        return FakeResp()

    monkeypatch.setattr(
        "requests.get",
        fake_get,
    )
    out = dashboard.probe_xai_access(timeout=3, via_proxy=False)
    assert out["ok"] is True
    assert out["direct_ok"] is True
    assert out["results"]
    assert all(r.get("via") == "direct" for r in out["results"])
