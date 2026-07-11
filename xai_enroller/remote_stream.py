"""Persistent, redacted SSH snapshot/follow source for registered sessions."""

import asyncio
import json
import shlex
from contextlib import suppress

from .models import SourceRecord

MAX_SESSION_RECORD_BYTES = 256 * 1024


class RemoteStreamError(RuntimeError):
    """A classified stream failure whose text never includes remote output."""


def parse_session_document(raw: bytes | str) -> SourceRecord:
    if len(raw) > MAX_SESSION_RECORD_BYTES:
        raise ValueError("invalid remote session record")
    try:
        document = json.loads(raw)
        source_id = document["email"]
        raw_cookies = document["cookies"]
    except (UnicodeDecodeError, TypeError, ValueError, KeyError) as exc:
        raise ValueError("invalid remote session record") from exc
    if not isinstance(source_id, str) or not source_id:
        raise ValueError("invalid remote session record")
    try:
        source_id.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("invalid remote session record") from exc
    if not isinstance(raw_cookies, list) or not raw_cookies:
        raise ValueError("invalid remote session record")

    cookies = []
    sso_token = ""
    fallback_sso_token = ""
    allowed = {
        "name",
        "value",
        "url",
        "domain",
        "path",
        "expires",
        "httpOnly",
        "secure",
        "sameSite",
    }
    for raw_cookie in raw_cookies:
        if not isinstance(raw_cookie, dict):
            raise ValueError("invalid remote session record")
        cookie = {key: raw_cookie[key] for key in allowed if key in raw_cookie}
        name = cookie.get("name")
        value = cookie.get("value")
        if not isinstance(name, str) or not name or not isinstance(value, str) or not value:
            raise ValueError("invalid remote session record")
        scope = cookie.get("url") or cookie.get("domain")
        if not isinstance(scope, str) or not scope:
            raise ValueError("invalid remote session record")
        try:
            name.encode("utf-8")
            value.encode("utf-8")
            scope.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError("invalid remote session record") from exc
        if name == "sso" and not sso_token:
            sso_token = value
        elif name == "sso-rw" and not fallback_sso_token:
            fallback_sso_token = value
        cookies.append(cookie)
    sso_token = sso_token or fallback_sso_token
    if not sso_token:
        raise ValueError("invalid remote session record")
    return SourceRecord(source_id, sso_token, tuple(cookies))


class RemoteSessionStream:
    """Yield full snapshots and appends from one reconnecting SSH child."""

    MAX_STDERR_BYTES = 16 * 1024
    MAX_RECORD_BYTES = MAX_SESSION_RECORD_BYTES
    RECONNECT_DELAYS = (1.0, 2.0, 5.0, 10.0, 30.0)

    def __init__(
        self,
        host: str,
        *,
        remote_root: str = "/opt/grok-free-register",
        identity_file: str | None = None,
        process_factory=asyncio.create_subprocess_exec,
        sleep=asyncio.sleep,
        event_callback=None,
    ):
        self.host = host
        self.remote_root = remote_root
        self.identity_file = identity_file
        self.process_factory = process_factory
        self.sleep = sleep
        self.event_callback = event_callback
        self._process = None
        self._closed = False
        self._last_disconnect_reason = None

    def _emit(self, kind, data):
        if self.event_callback is None:
            return
        try:
            self.event_callback(kind, data)
        except Exception:
            pass

    def _command(self):
        return (
            f"cd {shlex.quote(self.remote_root)} && "
            "python3 -u scripts/export_registered_sessions.py --follow "
            "keys/auth-sessions.jsonl keys/accounts.txt"
        )

    def _args(self):
        args = [
            "ssh",
            "-T",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=15",
            "-o",
            "ServerAliveInterval=15",
            "-o",
            "ServerAliveCountMax=3",
        ]
        if self.identity_file:
            args.extend(["-i", self.identity_file])
        args.extend(["--", self.host, self._command()])
        return args

    async def _read_stderr(self, stream):
        retained = bytearray()
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                break
            remaining = self.MAX_STDERR_BYTES - len(retained)
            if remaining > 0:
                retained.extend(chunk[:remaining])
        return bytes(retained)

    @staticmethod
    def _classify_disconnect(returncode, stderr):
        normalized = stderr.lower()
        if b"permission denied" in normalized or b"host key verification failed" in normalized:
            return "ssh_auth_failed"
        if b"could not resolve" in normalized or b"name or service not known" in normalized:
            return "ssh_resolution_failed"
        if any(
            marker in normalized
            for marker in (b"connection refused", b"connection timed out", b"no route to host")
        ):
            return "ssh_connection_failed"
        if returncode == 3:
            return "remote_snapshot_changed"
        if returncode == 4:
            return "remote_data_invalid"
        return "remote_stream_closed"

    async def _terminate_process(self, process):
        if process is None or process.returncode is not None:
            return
        with suppress(ProcessLookupError):
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=3)
        except TimeoutError:
            with suppress(ProcessLookupError):
                process.kill()
            await process.wait()

    async def close(self):
        self._closed = True
        await self._terminate_process(self._process)

    async def records(self):
        reconnect_index = 0
        while not self._closed:
            process = None
            stderr_task = None
            yielded = False
            reason = "remote_stream_closed"
            try:
                process = await self.process_factory(
                    *self._args(),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    limit=self.MAX_RECORD_BYTES + 1,
                )
                self._process = process
                stderr_task = asyncio.create_task(self._read_stderr(process.stderr))
                self._emit("source_connected", {})
                while not self._closed:
                    try:
                        raw = await process.stdout.readline()
                    except (ValueError, asyncio.LimitOverrunError):
                        reason = "remote_record_too_large"
                        break
                    if not raw:
                        break
                    if (
                        not raw.endswith(b"\n")
                        or len(raw) - 1 > self.MAX_RECORD_BYTES
                    ):
                        reason = "remote_record_too_large"
                        break
                    try:
                        record = parse_session_document(raw[:-1])
                    except ValueError:
                        self._emit("source_record_rejected", {"reason": "invalid_record"})
                        continue
                    if not yielded:
                        self._last_disconnect_reason = None
                    yielded = True
                    reconnect_index = 0
                    yield record

                # A follow exporter is intentionally long-lived.  Once framing is
                # invalid, waiting for it to exit on its own would hang this source
                # forever, so terminate it before collecting the exit status.
                if reason != "remote_stream_closed":
                    await self._terminate_process(process)
                if process.returncode is None:
                    await process.wait()
                stderr = await stderr_task
                if reason == "remote_stream_closed":
                    reason = self._classify_disconnect(process.returncode, stderr)
            except asyncio.CancelledError:
                raise
            except (OSError, RuntimeError):
                reason = "ssh_start_failed"
            finally:
                if stderr_task is not None and not stderr_task.done():
                    stderr_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await stderr_task
                await self._terminate_process(process)
                if self._process is process:
                    self._process = None

            if self._closed:
                break
            if reason != self._last_disconnect_reason:
                self._emit("source_disconnected", {"reason": reason})
                self._last_disconnect_reason = reason
            if reason == "remote_snapshot_changed":
                delay = 0.1
            else:
                delay = self.RECONNECT_DELAYS[min(reconnect_index, len(self.RECONNECT_DELAYS) - 1)]
                if not yielded:
                    reconnect_index += 1
            await self.sleep(delay)


PersistentSSHSource = RemoteSessionStream
