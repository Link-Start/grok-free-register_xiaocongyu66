# Central Release Orchestration for NewAPI and CPA

Last updated: 2026-07-11 Asia/Shanghai

## 1. Objective

Use the Netcup-hosted n8n instance as the central release orchestrator for:

- the primary NewAPI production stack at `/srv/new-api-ai`;
- the RTOC CPA compatibility build;
- machine-executed build, test, artifact verification, image delivery, candidate preparation, preflight, and smoke checks;
- one explicit human approval immediately before production traffic cutover;
- automatic restoration of the old slot when post-cutover smoke fails;
- an independent, human-triggered one-click rollback path.

The workflow starts from a reviewed Git commit. Code authoring and code review remain Git operations performed by an engineer or AI-assisted development process. n8n does not generate patches inside production SSH nodes and does not accept mutable source directories as release inputs.

This design does not target any historical NewAPI stack other than `/srv/new-api-ai`. It does not include the proposed CPA uTLS Transport cache; that optimization remains a separate research and experiment track.

## 2. Constraints and Invariants

The release system must preserve these invariants:

1. NewAPI public traffic continues to enter through the single stable local endpoint `127.0.0.1:3010`.
2. Routine NewAPI release switching does not edit or reload Nginx.
3. NewAPI traffic switching uses the existing HAProxy Runtime API and keeps the previous application slot running at weight zero.
4. CPA and NewAPI have separate credentials, state files, release locks, dispatchers, workflows, manifests, and rollback histories.
5. A release identifies an immutable Git commit and an immutable OCI digest. Mutable tags such as `latest` are never accepted as production identity.
6. n8n SSH nodes call small versioned commands. They do not contain multiline deployment scripts or unrestricted shell access.
7. Repeated execution with the same release ID is idempotent. A conflicting release cannot silently replace an already prepared candidate.
8. Only the cutover gate requires human approval. All earlier quality gates are machine-enforced. Failed gates stop before production traffic changes.
9. A successful cutover is not considered complete until public smoke checks pass and deployment evidence is persisted.
10. Any automatic recovery restores the exact pre-cutover slot and verifies the restored public endpoint.

## 3. Considered Approaches

### 3.1 n8n as both builder and deployer

n8n would clone repositories, run Docker builds, push images, and execute deployment shell directly. This minimizes the number of systems but gives the workflow runner broad Git, registry, filesystem, and production permissions. Long-running builds also make workflow retries and artifact provenance harder to reason about.

This approach is rejected.

### 3.2 GitHub Actions build, n8n orchestration, production dispatcher

GitHub Actions builds and tests an exact commit and publishes an immutable image
plus signed provenance and a signed release-manifest attestation. n8n verifies
the build result, asks the production dispatcher to prepare the inactive slot,
waits at the cutover approval gate, performs the switch, runs smoke checks, and
records evidence.

This is the selected design. It uses each system for the operation it can isolate best: GitHub for source-bound builds, n8n for stateful orchestration and approval, and a narrow production program for host-local deployment mechanics.

### 3.3 Dedicated release service instead of n8n

A custom service would own build polling, approvals, deployment state, and rollback. It could provide stronger domain typing, but it would duplicate n8n's credential storage, execution history, webhook handling, approval, and retry capabilities. It is not justified for the current number of services.

This approach is deferred unless workflow complexity later exceeds n8n's maintainable boundary.

## 4. System Architecture

```text
reviewed Git commit
        |
        v
GitHub Actions
  build + tests + image push + manifest
        |
        v
Netcup n8n
  verify build -> prepare candidate -> preflight
        |
        v
human cutover approval
        |
        v
service-specific forced-command dispatcher
  cutover -> public smoke -> commit or restore
        |
        v
release evidence and rollback handle
```

The common orchestration model is implemented as shared workflow conventions, not as a shared production command. NewAPI and CPA each expose the same logical verbs while retaining service-specific validation and switch mechanics.

## 5. Release State Machine

