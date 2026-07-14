#!/usr/bin/env python3
"""Tiny local HTTP(S) CONNECT proxy that forwards via authenticated SOCKS5.

Used when sing-box mixed inbound cannot bind under proot/cgroup restrictions.
Playwright/Chromium can use plain local HTTP proxies without SOCKS auth.
"""
from __future__ import annotations

import argparse
import select
import socket
import socketserver
import sys
import threading
from urllib.parse import urlparse

import socks  # PySocks


def parse_socks(url: str):
    u = urlparse(url if "://" in url else f"socks5h://{url}")
    host = u.hostname
    port = u.port or 1080
    user = u.username or None
    pwd = u.password or None
    if not host:
        raise SystemExit(f"bad socks url: {url}")
    return host, int(port), user, pwd


class Handler(socketserver.BaseRequestHandler):
    upstream = ("127.0.0.1", 1080, None, None)  # host, port, user, pwd

    def handle(self):
        self.request.settimeout(30)
        try:
            data = b""
            while b"\r\n\r\n" not in data and len(data) < 65536:
                chunk = self.request.recv(4096)
                if not chunk:
                    return
                data += chunk
        except Exception:
            return
        try:
            head = data.split(b"\r\n\r\n", 1)[0].decode("iso-8859-1", "replace")
            first = head.split("\r\n", 1)[0]
            method, target, _ver = first.split(" ", 2)
        except Exception:
            return

        if method.upper() != "CONNECT":
            # Minimal non-CONNECT rejection (Chromium HTTPS uses CONNECT)
            try:
                self.request.sendall(b"HTTP/1.1 405 Method Not Allowed\r\nConnection: close\r\n\r\n")
            except Exception:
                pass
            return

        try:
            host, port_s = target.rsplit(":", 1)
            port = int(port_s)
            if host.startswith("[") and host.endswith("]"):
                host = host[1:-1]
        except Exception:
            try:
                self.request.sendall(b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n")
            except Exception:
                pass
            return

        uhost, uport, user, pwd = self.upstream
        remote = socks.socksocket()
        remote.set_proxy(socks.SOCKS5, uhost, uport, True, user, pwd)
        remote.settimeout(30)
        try:
            remote.connect((host, port))
        except Exception:
            try:
                self.request.sendall(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
            except Exception:
                pass
            try:
                remote.close()
            except Exception:
                pass
            return

        try:
            self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        except Exception:
            remote.close()
            return

        self._pipe(self.request, remote)

    def _pipe(self, a: socket.socket, b: socket.socket):
        a.settimeout(None)
        b.settimeout(None)
        sockets = [a, b]
        try:
            while True:
                r, _, x = select.select(sockets, [], sockets, 300)
                if x or not r:
                    break
                for s in r:
                    other = b if s is a else a
                    try:
                        data = s.recv(65536)
                    except Exception:
                        return
                    if not data:
                        return
                    try:
                        other.sendall(data)
                    except Exception:
                        return
        finally:
            for s in (a, b):
                try:
                    s.close()
                except Exception:
                    pass


class ThreadingTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--listen", default="127.0.0.1")
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--socks", required=True, help="socks5h://user:pass@host:port")
    args = ap.parse_args()
    upstream = parse_socks(args.socks)

    class H(Handler):
        pass

    H.upstream = upstream
    srv = ThreadingTCPServer((args.listen, args.port), H)
    print(f"relay http://{args.listen}:{args.port} -> socks5://{upstream[0]}:{upstream[1]}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
