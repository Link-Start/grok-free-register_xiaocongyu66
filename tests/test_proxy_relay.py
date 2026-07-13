import base64
import json

from grok_register.proxy_relay import share_link_to_sing_box_outbound


def test_vless_share_link_converts_to_sing_box_outbound():
    outbound = share_link_to_sing_box_outbound(
        "vless://00000000-0000-0000-0000-000000000000@example.test:443"
        "?encryption=none&security=tls&type=ws&host=cdn.example.test&path=%2Fws&sni=sni.example.test"
        "#node"
    )

    assert outbound["type"] == "vless"
    assert outbound["server"] == "example.test"
    assert outbound["server_port"] == 443
    assert outbound["uuid"] == "00000000-0000-0000-0000-000000000000"
    assert outbound["tls"]["server_name"] == "sni.example.test"
    assert outbound["transport"] == {
        "type": "ws",
        "path": "/ws",
        "headers": {"Host": "cdn.example.test"},
    }


def test_vmess_share_link_converts_to_sing_box_outbound():
    payload = {
        "v": "2",
        "ps": "node",
        "add": "vmess.example.test",
        "port": "443",
        "id": "00000000-0000-0000-0000-000000000000",
        "aid": "0",
        "scy": "auto",
        "net": "ws",
        "type": "none",
        "host": "cdn.example.test",
        "path": "/socket",
        "tls": "tls",
        "sni": "sni.example.test",
    }
    link = "vmess://" + base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")

    outbound = share_link_to_sing_box_outbound(link)

    assert outbound["type"] == "vmess"
    assert outbound["server"] == "vmess.example.test"
    assert outbound["server_port"] == 443
    assert outbound["uuid"] == "00000000-0000-0000-0000-000000000000"
    assert outbound["tls"]["server_name"] == "sni.example.test"
    assert outbound["transport"]["type"] == "ws"


def test_hysteria2_share_link_converts_obfs_and_tls():
    outbound = share_link_to_sing_box_outbound(
        "hysteria2://secret@example.test:8443?insecure=1&sni=hy.example.test"
        "&obfs=salamander&obfs-password=pepper#node"
    )

    assert outbound["type"] == "hysteria2"
    assert outbound["password"] == "secret"
    assert outbound["tls"]["server_name"] == "hy.example.test"
    assert outbound["tls"]["insecure"] is True
    assert outbound["obfs"] == {"type": "salamander", "password": "pepper"}


def test_hysteria2_without_sni_still_enables_tls():
    outbound = share_link_to_sing_box_outbound("hysteria2://secret@example.test:8443#node")

    assert outbound["tls"] == {"enabled": True}


def test_vless_udp443_flow_is_normalized_for_sing_box():
    outbound = share_link_to_sing_box_outbound(
        "vless://00000000-0000-0000-0000-000000000000@example.test:443"
        "?security=tls&flow=xtls-rprx-vision-udp443-udp443#node"
    )

    assert outbound["flow"] == "xtls-rprx-vision"


def test_shadowsocks_sip002_link_converts_to_outbound():
    credentials = base64.urlsafe_b64encode(b"2022-blake3-aes-128-gcm:secret").decode().rstrip("=")

    outbound = share_link_to_sing_box_outbound(f"ss://{credentials}@ss.example.test:8388#node")

    assert outbound == {
        "type": "shadowsocks",
        "server": "ss.example.test",
        "server_port": 8388,
        "method": "2022-blake3-aes-128-gcm",
        "password": "secret",
    }
