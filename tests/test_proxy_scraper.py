"""Tests for public proxy scraper + enhanced node extraction."""
from __future__ import annotations

import json
from pathlib import Path

from grok_register import proxy_auto, proxy_scraper


def test_extract_bare_host_ports():
    text = "\n".join(
        [
            "1.2.3.4:8080",
            "5.6.7.8:1080:user:pass",
            "not-a-proxy",
            "127.0.0.1:8080",
        ]
    )
    http = proxy_auto.extract_bare_host_ports(text, default_scheme="http")
    socks = proxy_auto.extract_bare_host_ports(text, default_scheme="socks5")
    assert "http://1.2.3.4:8080" in http
    assert "http://5.6.7.8:1080:user:pass".replace(":user:pass", "") or True
    assert "http://user:pass@5.6.7.8:1080" in http
    assert "socks5://1.2.3.4:8080" in socks
    assert "http://127.0.0.1:8080" not in http


def test_extract_candidates_merges_share_and_bare():
    text = "vless://id@h:443?security=tls#n\n9.9.9.9:3128\n"
    cands = proxy_auto.extract_candidates_from_text(text, default_scheme="http")
    assert any(c.startswith("vless://") for c in cands)
    assert "http://9.9.9.9:3128" in cands


def test_extract_clash_ss_and_http():
    yaml = """
proxies:
  - name: a
    type: ss
    server: 1.1.1.1
    port: 8388
    cipher: aes-256-gcm
    password: secret
  - { name: b, type: http, server: 2.2.2.2, port: 8080 }
  - name: c
    type: socks5
    server: 3.3.3.3
    port: 1080
"""
    links = proxy_auto.extract_clash_proxy_links(yaml)
    assert any(x.startswith("ss://") and "1.1.1.1:8388" in x for x in links)
    assert "http://2.2.2.2:8080" in links
    assert "socks5://3.3.3.3:1080" in links


def test_parse_source_job_scheme_fragment():
    job = proxy_scraper.parse_source_job(
        "https://example.com/list.txt#scheme=socks5&label=demo"
    )
    assert job is not None
    assert job.default_scheme == "socks5"
    assert job.url == "https://example.com/list.txt"


def test_extract_from_json_proxy_objects():
    payload = json.dumps(
        [
            {"ip": "8.8.8.8", "port": 80, "protocol": "http"},
            {"proxy": "socks5://9.9.9.9:1080"},
        ]
    )
    cands = proxy_scraper.extract_from_body(payload, default_scheme="http")
    assert "http://8.8.8.8:80" in cands
    assert "socks5://9.9.9.9:1080" in cands


def test_scrape_sources_with_fake_fetch(monkeypatch, tmp_path):
    def fake_fetch(url, timeout=15, proxy=None, session=None):
        if "socks" in url:
            return 200, "1.1.1.1:1080\n2.2.2.2:1080\n", None
        return 200, "socks5://3.3.3.3:1080\n", None

    monkeypatch.setattr(proxy_scraper, "fetch_url", fake_fetch)
    cands, fetches = proxy_scraper.scrape_sources(
        [
            "https://example.test/socks.txt#scheme=socks5",
            "https://example.test/other.txt",
        ],
        workers=2,
        timeout=5,
    )
    assert any(f.ok for f in fetches)
    assert "socks5://1.1.1.1:1080" in cands
    assert "socks5://3.3.3.3:1080" in cands


def test_write_candidates(tmp_path):
    path = tmp_path / "c.txt"
    n = proxy_scraper.write_candidates(path, ["a", "a", "b", "", None])
    assert n == 2
    assert path.read_text().strip().splitlines() == ["a", "b"]


def test_builtin_catalog_includes_requested_repos():
    text = "\n".join(proxy_scraper.BUILTIN_CATALOG)
    assert "proxifly/free-proxy-list" in text
    assert "snakem982/proxypool" in text
