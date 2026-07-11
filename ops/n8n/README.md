# Central NewAPI Release Workflows

`newapi-workflows.json` defines the four disabled operator workflows installed
on the central n8n instance:

```text
RTOC New API / 00 Status
RTOC New API / 01 Build and Prepare
RTOC New API / 02 Cut Over Prepared Candidate
RTOC New API / 03 Roll Back
```

Regenerate and validate the import file with:

```bash
python3 ops/n8n/build_newapi_workflows.py
python3 ops/n8n/build_newapi_workflows.py --check
python3 -m unittest tests.test_n8n_newapi_workflows -v
```

The workflows expect these encrypted n8n credentials:

```text
RTOCNewAPIDeploySSH001
RTOCNewAPIGitHubActions001
```

The release sequence is serial:

```text
exact commit -> GitHub tests/build -> immutable manifest callback
-> prepare weight-zero slot -> verify unchanged weights
-> explicit CUTOVER approval -> HAProxy Runtime API switch
```

`Build and Prepare` stops at `AWAITING_APPROVAL`. It does not modify Nginx and
does not change production traffic weights. `Cut Over` and `Roll Back` are
separate manual workflows with explicit confirmation strings.

The host-local NewAPI dispatcher also uses `flock`, so two n8n executions cannot
modify the A/B deployment concurrently.
