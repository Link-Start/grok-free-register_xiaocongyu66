# Authentication Source Modes and Repository Layout Design

## Goal

Make a same-machine registration and authentication deployment the zero-configuration
default without removing the existing SSH-separated deployment. Restructure the tracked
source tree so the repository root contains only project metadata and supported shell
entry points, while preserving user data and operational compatibility.

## Scope

This change covers authentication source selection, the local registered-session input,
tracked source-file placement, supported commands, documentation, and safe handling of
existing untracked workspace artifacts. It does not change registration concurrency,
authentication pacing, cooldown behavior, credential schema, inventory semantics, email
provider defaults, or CPA refresh behavior.

## Source Selection

`XAI_AUTH_SERVICE_SOURCE` accepts `auto`, `local`, or `ssh` and defaults to `auto`.

- `auto`: use `ssh` when `XAI_AUTH_SERVICE_SSH_HOST` is non-empty; otherwise use
  `local`.
- `local`: read registration output below the local project root and ignore any SSH
  settings.
- `ssh`: use the existing atomic SSH snapshot synchronizer and require
  `XAI_AUTH_SERVICE_SSH_HOST`.

The optional `XAI_AUTH_SERVICE_REGISTER_ROOT` selects the registration project root for
local mode. It defaults to the repository root selected by `auth-service.sh`, so the
normal same-machine command remains only:

```bash
bash auth-service.sh
```

The existing SSH variables remain compatible. A user who already exports
`XAI_AUTH_SERVICE_SSH_HOST` continues to use remote mode without adding a new variable.
An explicit source setting always takes precedence over auto-detection.

Invalid source values are configuration errors. Explicit `ssh` without a host is a
configuration error. Local mode with no registration output is not fatal: the service
starts with an empty valid snapshot and waits for registration to produce records.

## Local Snapshot Source

Local mode consumes the same two files that the registration process already maintains:

```text
keys/auth-sessions.jsonl
keys/accounts.txt
```

It must preserve the SSH source's important properties:

1. Complete JSONL session records retain their cookie names and scopes.
2. Historical `accounts.txt` records remain a fallback for accounts without a complete
   session record.
3. An incomplete trailing append is ignored until the next refresh rather than treated
   as a valid record.
4. Duplicate account identifiers are collapsed before entering the pipeline.
5. The pipeline continues to read an atomically replaced, mode `0600` snapshot in the
   authentication state directory; it never consumes a file while registration is
   appending to it.
6. A malformed refresh does not replace the preceding valid snapshot.

For newly registered accounts, registration commits the exact
`auth-sessions.jsonl` record before appending the compatibility
`accounts.txt` record. The exact record is flushed and synced before the fallback becomes
visible. This write order ensures a concurrent local export can see either no new account
or the exact session; it cannot first admit that account through the legacy-cookie
fallback. Historical accounts that genuinely lack an exact session continue to use the
fallback.

Implement a local snapshot synchronizer behind the same refresh contract used by
`DiskSnapshotSource`. It executes the exporter anchored to the authentication checkout
with `sys.executable`; `XAI_AUTH_SERVICE_REGISTER_ROOT` supplies only the two input data
paths. Code is never loaded or executed from the selected registration root. The
synchronizer validates exporter output using the existing snapshot parser and atomically
installs the snapshot. This reuses one normalization path for local and SSH modes instead
of introducing a second account parser.

Before and after each export, the synchronizer records each input's existence, device,
inode, size, and nanosecond modification time. If either generation changes, the candidate
snapshot is rejected and refreshed again; it never replaces the last valid snapshot.
This protects complete-line boundaries and multi-file consistency, while the exact-first
registration write order protects precedence during the stable interval between the two
appends.

Normal terminal output may state `来源 本机` or `来源 远端`, but must not print absolute
paths, SSH hosts, identities, account identifiers, cookies, or tokens. Debug mode may
show the source kind and aggregate refresh state, but retains the same secrecy rules.

## Tracked Repository Layout

The version-controlled root keeps only supported entry points and project metadata:

```text
.env.example
.gitignore
LICENSE
README.md
auth-service.sh
pyproject.toml
requirements.txt
setup.sh
start.sh
```

Python implementation and developer utilities move to explicit namespaces:

```text
grok_register/
  __init__.py
  register.py
  email_server.py
  core/
xai_enroller/
tools/
  __init__.py
  run_tests.py
  runtime_log_analyzer.py
scripts/
deploy/
cloudflare/
docs/
tests/
```

`start.sh` runs `python -m grok_register.register`. Its supported
`--email-service` branch runs only `python -m grok_register.email_server`, so the optional
custom-domain mailbox remains available through `bash start.sh --email-service` without
exposing another user entry convention. Direct Python module commands are developer-only.
Developer commands use `python tools/run_tests.py`. Imports and tests are updated to the
package paths; no root-level compatibility Python shims are retained, because they would
preserve the ambiguous layout being removed.

