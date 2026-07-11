# NewAPI A/B Release Controller

This directory contains the host-local release controller used by central n8n.
It operates only the primary stack at `/srv/new-api-ai`.

## Commands

```text
rtoc-newapi-deploy status
rtoc-newapi-deploy prepare IMAGE IMAGE_ID REVISION RELEASE
rtoc-newapi-deploy cutover RELEASE
rtoc-newapi-deploy rollback
```

`IMAGE` must be an immutable
`ghcr.io/hechuyi/new-api-rtoc@sha256:<digest>` reference. `prepare` changes and
recreates only the current HAProxy weight-zero slot. `cutover` and `rollback`
restore their original HAProxy weights if stable or public health checks fail.

`IMAGE_ID` is the same top-level `sha256:<digest>` carried by `IMAGE`. The
controller verifies that stable OCI identity before pulling, then records the
production Docker engine's local image ID separately. This avoids assuming that
Docker's `.Id` is always the OCI config digest; containerd-backed image stores
may expose the top-level index digest instead.

`rtoc-newapi-deploy-dispatch` is intended for an SSH `authorized_keys`
forced-command entry. It accepts n8n's `cd / ; COMMAND` wrapper and rejects
arbitrary shell syntax or extra arguments.

## Host State

The controller stores root-only state under:

```text
/srv/new-api-ai/.rtoc-release/current.env
/srv/new-api-ai/.rtoc-release/prepared.env
/srv/new-api-ai/.rtoc-release/evidence.jsonl
```

The runtime defaults can be overridden with `NEWAPI_DEPLOY_*` environment
variables for tests. Production should use the defaults.

## Verification

```bash
python3 -m unittest tests.test_newapi_release -v
bash -n \
  ops/new-api-release/rtoc-newapi-deploy \
  ops/new-api-release/rtoc-newapi-deploy-dispatch
```
