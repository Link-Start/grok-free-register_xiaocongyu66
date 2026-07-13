"""Tests for optional Go proxy-worker integration."""
from __future__ import annotations

import json
import os
from pathlib import Path

from grok_register import proxy_auto


def test_resolve_proxy_worker_bin_finds_native_build():
    root = Path(__file__).resolve().parents[1]
    binary = root / "native" / "proxy-worker" / "proxy-worker"
    if not binary.is_file():
        # not built in this environment
        assert proxy_auto.resolve_proxy_worker_bin() in (None, binary)
        return
    found = proxy_auto.resolve_proxy_worker_bin()
    assert found is not None
    assert found.name == "proxy-worker"


def test_test_candidates_prefers_go_when_engine_auto(monkeypatch):
    calls = {"go": 0, "py": 0}

    def fake_go(config, candidates, normalize_proxy):
        calls["go"] += 1
        return [
            proxy_auto.ProxyTestResult(c, c, True, latency_ms=1, status_code=200)
            for c in candidates
        ]

    def fake_py(config, candidates, normalize_proxy, cleanup_proxy=None, request_get=None):
        calls["py"] += 1
        return []

    monkeypatch.setenv("PROXY_WORKER_ENGINE", "auto")
    monkeypatch.setattr(proxy_auto, "_test_candidates_via_go", fake_go)
    monkeypatch.setattr(proxy_auto, "_test_candidates_python", fake_py)

    cfg = proxy_auto.ProxyAutoConfig(enabled=True, test_workers=2)
    out = proxy_auto.test_candidates(cfg, ["http://1.1.1.1:80"], lambda x: x)
    assert calls["go"] == 1
    assert calls["py"] == 0
    assert out and out[0].ok


def test_test_candidates_falls_back_to_python(monkeypatch):
    calls = {"go": 0, "py": 0}

    def fake_go(config, candidates, normalize_proxy):
        calls["go"] += 1
        return None

    def fake_py(config, candidates, normalize_proxy, cleanup_proxy=None, request_get=None):
        calls["py"] += 1
        return [proxy_auto.ProxyTestResult("x", "x", False, error="py")]

    monkeypatch.setenv("PROXY_WORKER_ENGINE", "auto")
    monkeypatch.setattr(proxy_auto, "_test_candidates_via_go", fake_go)
    monkeypatch.setattr(proxy_auto, "_test_candidates_python", fake_py)

    cfg = proxy_auto.ProxyAutoConfig(enabled=True)
    out = proxy_auto.test_candidates(cfg, ["http://1.1.1.1:80"], lambda x: x)
    assert calls == {"go": 1, "py": 1}
    assert out[0].error == "py"


def test_custom_request_get_skips_go(monkeypatch):
    """Unit tests inject request_get and must stay on Python path."""
    called = {"go": 0}

    def fake_go(*_a, **_k):
        called["go"] += 1
        return []

    monkeypatch.setenv("PROXY_WORKER_ENGINE", "go")
    monkeypatch.setattr(proxy_auto, "_test_candidates_via_go", fake_go)

    class Resp:
        status_code = 200

    cfg = proxy_auto.ProxyAutoConfig(
        enabled=True,
        test_urls=("https://example.test",),
        test_timeout=1,
        test_workers=1,
    )
    out = proxy_auto.test_candidates(
        cfg,
        ["http://proxy.test:1"],
        lambda x: x,
        request_get=lambda *a, **k: Resp(),
    )
    assert called["go"] == 0
    assert out[0].ok is True


def test_go_worker_cli_parse_roundtrip(monkeypatch, tmp_path):
    """If binary exists, run a real CLI roundtrip on a dead proxy."""
    binary = proxy_auto.resolve_proxy_worker_bin()
    if binary is None:
        return
    payload = {
        "candidates": ["http://127.0.0.1:9"],
        "test_urls": ["https://example.com"],
        "timeout_sec": 2,
        "workers": 2,
        "accept_status": [[200, 399]],
    }
    data = proxy_auto._go_worker_cli_test(binary, payload)
    assert data.get("engine") == "go"
    assert isinstance(data.get("results"), list)
    assert data["results"][0]["ok"] is False
