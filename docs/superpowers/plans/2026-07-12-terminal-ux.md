# Terminal User Experience Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make registration and local authentication simple by default in a normal terminal, while preserving complete diagnostics behind a consistent Debug mode and documenting the complete workflow.

**Architecture:** Keep business pipelines emitting structured events. Add mode-aware terminal renderers, expose `--debug` consistently, and derive progress from durable success records plus the current source snapshot instead of internal queue capacity. Registration, authentication, and documentation are independent work packages and can run in parallel.

**Tech Stack:** Bash, Python 3.11+, asyncio, SQLite ledger, pytest/unittest, Markdown.

---

## File ownership

- `xai_enroller/service.py`: authentication mode parsing, event rendering and command help.
- `xai_enroller/remote_stream.py`: aggregate keyed set for each valid atomic snapshot and source-change events.
- `xai_enroller/auth_pipeline.py`: exact snapshot/ledger pending metrics and task-number propagation.
- `xai_enroller/models.py`: carry the process-local task number through asynchronous completion.
- `auth-service.sh`: authentication shell entry point.
- `register.py`: registration mode parsing and user events.
- `core/observer.py`: registration five-minute completion rate.
- `start.sh`, `setup.sh`: startup stages and Debug flag.
- Existing `tests/test_xai_*.py` and `tests/test_register_runtime_unittest.py`: focused output contracts.
- `README.md` and `docs/guides/*.md`: short quick start plus detailed tutorials.

Use a clean feature worktree. Do not touch or stage unrelated dirty files in the main checkout.

### Task 1: Authentication progress data

**Files:**
- Modify: `xai_enroller/remote_stream.py:18-220`
- Modify: `xai_enroller/auth_pipeline.py:150-220, 380-910`
- Modify: `xai_enroller/models.py:65-90`
- Modify: `xai_enroller/service.py:530-570`
- Test: `tests/test_xai_remote_snapshot.py`
- Test: `tests/test_xai_auth_pipeline.py`

- [ ] **Step 1: Write failing snapshot-count tests**

Extend the successful synchronizer case with two distinct valid records and an injected `fingerprint` callback:

```python
assert await synchronizer.sync_once() is True
assert len(synchronizer.snapshot_fingerprints) == 2
```

Then make a refresh fail and assert it preserves the preceding valid keyed set. Tests use deterministic fake fingerprints and never assert raw identifiers on terminal output.

- [ ] **Step 2: Verify RED**

Run `.venv/bin/python -m pytest -q tests/test_xai_remote_snapshot.py`.
Expected: failure because `snapshot_fingerprints` does not exist.

- [ ] **Step 3: Implement unique snapshot totals**

Initialize `SSHSnapshotSynchronizer.snapshot_fingerprints = None` and require a one-way `fingerprint(source_id)` callback. While validating a refresh, retain only keyed fingerprints returned by that callback. Assign the frozen set only after the temporary file is flushed, atomically replaced, and the directory is synced. A failed refresh leaves the previous set unchanged. Never emit or persist identifiers.

Expose it from `DiskSnapshotSource` through a read-only `snapshot_fingerprints` property. Construct the ledger before the synchronizer in `main_async()` and inject `ledger.fingerprint`. For a local-only source, use the same callback and replace the last valid frozen set only after a full generation reaches EOF; an invalid generation does not replace it.

In the synchronization loop, compare the preceding and new frozen sets. Continue emitting `source_connected` only on connection-state transition, and emit `source_updated` when a later successful snapshot adds keyed accounts:

```python
{"new": len(current - previous), "total": len(current)}
```

No event contains a fingerprint or identifier. A removal-only refresh updates the authoritative set without claiming that new accounts were found.

- [ ] **Step 4: Write failing pending-total tests**

Use a source stub with `snapshot_fingerprints = {"a", "b", "c"}` and a ledger whose `imported_fingerprints()` returns `{"b", "outside"}`:

```python
assert pipeline.status()["pending_total"] == 2
```

This proves historical imports outside the current snapshot do not reduce pending work. Also assert `None` remains unknown and an empty current snapshot produces zero.

- [ ] **Step 5: Verify RED and implement**

In the common pipeline metrics result compute:

```python
snapshot = getattr(self.source, "snapshot_fingerprints", None)
pending_total = None if snapshot is None else len(
    snapshot - self.ledger.imported_fingerprints()
)
```

Keep queue sizes as Debug diagnostics. Compute the keyed-set difference for explicit status/result events only, not on a timer. Do not alter queue capacity, retries, rate limiting or ledger transitions.

Add a `task_number: int | None = None` field to `PreparedJob`. Allocate it exactly when authorization starts, replace the immutable prepared job with that value, and carry it through `CompletionJob`, retry/failure paths and `_emit_result()`. Every `result` event contains the matching task number; the renderer must never infer it from the most recently started task.

