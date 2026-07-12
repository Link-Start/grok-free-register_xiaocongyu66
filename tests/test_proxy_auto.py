import base64
import json

from grok_register import proxy_auto


class Response:
    def __init__(self, text="", status_code=200):
        self.text = text
        self.status_code = status_code


def test_extract_nodes_from_base64_subscription():
    payload = "\n".join(
        [
            "vless://id@example.test:443?security=tls#node",
            "trojan://secret@example.test:443#node",
            "https://user:pass@proxy.example.test:8443",
            "https://raw.githubusercontent.com/example/repo/main/sub",
        ]
    )
    encoded = base64.b64encode(payload.encode()).decode()

    nodes = proxy_auto.extract_nodes_from_text(encoded)

    assert "vless://id@example.test:443?security=tls#node" in nodes
    assert "trojan://secret@example.test:443#node" in nodes
    assert "https://user:pass@proxy.example.test:8443" in nodes
    assert "https://raw.githubusercontent.com/example/repo/main/sub" not in nodes


def test_fetch_sources_uses_rotating_bootstrap_proxies(tmp_path):
    calls = []
    config = proxy_auto.ProxyAutoConfig(
        enabled=True,
        sources=("https://source-one.test/sub", "https://source-two.test/sub"),
        source_list_depth=0,
        fetch_workers=2,
        output_dir=str(tmp_path),
    )

    def fake_get(url, proxy, timeout, headers):
        calls.append((url, proxy, timeout, headers))
        return Response("vless://id@example.test:443#node")

    results = proxy_auto.fetch_source_bodies(
        config,
        ["http://proxy-one.test:8080", "http://proxy-two.test:8080"],
        request_get=fake_get,
    )

    assert len(results) == 2
    assert {call[1] for call in calls} == {
        "http://proxy-one.test:8080",
        "http://proxy-two.test:8080",
    }


def test_source_list_expands_one_level_with_rotating_proxies(tmp_path):
    config = proxy_auto.ProxyAutoConfig(
        enabled=True,
        sources=("*https://sources.test/list",),
        source_list_depth=1,
        fetch_workers=2,
        output_dir=str(tmp_path),
    )

    def fake_get(url, proxy, timeout, headers):
        if url == "https://sources.test/list":
            return Response(
                "\n".join(
                    [
                        "https://source-one.test/sub",
                        "https://source-two.test/sub",
                    ]
                )
            )
        return Response("trojan://secret@example.test:443#node")

    results = proxy_auto.fetch_source_bodies(
        config,
        ["http://proxy-one.test:8080", "http://proxy-two.test:8080"],
        request_get=fake_get,
    )

    assert len(results) == 2
    assert all("trojan://" in result.text for result in results)


def test_test_candidates_uses_each_candidate_as_request_proxy(tmp_path):
    config = proxy_auto.ProxyAutoConfig(
        enabled=True,
        test_urls=("https://accounts.x.ai/sign-up",),
        test_workers=2,
        output_dir=str(tmp_path),
    )
    calls = []

    def fake_get(url, proxy, timeout, headers):
        calls.append(proxy)
        return Response(status_code=200 if "good" in proxy else 503)

    results = proxy_auto.test_candidates(
        config,
        ["http://good-proxy.test:8080", "http://bad-proxy.test:8080"],
        lambda line: line,
        request_get=fake_get,
    )

    assert {result.proxy for result in results if result.ok} == {"http://good-proxy.test:8080"}
    assert set(calls) == {"http://good-proxy.test:8080", "http://bad-proxy.test:8080"}


def test_refresh_retests_previous_active_proxies_when_fetch_finds_nothing(monkeypatch, tmp_path):
    config = proxy_auto.ProxyAutoConfig(
        enabled=True,
        sources=(),
        output_dir=str(tmp_path),
        active_file="active.txt",
        export_formats=("raw",),
    )
    config.active_path.write_text("http://previous-good.test:8080\n", encoding="utf-8")
    fetch_bootstrap = []
    tested = []

    def fake_fetch(_config, bootstrap):
        fetch_bootstrap.extend(bootstrap)
        return []

    def fake_test(_config, candidates, normalize_proxy):
        tested.extend(candidates)
        return [
            proxy_auto.ProxyTestResult(
                candidate,
                normalize_proxy(candidate),
                True,
                latency_ms=5,
                status_code=200,
            )
            for candidate in candidates
        ]

    monkeypatch.setattr(proxy_auto, "fetch_source_bodies", fake_fetch)
    monkeypatch.setattr(proxy_auto, "test_candidates", fake_test)

    manager = proxy_auto.ProxyAutoManager(
        config,
        lambda line: line,
        bootstrap_proxies=lambda: ["http://manual.test:8080"],
    )

    assert manager.refresh_once() == ["http://previous-good.test:8080"]
    assert fetch_bootstrap == ["http://manual.test:8080", "http://previous-good.test:8080"]
    assert tested == ["http://previous-good.test:8080"]
    assert config.active_path.read_text(encoding="utf-8").strip() == "http://previous-good.test:8080"


def test_write_outputs_supports_sub2api_and_cpa(tmp_path):
    config = proxy_auto.ProxyAutoConfig(
        enabled=True,
        output_dir=str(tmp_path),
        export_formats=("raw", "base64", "sub2api", "cpa"),
    )
    active = [
        proxy_auto.ProxyTestResult(
            "vless://node",
            "http://user:pass@127.0.0.1:19080",
            True,
            latency_ms=12,
            status_code=200,
        )
    ]

    proxy_auto.write_outputs(config, active)

    assert config.active_path.read_text(encoding="utf-8").strip() == "http://user:pass@127.0.0.1:19080"
    decoded = base64.b64decode(config.base64_path.read_text(encoding="utf-8")).decode()
    assert decoded == "http://user:pass@127.0.0.1:19080"
    sub2api = json.loads(config.sub2api_path.read_text(encoding="utf-8"))
    cpa = json.loads(config.cpa_path.read_text(encoding="utf-8"))
    assert sub2api["type"] == "sub2api-data"
    assert sub2api["accounts"] == []
    assert sub2api["proxies"][0]["proxy_key"] == "http|127.0.0.1|19080|user|pass"
    assert cpa["proxies"][0]["host"] == "127.0.0.1"


def test_sub2api_payload_skips_protocols_sub2api_cannot_import():
    assert proxy_auto.proxy_url_to_sub2api_proxy("socks4://127.0.0.1:1080") is None