Each build orchestration has a random 128-bit `build_request_id`. n8n passes it as
a GitHub Actions `workflow_dispatch` input. The workflow uses that value as its
unique `run-name`; n8n polls the configured workflow until exactly one
post-dispatch run has the expected run-name, event, repository, and trusted
workflow-ref head SHA, then locks that run ID. The separately requested source
commit is verified through the signed manifest and provenance, not the workflow
run's `head_sha`. The build embeds the same request ID in its manifest.

An immutable `release_id` is created only after the build finishes:

```text
<service>-<commit-sha>-<workflow-run-id>-<attempt>-<manifest-digest-prefix>
```

The complete manifest digest and OCI digest remain in state; the prefix is only
for readable identifiers. A rerun attempt is a distinct release even when it
builds the same commit. Retrying a failed orchestration may resume the exact
workflow run and attempt, but it may not silently select another successful run
for the same commit.

The durable states are:

```text
REQUESTED
  -> BUILDING
  -> BUILT
  -> VERIFIED
  -> PREPARING
  -> PREPARED
  -> AWAITING_CUTOVER_APPROVAL
  -> CUTTING_OVER
  -> SMOKE_TESTING
  -> ACTIVE
```

Failure states are:

```text
BUILD_FAILED
VERIFY_FAILED
PREPARE_FAILED
CUTOVER_FAILED
SMOKE_FAILED_RESTORED
RESTORE_FAILED
SUPERSEDED
```

State transitions are monotonic except for an explicit rollback operation, which creates a new rollback execution record referring to the immediately retained prior release. Rerunning a node may re-read or re-verify the current state, but cannot move a release backward or overwrite a different prepared release.

Only `PREPARED` may enter `AWAITING_CUTOVER_APPROVAL`. Only a human
approval associated with the exact `release_id`, image digest, target slot,
source slot, manifest digest, state generation, and dispatcher-issued approval
challenge may enter `CUTTING_OVER`.

## 6. GitHub Build Contract

### 6.1 Inputs

The build workflow accepts:

- full Git commit SHA;
- service name;
- random `build_request_id`;
- current production base release ID and commit SHA when cross-version
  compatibility is required;
- optional release reason or incident identifier;
- workflow initiator identity.

The workflow definition runs from a configured trusted release branch. Its
`commit_sha` input is separately validated as a commit in the same repository
and checked out explicitly. Branch names are informational and cannot alter the
checked-out source. Provenance records both the source commit and the workflow
definition revision. GitHub workflow-run `head_sha` is required to match the
trusted workflow ref, while `manifest.commit_sha` and the signed provenance
source subject are required to match the requested source commit.

n8n always dispatches a new run and records the uniquely correlated run ID. It
does not search for an arbitrary prior successful run by commit. No match or
multiple matches is a hard failure. A manual recovery may reuse a prior run only
when the operator supplies its exact run ID and attempt and the manifest contains
the matching `build_request_id`; this remains a machine-verifiable resume, not
run selection by branch or commit alone.

### 6.2 Required build operations

For both services:

- verify the requested commit exists in the configured repository;
- run service-specific unit and compatibility tests;
- build `linux/amd64`;
- push to the service-specific GHCR repository;
- resolve the pushed manifest to an immutable OCI digest;
- generate an SBOM and image metadata;
- generate the release manifest;
- create GitHub/Sigstore build provenance and a signed release-manifest
  attestation bound to the OCI digest;
- upload test output and release metadata as workflow artifacts.

NewAPI uses the existing private repository and extends its current image workflow to emit the full release contract.

CPA first requires a private RTOC repository derived from the official upstream history. Its dedicated workflow builds only the selected `rtoc/*` compatibility branch and publishes to a separate GHCR image name. The official upstream remote remains recorded for audit and update comparison.

### 6.3 Release manifest

The manifest is canonical JSON serialized using RFC 8785 JSON Canonicalization
Scheme, with at least:

```json
{
  "schema_version": 1,
  "service": "newapi",
  "build_request_id": "128-bit random identifier",
  "repository": "owner/repository",
  "commit_sha": "40-character SHA",
  "workflow_run_id": 123,
  "workflow_attempt": 1,
  "image": "ghcr.io/owner/image",
  "image_digest": "sha256:...",
  "platform": "linux/amd64",
  "built_at": "RFC3339 timestamp",
  "tests": [
    {
      "name": "unit",
      "status": "passed",
      "artifact_sha256": "..."
    }
  ],
  "database": {
    "compatibility": "none",
    "evidence_sha256": "..."
  },
  "runtime_compatibility": {
    "base_release_id": "...",
    "base_commit_sha": "...",
    "new_web_with_old_worker": "passed",
    "rollback_read_compatibility": "passed",
    "evidence_sha256": "..."
  },
  "process_role_contract_version": 1,
  "sbom_sha256": "...",
  "source_archive_sha256": "...",
  "builder_identity": "...",
  "manifest_sha256": "..."
}
```

The manifest digest excludes the `manifest_sha256` field while calculating that
field. The self-digest detects corruption but is not treated as proof of origin.
n8n and the production dispatcher independently verify:

- GitHub/Sigstore issuer identity;
- repository owner and repository;
- exact workflow file path and trusted workflow revision;
- workflow run ID, attempt, `build_request_id`, and commit SHA;
- subject OCI digest and signed manifest digest;
- schema, service, platform, image repository allowlist, and checksums.

The trusted workflow path, repository, issuer, and allowed signing identity are
host-local policy. They cannot be supplied by n8n or the manifest.

## 7. n8n Workflow Layers

Each service has four user-facing workflows and reusable internal subworkflows.

The central n8n currently has eleven disabled NewAPI draft workflows. They have
no bound SSH credential and target a historical stack and paths that are outside
this design. They are exported for audit, then replaced with the four-layer
primary-stack workflow below; string substitution or credential binding is not
an acceptable migration. The four disabled CPA workflows are handled separately
as described in Section 10.

### 7.1 Status

Read-only. It reports:

- active and inactive slot;
- slot health and image digest;
- current prepared release, if any;
- release lock owner and age;
- stable/public endpoint health;
- last successful release and last rollback.

Status cannot mutate production state.

### 7.2 Build and Prepare

Input is an exact commit SHA. The workflow:

1. validates input, reads the current active release and state generation, and
   creates a random `build_request_id`;
2. dispatches GitHub Actions with the exact candidate commit, request ID, and
   active base release identity required for cross-version tests;
3. correlates by unique run-name, locks the exact run ID, and waits for its
   current attempt with bounded polling;
4. rejects runs whose trusted workflow-ref head SHA or request ID differs from
   the dispatch, and rejects manifests or provenance whose source commit differs
   from the requested commit;
5. downloads and verifies attestations, release manifest, and artifacts;
6. derives the immutable `release_id`;
7. uploads a one-time `prepare` operation envelope;
8. asks the production dispatcher to consume the envelope;
9. polls `status` until service-specific candidate preparation completes;
10. runs every direct candidate check permitted by the service's isolation model;
11. stores the dispatcher-issued approval challenge and pauses at the cutover gate.

The workflow is safe to rerun. If the same release is already prepared, it returns the current prepared state. If another release is prepared, it fails with a conflict instead of replacing it.

### 7.3 Cut Over Prepared Candidate

This workflow resumes only from an approval payload created by Build and Prepare. It:

1. re-reads dispatcher status;
2. verifies release ID, state generation, source slot, target slot, image digest,
   approval challenge, and expiry still match the approved payload;
3. uploads and consumes a one-time `cutover` operation envelope containing the
   approved challenge and expected generation;
4. runs stable local and public smoke checks;
5. uploads and consumes a fenced `commit` envelope when smoke passes;
6. requests `restore` automatically when smoke fails, while the production
   watchdog independently enforces the same deadline;
7. verifies the restored endpoint and records whether restoration succeeded.

The approval challenge is issued by the production dispatcher, bound to release
ID, manifest digest, source slot, target slot, state generation, and expiry, and
accepted once. An expired approval requires a fresh preflight and challenge; it
does not require rebuilding the image if the candidate remains valid.