Keep the authentication five-minute metric as a Debug diagnostic. Normal output uses the process-lifetime average and returns `None` until the first process success; all cooldown time remains in its denominator.

- [ ] **Step 6: Verify and commit**

Run `.venv/bin/python -m pytest -q tests/test_xai_remote_snapshot.py tests/test_xai_auth_pipeline.py`.
Expected: pass.

Commit only the six task files as `feat: expose authentication progress totals`.

### Task 2: Authentication normal and Debug modes

**Files:**
- Modify: `xai_enroller/service.py:50-105, 225-322, 470-610`
- Modify: `auth-service.sh`
- Test: `tests/test_xai_auth_service.py`

- [ ] **Step 1: Write failing mode-resolution tests**

Add a pure resolver contract:

```python
assert resolve_auth_log_mode([], {}) == "user"
assert resolve_auth_log_mode([], {"XAI_AUTH_SERVICE_LOG_MODE": "debug"}) == "debug"
assert resolve_auth_log_mode(["--debug"], {"XAI_AUTH_SERVICE_LOG_MODE": "user"}) == "debug"
with pytest.raises(ValueError, match="XAI_AUTH_SERVICE_LOG_MODE"):
    resolve_auth_log_mode([], {"XAI_AUTH_SERVICE_LOG_MODE": "verbose"})
```

Unknown arguments are startup errors.

- [ ] **Step 2: Write failing renderer tests**

Inject `messages.append` into `EventTerminal` and assert user mode renders:

```python
terminal = EventTerminal(mode="user", output=messages.append)
terminal.emit(("authorization_started", {
    "task_number": 3, "attempt_number": 1,
    "pending_total": 41, "source_queue": 64,
}))
assert messages == ["[→] 开始认证 #3 | 待处理 41"]
```

Cover imported result with its matching `task_number`, classified failure, limit open/clear, pause/resume, inventory take, first source connection, `source_updated` and `status`. Normal status must contain state, pending, current phase, five-minute rate, cumulative imports, available/claimed and cooldown, but not queue/probe internals or any identifier/credential sentinel. Assert a `None` rate renders `—` and a later empty window renders `0.0/分`. Add one Debug assertion that existing internals remain visible.

Inject an output sink that raises `OSError` and assert `EventTerminal.emit()` returns normally for lifecycle, result and command-response events. Renderer failure is display-only and must never stop the interactive runner or authentication pipeline.

- [ ] **Step 3: Verify RED**

Run `.venv/bin/python -m pytest -q tests/test_xai_auth_service.py`.
Expected: failures for the missing resolver, injectable output and mode formats.

- [ ] **Step 4: Implement the display boundary**

Add `resolve_auth_log_mode(argv, env)`. Make `EventTerminal(mode="user", output=print)` delegate to `_format_user()` or `_format_debug()`. User mode suppresses no-change/internal events, maps known reasons to fixed sanitized Chinese categories, renders unknown pending/rate as `—`, uses the five-minute imported rate, and never reads identifier or credential fields. Debug preserves queue, stage, retry, cooldown, probe, pace, attempt-success and eventual-success fields.

Unknown events return `None` in user mode. Debug renders only a sanitized event-kind token plus an allowlisted reason code or exception class; it must not serialize the event payload or call `str()` on arbitrary errors. `emit()` catches output-sink exceptions after formatting so a broken terminal cannot unwind command handling or pipeline callbacks.

- [ ] **Step 5: Wire startup and help**

Resolve mode before constructing the service and pass it to `EventTerminal`. Keep `auth-service.sh` as transparent `exec ... "$@"`. Use this command banner:

```text
命令：s 状态 | take N 取用凭据 | p 暂停 | r 恢复 | c 取消当前任务 | q 退出
```

Before the pipeline begins, emit a `startup` event containing only inventory counts and the logical destination name `authenticated/`; source state and pending count are initially `—`. The subsequent `source_connected` event reports that the local snapshot is ready, and `source_updated` reports aggregate new/total counts. Tests assert the startup summary includes service purpose, source state, logical output name, pending state and available inventory. It must not print the absolute output path, SSH host, identity path or account identifiers.

Wrap mode/settings validation in a top-level CLI boundary. Missing `XAI_AUTH_SERVICE_SSH_HOST`, an invalid mode, or another known configuration error exits nonzero with one sanitized line naming the configuration key and linking to `docs/guides/auth-service.md#配置远端同步`; no traceback is printed. Add subprocess tests for missing host and invalid mode. Unexpected implementation errors are not mislabeled as configuration errors.

- [ ] **Step 6: Verify and commit**

Run `.venv/bin/python -m pytest -q tests/test_xai_auth_service.py tests/test_xai_auth_pipeline.py`.
Expected: pass.