`--email-service` dispatch occurs after dependency availability is established but before
registration locking, `.env` mailbox selection, or registration argument handling. The
email server and registration therefore have independent long-lived process locks and can
run concurrently. A short setup lock serializes first-run `.venv` creation and dependency
installation across both commands; it is released before either service starts.

The obsolete tracked `run.sh` has no supported references and is deleted. The supported
user commands remain `bash start.sh` and `bash auth-service.sh`.

Generated `.venv/`, `keys/`, caches, logs, and local configuration remain ignored runtime
state rather than tracked source. Their presence does not change the tracked layout.

## Existing Workspace Artifacts

The user explicitly approved removing unrelated root clutter from this worktree.
Identified untracked root items that are unrelated to this repository, including
experiment images, CPA migration material, temporary router work, ad-hoc CSV exports,
screenshots, and malformed empty files, must not be committed.

Before moving any approved root-clutter item, record its relative path, byte size, and
destination in a local manifest. Preserve every item, including empty malformed files,
under a timestamped sibling archive outside the Git worktree. Do not use `git clean`,
reset, deletion, or content-destructive restoration. Do not read credential contents
merely to classify an artifact, and do not copy secrets into Git, commit messages, logs,
or documentation.

Pre-existing tracked modifications and organized untracked project documents under
`docs/` or deployment material under `deploy/` are not silently discarded or bundled
into the layout commit. They remain outside the staged change unless separately reviewed
and intentionally integrated. Before any package migration, preflight every source that
will move or be deleted and every destination that will be created. Any worktree/index
modification, untracked destination collision, or unexpected source aborts the complete
layout migration before its first move; partial package migration is forbidden. Stage
every implementation commit from an explicit path allowlist.

## Compatibility and Migration

- Existing registration output remains in `keys/`; no data-file migration is required.
- Existing authentication output and SQLite inventory remain under the configured local
  authentication directory; no reauthentication is required.
- Existing remote deployments that export the SSH host keep using SSH automatically.
- Existing `.env` registration configuration remains valid.
- Server startup remains `bash start.sh`; local authentication startup remains
  `bash auth-service.sh`.
- The server update must use a fast-forward merge and must not delete live logs or backup
  files. Repository-root server artifacts are handled separately from source migration
  because the running lock file lives below `logs/`.

## Error Handling

Local snapshot command failure, malformed output, unreadable registration files, or an
atomic-write failure emits a sanitized source-disconnected event and continues using the
last valid snapshot. No record-level identifier or absolute path appears in normal mode.
Explicit configuration errors exit before the browser starts and identify only the
invalid configuration key.

Moving implementation files must not change exception classification. Shell entry points
remain `exec`-based so exit codes and signals reach the Python process directly.

## Verification

Verification must cover:

1. `auto` without an SSH host selects local mode.
2. `auto` with an SSH host selects SSH mode.
3. Explicit `local` overrides an existing SSH host.
4. Explicit `ssh` without a host fails with a sanitized configuration error.
5. Local mode accepts an initially absent/empty registration output and later imports
   complete appends without restarting.
6. Exact session Cookie names and scopes survive local export, while `accounts.txt`
   contributes only identifiers missing from the exact session file.
7. Duplicate identifiers collapse deterministically with exact-session precedence.
8. An interleaved registration write cannot expose a legacy fallback before its exact
   session; changing either input generation during export rejects and retries the whole
   candidate.
9. An incomplete or malformed local append, unreadable input, exporter failure, or
   atomic-write failure does not replace the last valid snapshot.
10. A successful local refresh installs a mode `0600` snapshot.
11. SSH source regression tests remain green.
12. Registration and `bash start.sh --email-service` run concurrently with distinct
    long-lived locks; simultaneous first-run invocations serialize setup without starting
    duplicate installation work.
13. Registration, custom mailbox, analysis tools, and tests import from their new package
   locations.
14. A migration preflight conflict at any source or destination leaves every source and
    destination unmoved.
15. `bash -n start.sh setup.sh auth-service.sh` succeeds.
16. An isolated archive of `HEAD` plus the candidate patch runs fake/no-network CLI smoke
    tests for source selection and both shell dispatch branches. It does not install
    dependencies, start a real browser, contact SSH, or require interactive input.
17. The base-to-head diff passes `git diff --check`; `git ls-tree` confirms the approved
    root allowlist and absence of root-level Python shims; every commit's changed paths
    exclude runtime data and credential material.
18. The complete repository test suite passes.

Operational handoff is separate from deterministic repository verification. When the
user has authorized deployment, record the local and server commit IDs, confirm one
registration process on the registration host and one authentication process on the
authentication host (or both on one host for a same-machine topology), and observe
post-restart work. A live upstream success is evidence about that deployment, not a
portable repository acceptance criterion.