The approval action is not a public webhook, public form, email link, or bearer
URL. It is a manual execution inside an n8n project accessible only to
allowlisted immutable n8n user IDs with MFA enabled. The workflow reads the
authenticated execution owner from n8n's execution metadata, rejects identities
outside the service-specific approver allowlist, and writes that user ID,
execution ID, approval timestamp, and challenge into the approval record and
cutover envelope. If the deployed n8n edition or API cannot expose a
non-forgeable authenticated execution owner, cutover remains disabled until an
equivalent authenticated approval broker is installed; display names or
workflow-supplied email fields are not accepted as identity.

### 7.4 Roll Back

Human-triggered and independent of the prepare workflow. One-click rollback
targets only the immediately retained previous release. It:

1. displays current and rollback release identities;
2. requires explicit confirmation of the retained target release;
3. uploads and consumes a fenced `rollback` operation envelope naming the
   retained target and expected generation;
4. inside that operation, cancels any prepared release that occupies the
   rollback slot and ensures the slot runs the retained digest;
5. switches only after direct health passes;
6. runs stable and public smoke checks;
7. automatically restores the release that was active before rollback if the
   rollback candidate fails health or smoke;
8. records the result.

One-click rollback never rebuilds an image and never pulls a mutable tag. An
older release that is no longer present in the retained slot must enter the
normal Build and Prepare flow using its immutable prior digest, followed by a
new cutover approval. It is a redeployment, not one-click rollback.

### 7.5 Internal subworkflows

Internal workflows provide:

- GitHub run trigger and polling;
- artifact and manifest verification;
- dispatcher invocation;
- bounded health polling;
- smoke checks;
- evidence persistence;
- notification on failed build, failed preparation, restored cutover, or restore failure.

They accept typed JSON inputs and return typed JSON outputs. They do not contain service credentials directly; credentials are bound at the calling service workflow.

## 8. Production Dispatcher Interface

The dispatcher is a root-owned, versioned program installed on each production host. n8n reaches it through a dedicated SSH key whose `authorized_keys` entry enforces:

- Netcup source IP allowlist;
- a forced command;
- no port, agent, X11, or PTY forwarding;
- no arbitrary environment variables;
- no arbitrary shell execution.

Each service also has a distinct restricted SFTP upload account. It is chrooted
to a spool containing only a write-only `incoming` directory and cannot execute
commands, list host paths, or read application data. n8n uploads one canonical
operation envelope named by a random 128-bit handle.

The command account and SFTP account use different keys. The forced-command
wrapper tolerates n8n's fixed `cd / ; COMMAND` prefix but rejects all other shell
syntax. The accepted external commands are:

```text
status --service <service>
execute --service <service> --handle <128-bit-handle>
```

`status` is read-only. `execute` atomically consumes an operation envelope whose
operation is one of `prepare`, `cutover`, `commit`, `restore`, or `rollback`.
The SSH command line therefore carries no registry credential, raw manifest,
approval capability, or arbitrary image reference.

The dispatcher opens the envelope with `O_NOFOLLOW`, requires a regular file
owned by the service's upload account, enforces a small maximum size and exact
filename, validates canonical JSON, service, nonce, expiry, expected state
generation, n8n execution ID, release identity, build attestations, and
operation-specific fields, then atomically renames it into a non-uploadable
`consumed` directory before mutation. A consumed handle can never be replayed.

For `cutover`, the envelope must contain the unexpired one-time approval
challenge issued by the dispatcher during preflight. `commit`, `restore`, and
`rollback` are fenced by the operation generation created under the service
lock. Commands from stale or duplicate n8n executions fail with a typed conflict
instead of changing the current release.

All responses are single JSON objects with:

```json
{
  "ok": true,
  "operation": "status",
  "service": "newapi",
  "release_id": "...",
  "state": "PREPARED",
  "details": {},
  "error": null
}
```

Exit code zero means the requested operation reached a valid terminal state. Nonzero exits return a machine-readable error code. Secrets and full environment variables are never included.

## 9. NewAPI Deployment Mechanics

NewAPI dispatcher configuration is fixed to:

```text
compose directory: /srv/new-api-ai
slot A endpoint:   127.0.0.1:3012
slot B endpoint:   127.0.0.1:3013
stable endpoint:   127.0.0.1:3010
HAProxy socket:    /run/new-api-runtime-proxy/admin.sock
public endpoint:   api.rtoc.cc
```

