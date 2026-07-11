# Async Local Authentication Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace per-account SSH polling and browser restarts with a bounded, event-driven authentication pipeline that drains historical and incremental accounts while respecting a single global authorization rate-limit gate.

**Architecture:** A persistent SSH snapshot/follow producer feeds a bounded source queue. One-flow-ahead preparation, a serialized browser authorization worker, and a bounded token/sink completion worker overlap safe stages around one persistent Chromium process. Ledger fingerprints provide restart idempotency; an in-memory state map prevents live duplicates.

**Tech Stack:** Python 3.14, asyncio subprocesses/queues/conditions, Playwright, httpx, SQLite, SSH, newline-delimited JSON.

---

### Task 1: Lossless remote snapshot/follow exporter

**Files:**
- Modify: `scripts/export_registered_sessions.py`

- [ ] Refactor exact-session parsing to consume only newline-terminated binary records and retain an incomplete trailing record.
- [ ] Preserve the current full-snapshot merge of exact sessions and legacy `accounts.txt` records without emitting passwords.
- [ ] Add `--follow`: emit the full snapshot, keep the same session-file descriptor/offset, then emit completed appended records.
- [ ] Exit cleanly on inode replacement/truncation so the local source reconnects and obtains a fresh snapshot.
- [ ] Run a one-off bounded diagnostic that appends one complete and one split JSONL record at the snapshot/follow boundary; expect every complete record exactly once and no partial parse.

### Task 2: Persistent SSH source and ledger bulk state

**Files:**
- Create: `xai_enroller/remote_stream.py`
- Modify: `xai_enroller/ledger.py`
- Modify: `xai_enroller/models.py`

- [ ] Implement one SSH child with batch mode, connect timeout, keepalives, stdout JSONL parsing, bounded stderr classification, cancellation cleanup, and reconnect backoff.
- [ ] Represent snapshot/follow records as existing redacted `SourceRecord` values; never place identifiers or credentials in events/exceptions.
- [ ] Add ledger operations for keyed fingerprint lookup, imported-fingerprint bulk load, next attempt number, and aggregate counts without returning raw source values.
- [ ] Add redacted prepared/completion job data objects with monotonic device-flow creation time.
- [ ] Run a one-off diagnostic that reconnects a stream and confirms queued/active/retry/imported fingerprints suppress duplicate admission.

### Task 3: Bounded asynchronous authentication pipeline

**Files:**
- Create: `xai_enroller/auth_pipeline.py`
- Modify: `xai_enroller/executors.py`
- Modify: `xai_enroller/sinks.py`

- [ ] Implement source queue capacity 64, prepared queue capacity 1, and completion queue capacity 2.
- [ ] Implement the live state map (`queued`, `prepared`, `active`, `retry_waiting`, `imported`) and a condition-driven delayed retry heap.
- [ ] Keep one `PlaywrightExecutor` browser alive for the pipeline lifetime; keep per-account contexts isolated and continue rejecting optional privacy Cookies.
- [ ] Prefetch exactly one device flow and discard/recreate it when less than 60 seconds of issuer lifetime remains.
- [ ] Implement one authorization worker and one completion worker so token polling/local atomic storage overlap preparation/authorization safely.
- [ ] Move synchronous file serialization/fsync into `asyncio.to_thread` while preserving atomic replace and `0600` mode.
- [ ] Implement retry classification and attempt numbering from the design; settle every cancellation/exceptional path in the ledger.

### Task 4: Global rate-limit state machine and event metrics

**Files:**
- Modify: `xai_enroller/auth_pipeline.py`
- Modify: `xai_enroller/service.py`

- [ ] Open the gate only on authorization-stage `rate_limited`; wait 60 seconds; admit one probe; clear only on authorization-stage `AUTHORIZED`.
- [ ] Re-arm an inconclusive/rate-limited/cancelled probe without allowing another concurrent probe; leave token/sink outcomes outside gate semantics.
- [ ] Implement pause/resume/cancel/quit across pipeline tasks without stopping the SSH producer on pause.
- [ ] Replace fixed cycle sleep with queue/event waits; retain issuer-required token polling and `slow_down` handling.
- [ ] Emit event-driven completion output with total imports, five-minute rate, attempt success, and eventual unique-account success.
- [ ] Make `s` report queue depths, active stage, cooldown/probe state, aggregate counts, and wall-clock rates without identifiers.
- [ ] Run a bounded one-off state transition diagnostic for rate-limit, probe, pause, cancel, resume, and shutdown.

### Task 5: Runtime composition and live migration

**Files:**
- Modify: `xai_enroller/service.py`
- Modify: `auth-service.sh` only if environment/lifecycle wiring requires it
- Deploy: `scripts/export_registered_sessions.py` to the existing registration host

- [ ] Compose the persistent stream, pipeline, terminal, protocol, executor, sink, and ledger in `main_async` with one shutdown owner.
- [ ] Syntax-check all touched Python files and run `git diff --check`; confirm `git diff -- tests` remains empty.
- [ ] Pause/quit the old local auth process cleanly, deploy the follow-capable exporter, and start the new interactive service with the existing local destination.
- [ ] Confirm exactly one local SSH child and one Chromium process while processing multiple accounts.
- [ ] Confirm historical stock continues draining, a new server registration arrives without restart, and privacy rejection still imports successfully.
- [ ] Trigger or observe a real rate-limit event and verify a 60-second no-confirmation interval followed by exactly one probe.
- [ ] Validate aggregate ledger statistics and every generated auth file's JSON shape/mode without printing names or credential values.

### Task 6: Focused commit and deployment handoff

**Files:**
- Stage only files named in this plan plus already-owned authentication/runtime changes.

- [ ] Inspect the staged diff for identifiers, credentials, unrelated files, and accidental test additions.
- [ ] Run final syntax, format, process-count, live-rate, and privacy-preserving output checks.
- [ ] Commit the implementation with a focused message and push the current branch only after live verification succeeds.