Commit only the three task files as `feat: simplify authentication terminal output`.

### Task 3: Registration normal and Debug modes

**Files:**
- Modify: `register.py:110-2060` (complete terminal-output audit; no business-flow rewrite)
- Modify: `core/observer.py:1-110`
- Modify: `start.sh`
- Modify: `setup.sh`
- Test: `tests/test_register_runtime_unittest.py`

- [ ] **Step 1: Write failing rate tests**

Exercise public `Metrics` methods with an injected monotonic clock:

```python
metrics = Metrics(clock=lambda: now[0])
assert metrics.five_minute_success_rate() is None
metrics.record_success()
now[0] = 30
assert metrics.five_minute_success_rate() == 2.0
```

Advance past 300 seconds and assert old completions are pruned.

- [ ] **Step 2: Write failing event and mode tests**

Update the existing event contract to:

```text
[→] 开始注册 #7 | 剩余 95
[✓] 注册成功 #7 | 运行平均 12.3/分 | 累计 5
[✗] 注册失败 #7 | 将继续下一任务
[⏸] 触发限流 | 60秒后恢复探测
[▶] 限流解除 | 实际等待 61秒
```

When `TARGET == 0`, omit remaining count and show “持续运行” in startup. Test `resolve_register_log_mode(argv, env)` for default user, compatible `REGISTER_LOG_MODE=debug`, `--debug` precedence and invalid-value failure. Preserve the existing Debug snapshot assertion.

Add a dedicated process-local `registration_starts` counter. Increment it immediately before emitting `started`, after cooldown permission and expiry checks have completed, and pass the resulting number unchanged through success/failure output. Do not reuse `pair_claimed`. Add a regression test where one claimed pair expires while waiting and the next real start is still `#1`.

Use this fixed user classification contract:

- rate limit: automatic cooldown and recovery probe;
- task timeout, rejected registration or missing session: current task is skipped and the next task continues;
- sanitized worker-internal exception: worker continues, with details available only in Debug;
- invalid configuration or browser startup failure: service did not start and the line points to `bash start.sh --debug`;
- operator shutdown: service stopped, with cumulative successes.

Tests cover one event from each category. Do not claim an account-local retry where the pipeline actually skips to a new pair.

- [ ] **Step 3: Verify RED**

Run `.venv/bin/python -m pytest -q tests/test_register_runtime_unittest.py`.
Expected: failures for missing sliding-rate methods, old event text and mode resolver.

- [ ] **Step 4: Implement sliding completion rate**

Add `_clock`, `started_monotonic` and `recent_success_times` to `Metrics`. Implement `record_success()` and `five_minute_success_rate()` with a 300-second deque and an actual elapsed denominator while the process is younger than five minutes. Return `None` before the first success. Replace direct success increments only after all account/session files have been written.

- [ ] **Step 5: Implement terminal filtering**

Resolve mode once. Keep `metrics.snapshot()` on the existing interval only in Debug mode. Normal startup shows mailbox mode, target or “持续运行”, and derived browser concurrency without printing a custom domain. Normal result events use the five-minute rate.

Add `sanitize_terminal_error(error)`, which returns an exception class and only allowlisted stable reason codes; it never returns arbitrary `str(error)`, URLs, response bodies or attributes. Route low-level worker exceptions through this sanitizer before `debug_log`; user mode receives only classified task results. Add a Debug-path sentinel test proving an email/token-like exception string is absent.

Audit every direct `log()` call in `register.py`, not only C-worker results. In particular, remove email and verification-code output from Q admission, sanitize background-settlement, email-creation, P-worker and C-worker exceptions, and ensure diagnostic response/solver messages contain only aggregate allowlisted fields. Add representative tests that execute Q admission and P/C exception paths with identifier-, code- and token-like sentinels, then assert neither user nor Debug output contains them.

Make `log()` accept an injectable output sink and catch sink exceptions. Tests use a sink that raises `OSError` while P/C workers process events and assert worker control flow continues. Rendering failure must not terminate a worker or set the global stop event. Do not change registration concurrency or circuit behavior.

Introduce a top-level CLI boundary for startup validation. Invalid `REGISTER_LOG_MODE` or mailbox configuration exits nonzero with one sanitized line naming the configuration key and linking to the relevant heading in `docs/guides/registration.md`; browser launch failure reports the safe category and `bash start.sh --debug`. Add subprocess coverage proving invalid mode/configuration output has no traceback. Unexpected runtime errors inside an established service are still Debug-diagnosable through sanitized exception classes.

- [ ] **Step 6: Normalize shell stages**

Make `start.sh` consume `--debug`, export `REGISTER_LOG_MODE=debug`, and preserve `--reconfig`, `--target` and `--max-mem`. Render three real stages: environment, configuration, service start. Fix `setup.sh` to one consistent four-stage sequence without changing dependency installation.