`prepare`:

1. acquires the NewAPI release lock;
2. determines the active slot from HAProxy Runtime API weights;
3. selects the zero-weight slot as candidate;
4. pulls the exact OCI digest;
5. updates only the candidate slot through a generated compose override or service-specific image variable;
6. recreates only the candidate container in `web` role;
7. waits for container health and direct `/api/status`;
8. verifies the running container image digest;
9. records the pre-cutover snapshot and prepared release atomically.

The current NewAPI binary is not passive merely because it has zero HAProxy
weight. Startup normally performs `AutoMigrate`, and several background workers
start independently of traffic. Therefore NewAPI automation is not enabled
until the application and compose stack implement a tested process-role
contract:

- `web`: serves HTTP, performs no database migration, and starts no scheduled
  job, task poller, credential refresh, cleanup, channel test, reporter, queue
  consumer, or other mutating background worker;
- `migrate`: performs only the declared database migration and exits;
- `worker`: starts the mutating background jobs, exposes a local health
  heartbeat, and does not serve public traffic.

Both A/B application slots always run in `web` role. A separate singleton
`new-api-worker` service runs the active release's `worker` role. Compose and the
dispatcher enforce one worker container name and stop the old worker before
starting a new one, so worker versions cannot overlap. Tests fail when a newly
added background component has not declared its process role.

The build manifest declares database compatibility as `none`,
`backward_compatible`, or `incompatible`, with test evidence. The ordinary A/B
workflow rejects `incompatible`. A release marked `backward_compatible` must
prove that any startup migration is additive and that both the old and new
binaries, including the old worker and new web process, operate against the
resulting schema. Missing or unproven migration metadata fails before approval.

NewAPI also requires a runtime cross-version contract bound to the active base
release captured before build. Tests must prove:

- the old worker understands every queue/task payload, Redis record, lock, and
  background-work request that the new Web process can emit during cutover
  smoke;
- the old Web and old worker can read database and Redis state written by the
  candidate release before a watchdog or human rollback;
- any changed payload is versioned or dual-readable across the active/candidate
  pair.

The dispatcher rechecks that the active base release and generation still match
the manifest before cutover. Missing evidence, a changed active base, or an
incompatible queue/data contract fails closed. Such a release requires an
explicit coordinated migration design and is outside the ordinary A/B workflow.

`cutover`:

1. verifies the prepared candidate, approval challenge, generation, and both slot identities;
2. arms the host-local cutover watchdog;
3. runs the exact candidate image once in `migrate` role when the manifest
   declares a backward-compatible migration;
4. verifies the old web slot and singleton old worker remain healthy;
5. sets the candidate web slot weight to 100;
6. sets the previous active web slot weight to 0;
7. verifies HAProxy reports the expected weights and candidate health;
8. leaves the previous web container running in side-effect-free `web` role and
   leaves the old singleton worker active until public smoke passes.

`commit`, after public smoke:

1. stops the singleton old worker;
2. starts `new-api-worker` with the candidate digest in `worker` role;
3. waits for its health heartbeat;
4. if worker startup fails, recreates the old worker digest and restores the
   pre-cutover HAProxy weights;
5. otherwise marks the candidate release active and disarms the watchdog.

At no point do two mutating NewAPI workers run concurrently. A bounded worker
gap is permitted during the stop/start handoff; public Web traffic remains
served throughout it.

No Nginx file, Nginx process, NixOS configuration, database option, or shared
Redis/application setting is changed by routine release operations. The only
permitted shared Postgres mutation is a manifest-declared, tested,
backward-compatible startup schema migration executed inside the approved
cutover transaction.

`restore` reverses the Runtime API weights to the exact pre-cutover snapshot,
ensures the singleton worker runs the old digest, and disarms only the matching
generation. A successful `commit` retains the old web and worker release
metadata, manifest, and image digest as the sole one-click rollback target. A
later prepare may replace the then-inactive web container, but it does not
change the retained rollback identity. If rollback is requested while that slot
contains a newer prepared candidate, the dispatcher first cancels the candidate
and reconstructs the retained old web digest before changing traffic, then
performs the same singleton worker handoff in reverse.

