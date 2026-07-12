"""Interactive composition for the asynchronous local authentication service."""

import asyncio
import os
import secrets
import shlex
import sys
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from .models import SourceRecord
from .inventory import InventoryError
from .remote_stream import parse_session_document


DEFAULT_LOCAL_AUTH_DIR = Path.home() / "Downloads" / "grok-free-register-auth"
AUTHENTICATED_DIRNAME = "authenticated"
CLAIMED_DIRNAME = "claimed"


def prepare_local_service_environment(env=None):
    """Create private local persistence defaults without storing secrets in the repo."""
    merged = dict(os.environ if env is None else env)
    destination = Path(
        merged.get("XAI_ENROLLER_LOCAL_AUTH_DIR", DEFAULT_LOCAL_AUTH_DIR)
    ).expanduser()
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(destination, 0o700)
    merged["XAI_ENROLLER_SINK"] = "local"
    merged["XAI_ENROLLER_LOCAL_AUTH_DIR"] = str(destination)
    merged.setdefault(
        "XAI_ENROLLER_LEDGER_PATH", str(destination / "enrollment-ledger.db")
    )
    if not merged.get("XAI_ENROLLER_SOURCE_SALT"):
        salt_file = destination / ".ledger-salt"
        try:
            salt = salt_file.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            salt = secrets.token_hex(32)
            fd, temporary_name = tempfile.mkstemp(
                prefix=".ledger-salt.", suffix=".tmp", dir=destination, text=True
            )
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    stream.write(salt + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary_name, salt_file)
            finally:
                if os.path.exists(temporary_name):
                    os.unlink(temporary_name)
        os.chmod(salt_file, 0o600)
        merged["XAI_ENROLLER_SOURCE_SALT"] = salt
    return merged


@dataclass(frozen=True)
class AuthServiceSettings:
    ssh_host: str
    remote_root: str
    identity_file: str | None
    sync_seconds: int = 30
    retry_seconds: int = 60
    min_authorization_interval_seconds: float = 10.0

    @classmethod
    def from_environ(cls, env=None):
        env = dict(os.environ if env is None else env)
        ssh_host = (env.get("XAI_AUTH_SERVICE_SSH_HOST") or "").strip()
        if not ssh_host:
            raise ValueError("XAI_AUTH_SERVICE_SSH_HOST is required")
        remote_root = (
            env.get("XAI_AUTH_SERVICE_REMOTE_ROOT") or "/opt/grok-free-register"
        ).strip()
        identity_file = (env.get("XAI_AUTH_SERVICE_SSH_IDENTITY") or "").strip() or None
        try:
            sync_seconds = int(env.get("XAI_AUTH_SERVICE_SYNC_SEC", "30"))
            retry_seconds = int(env.get("XAI_AUTH_SERVICE_RETRY_SEC", "60"))
            min_authorization_interval_seconds = float(
                env.get("XAI_AUTH_SERVICE_MIN_INTERVAL_SEC", "10")
            )
        except ValueError as exc:
            raise ValueError("auth service intervals must be numeric") from exc
        if not 5 <= sync_seconds <= 3600:
            raise ValueError("XAI_AUTH_SERVICE_SYNC_SEC must be between 5 and 3600")
        if not 30 <= retry_seconds <= 86400:
            raise ValueError("XAI_AUTH_SERVICE_RETRY_SEC must be between 30 and 86400")
        if not 0 <= min_authorization_interval_seconds <= 3600:
            raise ValueError(
                "XAI_AUTH_SERVICE_MIN_INTERVAL_SEC must be between 0 and 3600"
            )
        return cls(
            ssh_host,
            remote_root,
            identity_file,
            sync_seconds,
            retry_seconds,
            min_authorization_interval_seconds,
        )


def parse_registered_accounts(lines):
    """Parse legacy ``source:discarded-password:sso`` records."""
    seen = set()
    for line_number, line in enumerate(lines, 1):
        raw = line.rstrip("\r\n")
        if not raw:
            continue
        try:
            source_id, _discarded_password, sso_token = raw.rsplit(":", 2)
        except ValueError as exc:
            raise ValueError(f"invalid registered account line {line_number}") from exc
        if not source_id or not sso_token or source_id in seen:
            continue
        seen.add(source_id)
        yield SourceRecord(source_id, sso_token)


def parse_exported_records(lines):
    """Parse the historical tab-delimited redacted exporter format."""
    seen = set()
    for line_number, line in enumerate(lines, 1):
        raw = line.rstrip("\r\n")
        if not raw:
            continue
        try:
            source_id, sso_token = raw.split("\t", 1)
        except ValueError as exc:
            raise ValueError(f"invalid remote export line {line_number}") from exc
        if not source_id or not sso_token or source_id in seen:
            continue
        seen.add(source_id)
        yield SourceRecord(source_id, sso_token)


