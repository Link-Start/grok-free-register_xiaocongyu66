"""Batch proxy x.ai reachability helpers."""
from __future__ import annotations

from pathlib import Path

from grok_register import proxy_batch_test as pbt


def test_collect_candidates_sources(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PROXY_POOL_FILE", str(tmp_path / "代理.txt"))
    monkeypatch.setenv("PROXY_SCRAPER_OUT", str(tmp_path / "logs" / "public.txt"))

    # patch PROJECT_ROOT used by module
    monkeypatch.setattr(pbt, "PROJECT_ROOT", tmp_path)

    (tmp_path / "代理.txt").write_text("http://1.1.1.1:8080\nsocks5://2.2.2.2:1080\n", encoding="utf-8")
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "proxy-auto-active.txt").write_text("http://3.3.3.3:8080\n", encoding="utf-8")
    (logs / "public.txt").write_text("http://9.9.9.9:8080\nhttp://8.8.8.8:8080\n", encoding="utf-8")

    without = pbt.collect_proxy_candidates(use_public=False, max_candidates=50)
    assert without["counts"]["total"] == 3
    assert "http://9.9.9.9:8080" not in without["candidates"]

    with_pub = pbt.collect_proxy_candidates(use_public=True, max_candidates=50)
    assert with_pub["counts"]["total"] == 5
    assert "http://9.9.9.9:8080" in with_pub["candidates"]
    assert with_pub["sources"]["http://9.9.9.9:8080"] == "public"


def test_simple_normalize():
    assert pbt._simple_normalize("http://1.2.3.4:8080") == "http://1.2.3.4:8080"
    assert pbt._simple_normalize("1.2.3.4:8080") == "http://1.2.3.4:8080"
    assert pbt._simple_normalize("") is None


def test_custom_proxies_and_urls(tmp_path, monkeypatch):
    monkeypatch.setattr(pbt, "PROJECT_ROOT", tmp_path)
    monkeypatch.setenv("PROXY_POOL_FILE", str(tmp_path / "empty.txt"))
    (tmp_path / "empty.txt").write_text("", encoding="utf-8")

    collected = pbt.collect_proxy_candidates(
        use_manual=False,
        use_active=False,
        use_public=False,
        custom_proxies=["http://5.5.5.5:1", "socks5://6.6.6.6:2"],
        max_candidates=10,
    )
    assert collected["counts"]["custom"] == 2
    assert collected["counts"]["total"] == 2

    urls = pbt._normalize_test_urls("https://a.example/\nhttps://b.example/")
    assert urls == ("https://a.example/", "https://b.example/")
    assert pbt._normalize_test_urls(None)[0].startswith("https://accounts.x.ai")