Introducing process roles uses a one-time, separately tested bootstrap
transaction. The bootstrap image is based on the current production code and may
contain only the process-role refactor and its tests: no schema migration,
feature change, queue format change, or shared configuration change is allowed.
Before mutation, the dispatcher archives and checksums the complete legacy
compose files, environment references, slot images, container settings, HAProxy
weights, and active-slot identity.

The approved bootstrap transaction:

1. stops and replaces only the zero-weight legacy slot with the baseline image
   in `web` role;
2. switches Web traffic to that role-capable slot and runs public smoke while
   the old active legacy container continues its existing background work;
3. stops the old legacy container, starts the baseline singleton worker, and
   verifies worker health;
4. recreates the old slot with the same baseline image in `web` role at weight
   zero;
5. commits only when both role-capable Web slots and exactly one baseline worker
   are healthy.

The bootstrap watchdog has a dedicated restore adapter. At any failure boundary
it stops the baseline worker and Web containers, restores the archived legacy
compose/image/container topology and HAProxy weights, and verifies the original
public endpoint. This legacy restoration path is exercised before bootstrap is
approved. After bootstrap commits, both slots and the singleton worker support
the role contract; the first ordinary role-capable release then retains the
baseline as its standard one-click rollback target. Bootstrap evidence and its
legacy restore command remain archived separately from normal release state.

## 10. CPA Deployment Mechanics

The central n8n currently contains four disabled CPA workflows:

```text
RTOC CPA / 00 Status
RTOC CPA / 01 Prepare Candidate
RTOC CPA / 02 Cut Over Prepared Candidate
RTOC CPA / 03 Roll Back
```

Only Status has passed an end-to-end n8n execution. The other workflows are
drafts, not proven deployment paths. Implementation upgrades these workflows in
place after exporting a backup; it does not create a second competing CPA
workflow family.

CPA retains its existing stable endpoint and the production controller's
single-writer invariant. The current A/B compose mounts shared mutable
configuration, auth, and log directories, so running both CPA application slots
simultaneously is not treated as safe prewarming.

`prepare` pulls or loads the exact CPA image digest, assigns it to the inactive
slot, verifies image identity, platform, binary version, and attestation, and
records the prepared release without starting a second writer. `cutover`, after
approval, drains the active slot through the stable HAProxy endpoint, stops it,
starts and probes the prepared slot, then enables it. The existing controller
already restores the old slot when candidate start, health, direct smoke, or
stable routing fails; the new dispatcher adds generation fencing, signed
artifact attestations, authenticated one-time operation envelopes, typed JSON,
and evidence around that behavior.

`restore` and one-click `rollback` return to the immediately retained old
release. If `prepare` has already replaced the inactive slot's configured image,
rollback first reconstructs that retained digest. It restores the pre-operation
active slot if the rollback target fails. True
simultaneous CPA prewarming would require isolated mutable state or an explicit
passive mode and is outside this release-orchestration implementation.

The current CPA compatibility release remains valid production state. Introducing the GitHub repository and workflow does not rebuild or replace it until a later, explicitly approved release.

## 11. Idempotency, Locking, and Recovery

Each service has:

- one advisory file lock for state-changing operations;
- one atomic JSON state file;
- an append-only JSONL evidence log;
- a directory of immutable release manifests;
- immutable release history plus exactly one designated one-click rollback
  release per service.

State writes use write-to-temp, `fsync`, and atomic rename. The state includes:

- schema version;
- active and inactive slots;
- active, prepared, and rollback release IDs;
- image digests;
- workflow execution ID;
- operation owner;
- monotonic state generation and operation generation;
- consumed operation-envelope handle;
- operation start and completion timestamps;
- pre-cutover weight snapshot;
- watchdog deadline and systemd unit identity;
- last smoke result;
- last error code.

If an execution is interrupted:

- before cutover, the active slot remains unchanged and a later `prepare` may resume or clean the incomplete candidate;
- after one HAProxy weight operation but before the second, the dispatcher reconciles against the stored pre-cutover snapshot and either completes the intended switch or restores the old state;
- before the first cutover mutation, the dispatcher persists the pre-cutover
  snapshot and starts a service-specific systemd watchdog bound to the operation
  generation and deadline;
- after cutover but before smoke completion, the watchdog restores the old slot
  unless the same generation is explicitly committed;
- after host reboot, a boot-time reconciliation service reloads pending
  operations, rejects stale n8n commands, and restores any uncommitted cutover
  whose deadline has passed;
- after successful smoke but before evidence recording, rerunning `commit` is idempotent.

Every state-changing operation acquires the service lock and supplies
`expected_generation`. The dispatcher increments generation before releasing the
lock. `commit`, `restore`, and watchdog actions compare both release ID and
operation generation; only one can win. The loser returns an idempotent terminal
result or a stale-generation conflict. The watchdog is service-local and does
not depend on n8n remaining online.

For steady-state NewAPI, restoration resets the exact HAProxy Web-slot weight
snapshot, stops any partially started singleton worker, recreates the old worker
digest when necessary, and leaves the failed candidate Web slot at weight zero
or stopped. For the one-time NewAPI bootstrap, restoration uses the archived
legacy-topology adapter defined in Section 9. For CPA, restoration uses the
existing controller to stop the failed slot and restart the recorded old slot.

## 12. Smoke and Health Contract

Smoke checks have three levels:

1. process health: container running and declared health check passing;
2. direct candidate health: service-specific local endpoint returns expected status and schema;
3. stable/public health: stable local endpoint and public domain return expected status.

NewAPI release smoke must avoid billable or mutating customer operations. It checks `/api/status` and one authenticated, low-cost compatibility probe only when a dedicated smoke credential exists. CPA uses a dedicated non-customer credential and a minimal protocol request that validates SSE completion semantics without consuming a customer account.

Transient failures are retried with fixed bounds. A smoke sequence records every attempt and latency. The release passes only when all required checks succeed within the configured deadline.

## 13. Credentials and Trust Boundaries

Credential sets are isolated by service and purpose:

- GitHub build trigger/read credential;
- GHCR pull credential on the production host;
- NewAPI dispatcher SSH credential;
- NewAPI restricted SFTP spool credential;
- CPA dispatcher SSH credential;
- CPA restricted SFTP spool credential;
- NewAPI smoke credential;
- CPA smoke credential;
- optional notification credential.

Build/prepare workflows and cutover workflows live in separate n8n projects.
The build project cannot use cutover SFTP or dispatcher credentials. The
cutover project has no public trigger and is shared only with the allowlisted
approver principals. n8n authentication, MFA, project membership, and execution
retention are therefore part of the production approval boundary, not optional
administrative settings.

The GitHub credential uses the minimum repository Actions and artifact permissions needed. It cannot administer organizations or unrelated repositories. Production GHCR credentials are read-only and restricted to their image package.

n8n stores encrypted credentials but does not receive database passwords, application `.env` files, or unrestricted root SSH keys. The dispatcher reads host-local application configuration without returning secrets.

An AI review node may later summarize commit changes, test output, upstream
issue/PR evidence, and risk markers. It is deferred until both deterministic
release paths are operational. Its future output is advisory metadata.
Machine-enforced tests and artifact verification remain authoritative gates; the
only human gate is production cutover approval.

## 14. Evidence and Audit

Every release records:

- initiator, immutable approver user ID, approval execution ID, and approval
  challenge digest;
- service, repository, commit SHA, image digest, manifest digest;
- GitHub workflow run and attempt;
- test and artifact verification results;
- production source and target slots;
- pre- and post-cutover weights;
- direct, stable, and public smoke attempts;
- automatic restoration result, if invoked;
- dispatcher version and n8n workflow version;
- timestamps and correlation IDs.

Evidence is stored in n8n execution data and copied to the production append-only evidence log. Sensitive headers, tokens, private keys, environment variables, and customer request bodies are redacted before persistence.

## 15. Testing Strategy