def parse_exported_sessions(lines):
    """Parse exact redacted session documents while preserving Cookie scope."""
    seen = set()
    for line_number, line in enumerate(lines, 1):
        raw = line.rstrip("\r\n")
        if not raw:
            continue
        try:
            record = parse_session_document(raw)
        except ValueError as exc:
            raise ValueError(f"invalid registered session line {line_number}") from exc
        if record.source_id in seen:
            continue
        seen.add(record.source_id)
        yield record


class SSHRegisteredSource:
    """Compatibility facade for the former one-shot account exporter."""

    def __init__(
        self,
        host,
        *,
        remote_root="/opt/grok-free-register",
        identity_file=None,
        process_factory=asyncio.create_subprocess_exec,
    ):
        self.host = host
        self.remote_root = remote_root
        self.identity_file = identity_file
        self.process_factory = process_factory

    async def fetch(self):
        command = (
            f"cd {shlex.quote(self.remote_root)} && "
            "python3 scripts/export_registered_sso.py keys/accounts.txt"
        )
        args = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15"]
        if self.identity_file:
            args.extend(["-i", self.identity_file])
        args.extend([self.host, command])
        process = await self.process_factory(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _stderr = await process.communicate()
        if process.returncode != 0:
            raise RuntimeError("registered account sync failed")
        return list(parse_exported_records(stdout.decode("utf-8").splitlines()))


class AuthService:
    """Compatibility facade for the former polling enrollment service."""

    def __init__(self, source, enroller, emit):
        self.source = source
        self.enroller = enroller
        self.emit = emit

    async def run_cycle(self):
        records = await self.source.fetch()
        fresh = [
            record
            for record in records
            if not self.enroller.ledger.has_imported(record.source_id)
        ]
        if not fresh:
            return []
        self.emit(("sync", {"new": len(fresh)}))
        results = await self.enroller.run_records(fresh)
        for result in results:
            self.emit(
                (
                    "result",
                    {
                        "source_id": result.source_id,
                        "status": result.status.value,
                        "reason": result.reason_code,
                    },
                )
            )
        return results


class EventTerminal:
    """Render only aggregate, classified events; identifiers and credentials stay hidden."""

    @staticmethod
    def _percentage(value):
        return f"{100.0 * float(value):.1f}%"

    def emit(self, event):
        kind, data = event
        message = None
        if kind == "service_started":
            message = (
                "• service: running; "
                f"min_interval={data['min_authorization_interval_seconds']:.1f}s"
            )
        elif kind == "service_stopped":
            message = "• service: stopped"
        elif kind == "source_connected":
            message = "• source: local snapshot updated"
        elif kind == "source_disconnected":
            message = f"⚠ source: {data['reason']}; keeping previous snapshot"
        elif kind == "source_record_rejected":
            message = "⚠ source: invalid record rejected"
        elif kind == "rate_limited":
            message = (
                "⏸ authentication rate limited; "
                f"next single probe in {data['wait_seconds']}s"
            )
        elif kind == "rate_limit_cleared":
            message = (
                "▶ authentication rate limit cleared after "
                f"{data['elapsed_seconds']}s"
            )
        elif kind == "authorization_started":
            message = (
                "→ authentication: next task started; "
                f"task={data['task_number']}; attempt={data['attempt_number']}; "
                f"queued={data['source_queue']}"
            )
        elif kind == "result":
            if data["status"] == "imported":
                message = (
                    "✓ authentication: imported; "
                    f"total={data['imported_unique']}, "
                    f"avg_rate={data['lifetime_imports_per_minute']:.2f}/min, "
                    f"attempt_success={self._percentage(data['attempt_success'])}, "
                    f"eventual_success={self._percentage(data['eventual_success'])}"
                )
            elif data["reason"] == "rate_limited":
                message = None
            else:
                message = (
                    f"⚠ authentication: {data['status']} ({data['reason']}); "
                    f"attempt={data['attempt_number']}; "
                    f"avg_rate={data['lifetime_imports_per_minute']:.2f}/min"
                )
        elif kind == "control":
            message = f"• service: {data['state']}"
        elif kind == "status":
            message = (
                f"• status: {data['state']}; "
                f"queues={data['source_queue']}/{data['prepared_queue']}/"
                f"{data['completion_queue']}; active={data['active_stage']}; "
                f"retry_waiting={data['retry_waiting']}; "
                f"next_retry={data['next_retry_seconds']:.1f}s; "
                f"started={data['authorization_starts']}; "
                f"cooldown={str(data['cooldown']).lower()}; "
                f"cooldown_remaining={data['cooldown_remaining_seconds']:.1f}s; "
                f"probe={str(data['probe_in_flight']).lower()}; "
                f"min_interval={data['min_authorization_interval_seconds']:.1f}s; "
                f"pace_remaining={data['pacing_remaining_seconds']:.1f}s; "
                f"imported={data['imported_unique']}; "
                f"attempted={data['attempted_unique']}; "
                f"rate_limited={data['rate_limited']}; "
                f"5m_rate={data['five_minute_imports_per_minute']:.2f}/min; "
                f"lifetime_rate={data['lifetime_imports_per_minute']:.2f}/min; "
                f"available={data['available']}; "
                f"claiming={data['claiming']}; claimed={data['claimed']}"
            )
        elif kind == "inventory_taken":
            message = (
                f"✓ inventory: claimed={data['moved']}; "
                f"available={data['available']}; batch={data['batch_id']}; "
                f"directory={data['directory']}"
            )
        elif kind == "inventory_error":
            message = (
                f"⚠ inventory: {data['reason']}; "
                f"available={data['available']}; "
                f"claiming={data['claiming']}; claimed={data['claimed']}"
            )
        elif kind == "pipeline_error":
            message = (
                f"⚠ service: {data['stage']} stage stopped ({data['reason']})"
            )
        if message is not None:
            print(message, flush=True)


class AuthPipelineRunner:
    """Map interactive controls onto the persistent pipeline lifecycle."""

    def __init__(self, pipeline, emit, *, interval_seconds=None, inventory=None):
        self.pipeline = pipeline
        self.emit = emit
        self.inventory = inventory
        self.paused = False
        self.current_cycle = None

    async def handle_command(self, command):
        command = command.strip().lower()
        if command == "p":
            self.paused = True
            self.pipeline.pause()
            self.emit(("control", {"state": "paused"}))
        elif command == "r":
            self.paused = False
            self.pipeline.resume()
            self.emit(("control", {"state": "running"}))
        elif command == "s":
            status = self.pipeline.status()
            status.update(self.pipeline.ledger.inventory_counts())
            self.emit(("status", status))
        elif command.startswith("take "):
            parts = command.split()
            if len(parts) != 2 or not parts[1].isdigit() or int(parts[1]) <= 0:
                self.emit(("control", {"state": "usage: take <positive-count>"}))
                return True
            if self.inventory is None:
                self.emit(("control", {"state": "inventory unavailable"}))
                return True
            try:
                batch = await asyncio.to_thread(self.inventory.take, int(parts[1]))
            except InventoryError as exc:
                self.emit(
                    (
                        "inventory_error",
                        {
                            "reason": str(exc),
                            **self.pipeline.ledger.inventory_counts(),
                        },
                    )
                )
                return True
            counts = self.pipeline.ledger.inventory_counts()
            self.emit(
                (
                    "inventory_taken",
                    {
                        "batch_id": batch.batch_id,
                        "directory": str(batch.directory),
                        "moved": batch.moved,
                        **counts,
                    },
                )
            )
        elif command == "c":
            cancelled = await self.pipeline.cancel_active()
            self.emit(
                ("control", {"state": "cancelling" if cancelled else "idle"})
            )
        elif command in {"q", "quit", "exit"}:
            self.pipeline.request_stop()
            self.emit(("control", {"state": "stopping"}))
            return False
        return True

    async def run(self):
        self.current_cycle = asyncio.create_task(self.pipeline.run())
        try:
            await self.current_cycle
        finally:
            self.current_cycle = None


class AuthServiceRunner:
    """Compatibility runner for the former polling service API."""

    def __init__(self, service, emit, *, interval_seconds):
        self.service = service
        self.emit = emit
        self.interval_seconds = interval_seconds
        self.paused = False
        self.current_cycle = None
        self._resume = asyncio.Event()
        self._resume.set()
        self._wake = asyncio.Event()
        self._stopping = False

    async def handle_command(self, command):
        command = command.strip().lower()
        if command == "p":
            self.paused = True
            self._resume.clear()
            self.emit(("control", {"state": "paused"}))
        elif command == "r":
            self.paused = False
            self._resume.set()
            self._wake.set()
            self.emit(("control", {"state": "running"}))
        elif command == "s":
            active = self.current_cycle is not None and not self.current_cycle.done()
            self.emit(
                (
                    "status",
                    {
                        "state": "paused" if self.paused else "running",
                        "active": active,
                    },
                )
            )
        elif command == "c":
            if self.current_cycle is not None and not self.current_cycle.done():
                self.current_cycle.cancel()
                self.emit(("control", {"state": "cancelling"}))
        elif command in {"q", "quit", "exit"}:
            self._stopping = True
            self._resume.set()
            self._wake.set()
            if self.current_cycle is not None and not self.current_cycle.done():
                self.current_cycle.cancel()
            self.emit(("control", {"state": "stopping"}))
            return False
        return True

    async def run(self):
        while not self._stopping:
            await self._resume.wait()
            if self._stopping:
                break
            self.current_cycle = asyncio.create_task(self.service.run_cycle())
            try:
                await self.current_cycle
            except asyncio.CancelledError:
                if self._stopping:
                    break
            except Exception:
                self.emit(("error", {"reason": "sync_failed"}))
            finally:
                self.current_cycle = None
            if self._stopping:
                break
            self._wake.clear()
            try:
                await asyncio.wait_for(
                    self._wake.wait(), timeout=self.interval_seconds
                )
            except TimeoutError:
                pass


async def _run_interactive(runner):
    print(
        "commands: s=status, take N=claim credentials, p=pause, "
        "r=resume, c=cancel active, q=quit",
        flush=True,
    )
    loop = asyncio.get_running_loop()
    commands = asyncio.Queue()
    stdin_fd = sys.stdin.fileno()

    def stdin_ready():
        line = sys.stdin.readline()
        if line == "":
            loop.remove_reader(stdin_fd)
            commands.put_nowait(None)
            return
        commands.put_nowait(line)

    loop.add_reader(stdin_fd, stdin_ready)
    worker = asyncio.create_task(runner.run())
    try:
        while not worker.done():
            command_task = asyncio.create_task(commands.get())
            done, _pending = await asyncio.wait(
                {worker, command_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if worker in done:
                command_task.cancel()
                with suppress(asyncio.CancelledError):
                    await command_task
                await worker
                break
            command = command_task.result()
            if command is None:
                command = "q"
            if not await runner.handle_command(command):
                break
    except asyncio.CancelledError:
        runner.pipeline.request_stop()
        raise
    finally:
        loop.remove_reader(stdin_fd)
        runner.pipeline.request_stop()
        await asyncio.gather(worker, return_exceptions=True)


async def main_async():
    import httpx

    from .auth_pipeline import AuthPipeline
    from .config import Settings
    from .executors import PlaywrightExecutor
    from .inventory import CredentialInventory
    from .ledger import Ledger
    from .protocol import XAIProfile, XAIProtocol
    from .remote_stream import DiskSnapshotSource, SSHSnapshotSynchronizer
    from .sinks import LocalAuthFileSink

    service_settings = AuthServiceSettings.from_environ()
    merged = prepare_local_service_environment()
    merged["XAI_ENROLLER_SOURCE_KIND"] = "remote"
    merged["XAI_ENROLLER_AUTH_EXECUTOR"] = "playwright"
    merged["XAI_ENROLLER_CONCURRENCY"] = "1"
    settings = Settings.from_environ(merged)
    terminal = EventTerminal()
    client = httpx.AsyncClient()
    pipeline = None
    try:
        snapshot_path = Path(settings.local_auth_dir) / "source-snapshot.jsonl"
        synchronizer = SSHSnapshotSynchronizer(
            service_settings.ssh_host,
            snapshot_path,
            remote_root=service_settings.remote_root,
            identity_file=service_settings.identity_file,
        )
        source = DiskSnapshotSource(
            snapshot_path,
            synchronizer=synchronizer,
            sync_seconds=service_settings.sync_seconds,
            event_callback=lambda kind, data: terminal.emit((kind, data)),
        )
        protocol = XAIProtocol(
            client,
            XAIProfile.default(),
            default_poll_interval=settings.poll_interval,
        )
        executor = PlaywrightExecutor(concurrency=1)
        sink = LocalAuthFileSink(
            Path(settings.local_auth_dir) / AUTHENTICATED_DIRNAME,
            name_secret=settings.source_salt,
        )
        ledger = Ledger(settings.ledger_path, settings.source_salt)
        inventory = CredentialInventory(
            ledger,
            Path(settings.local_auth_dir) / AUTHENTICATED_DIRNAME,
            Path(settings.local_auth_dir) / CLAIMED_DIRNAME,
        )
        recovered = await asyncio.to_thread(inventory.recover)
        if recovered:
            terminal.emit(("control", {"state": f"recovered {recovered} claims"}))
        pipeline = AuthPipeline(
            source=source,
            protocol=protocol,
            executor=executor,
            sink=sink,
            ledger=ledger,
            timeout=settings.timeout_sec,
            min_authorization_interval=(
                service_settings.min_authorization_interval_seconds
            ),
            event_callback=lambda kind, data: terminal.emit((kind, data)),
        )
        pipeline.rate_gate.COOLDOWN_SECONDS = float(service_settings.retry_seconds)
        runner = AuthPipelineRunner(pipeline, terminal.emit, inventory=inventory)
        await _run_interactive(runner)
    finally:
        if pipeline is not None:
            pipeline.request_stop()
        await client.aclose()


def main():
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