- [ ] **Step 7: Verify and commit**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_register_runtime_unittest.py \
  tests/test_runtime_log_analyzer.py tests/test_inventory_unittest.py
bash -n start.sh setup.sh
```

Expected: pass.

Commit only the five task files as `feat: simplify registration terminal output`.

### Task 4: User tutorials

**Files:**
- Modify: `README.md`
- Create: `docs/guides/registration.md`
- Create: `docs/guides/auth-service.md`
- Create: `docs/guides/credential-inventory.md`
- Create: `docs/guides/runtime-troubleshooting.md`

- [ ] **Step 1: Shorten README**

Keep the project purpose, clone command, `bash start.sh`, both entry points, output locations, development tests and architecture links. Replace detailed operational sections with a compact tutorial index.

- [ ] **Step 2: Write registration and authentication guides**

Registration guide: supported Linux setup, first-run prompts, mailbox modes, `--target`, `--max-mem`, `--debug`, stopping and outputs. Explain automatic capacity before tuning. Include a stable `配置邮箱` heading used by sanitized startup-error links.

Authentication guide: separation from registration, one-time exporter placement, SSH-agent or identity-file configuration, `bash auth-service.sh`, controls and safe shutdown. Explain silent unchanged snapshots and local browser execution. Include a stable `配置远端同步` heading used by sanitized startup-error links.

- [ ] **Step 3: Write inventory and troubleshooting guides**

Inventory guide: `available`, `claiming`, `claimed`, `take N`, batch directories and optional note.

Troubleshooting guide: normal events, Debug fields, 60-second single-probe cooldown, source disconnect fallback, configuration errors and exact Debug commands.

- [ ] **Step 4: Check links and privacy**

Run:

```bash
rg -n 'docs/guides/(registration|auth-service|credential-inventory|runtime-troubleshooting)\.md' README.md
rg -n 'bash (start|auth-service)\.sh|--debug|take N' README.md docs/guides
for file in registration auth-service credential-inventory runtime-troubleshooting; do test -f "docs/guides/$file.md"; done
git diff --check -- README.md docs/guides
```

Manually verify all examples use placeholders such as `example.com` and `/path/to/project`. Never include a real host, domain, email, token, key or private local path.

- [ ] **Step 5: Commit**

Commit only README and the four guides as `docs: add terminal workflow guides`.

### Task 5: Integration and live handoff

**Files:** Review all files changed by Tasks 1-4; modify only if verification finds an in-scope defect.

- [ ] **Step 1: Review combined diff**

Confirm no scheduling, rate-limit timing, credential schema, sink behavior, SSH command or inventory transition changed. Confirm unrelated main-checkout files are absent from every commit.

- [ ] **Step 2: Run focused suites**

```bash
.venv/bin/python -m pytest -q tests/test_xai_auth_service.py \
  tests/test_xai_remote_snapshot.py tests/test_xai_auth_pipeline.py \
  tests/test_register_runtime_unittest.py tests/test_runtime_log_analyzer.py
```

Expected: pass.

- [ ] **Step 3: Run complete verification**

```bash
.venv/bin/python -m pytest -q tests
.venv/bin/python -m py_compile register.py core/observer.py \
  xai_enroller/service.py xai_enroller/remote_stream.py xai_enroller/auth_pipeline.py
bash -n start.sh setup.sh auth-service.sh
git diff --check
```

Expected: all tests pass and all syntax/diff checks exit zero.

- [ ] **Step 4: Exercise both modes without real credentials**

Pass synthetic aggregate events with credential-like sentinel values in unused fields through both renderers. Confirm user output is concise and never prints sentinels; Debug retains aggregate internals without secrets. Separately drive the registration worker-exception sanitizer with the same sentinels so direct Debug logging is covered.

- [ ] **Step 5: Preserve and hand off the live process safely**

Before restart, send `s` to the existing process and record aggregate status only. Do not interrupt a task or cooldown. The old process continues using already-loaded code.

After all feature-worktree commits pass review, fast-forward the main checkout to the feature branch while preserving unrelated dirty files, then rerun focused syntax/tests from the main checkout. Confirm `git rev-parse HEAD` equals the reviewed feature commit. Do not launch from the feature worktree, which intentionally has no private runtime configuration.

Apply the new main-checkout entry point only at an idle boundary outside cooldown and only when the existing environment can be preserved. Otherwise leave the verified old process running and report deployment pending rather than forcing a restart or reconstructing secrets from process output.

- [ ] **Step 6: Commit only a real integration fix**

If integration reveals an in-scope defect, add the smallest focused regression assertion and commit its fix. Do not create an empty integration commit.