### 15.1 Dispatcher tests

- parser rejects arbitrary shell, separators, extra arguments, unknown services, and path traversal;
- SFTP spool tests reject readable/listable host paths, symlinks, oversized files,
  wrong ownership, malformed handles, expired envelopes, and replayed handles;
- manifest validation rejects mutable tags, wrong repositories, wrong platforms,
  malformed digests, stale handles, checksum mismatch, wrong GitHub issuer,
  untrusted workflow identity, wrong run/attempt, and wrong signed subjects;
- state transitions reject skipped states and conflicting release IDs;
- repeated `status`, `prepare`, `cutover`, `commit`, and `restore` calls are idempotent;
- stale expected generations cannot commit or restore a newer operation;
- interruption tests cover every write and switch boundary;
- HAProxy adapter tests use a fake Runtime API socket;
- compose adapter tests verify only the inactive service is recreated;
- NewAPI process-role tests prove A/B Web slots cannot migrate or start mutating
  workers, `migrate` exits after schema work, and only the singleton `worker`
  service runs background jobs;
- worker handoff tests prove old and new worker versions never overlap and a
  failed new worker restores the old digest and Web weights;
- database compatibility tests prove old and new binaries both operate after any
  release classified as backward compatible;
- cross-version contract tests cover new-Web/old-worker queue and Redis payloads
  plus old-release reads after candidate writes;
- bootstrap fault-injection tests restore the archived legacy topology from
  every mutation boundary;
- secret-redaction tests scan output and evidence.

### 15.2 Workflow tests

- mocked GitHub run with a mismatched trusted workflow-ref head is rejected, and
  signed provenance with a mismatched requested source commit is rejected;
- workflow run, attempt, and `build_request_id` must all match;
- failed tests or missing artifacts stop before production SSH;
- same release resumes preparation;
- conflicting prepared release fails closed;
- approval payload tampering and expiry are rejected;
- public or anonymous approval triggers are absent, and unauthorized n8n user IDs
  cannot produce a cutover envelope;
- smoke failure invokes restore;
- a lost n8n execution still triggers host-local watchdog restoration;
- restore failure raises a high-severity terminal state;
- one-click rollback uses only the immediately retained prior release and
  restores the pre-rollback release if rollback smoke fails.

### 15.3 Production rollout tests

For each service:

1. install dispatcher and forced-command key without enabling state changes;
2. execute `status` end to end from n8n;
3. verify all forbidden commands are rejected;
4. prepare a candidate in the inactive slot without traffic change;
5. verify candidate directly;
6. obtain human approval;
7. cut over and run smoke;
8. exercise one controlled rollback;
9. verify evidence and retained slot identities.

Before NewAPI enters this sequence, rehearse the one-time process-role bootstrap
and legacy restoration against an isolated clone of the production topology,
then execute the approved bootstrap so both Web slots are side-effect free and
exactly one worker service owns background jobs.

## 16. Delivery Order

Implementation is split into independent release boundaries:

1. common canonical manifest, attestation policy, evidence schema, operation
   envelope, restricted SFTP spool, and dispatcher protocol;
2. NewAPI `web`/`migrate`/`worker` process-role and database compatibility
   contract;
3. NewAPI bootstrap transaction and legacy-topology restore adapter, validated
   against an isolated production-topology clone;
4. NewAPI dispatcher with read-only status and fake-adapter tests;
5. approved NewAPI process-role bootstrap;
6. NewAPI prepare path and candidate-only production validation;
7. NewAPI cutover, watchdog restore, immediate rollback, and replacement of the
   unusable historical NewAPI n8n drafts;
8. CPA private repository and dedicated GitHub build workflow;
9. CPA dispatcher fencing around the already deployed A/B controller;
10. upgrade of the four existing disabled CPA workflows, followed by controlled
   end-to-end prepare, cutover, and rollback exercises;
11. optional advisory AI review node after both deterministic pipelines are
   operational.

All newly imported or created n8n workflows remain disabled during construction. They are enabled only after the corresponding status and candidate preparation paths have passed end-to-end verification. No production cutover is executed without the explicit human approval bound to that release.
