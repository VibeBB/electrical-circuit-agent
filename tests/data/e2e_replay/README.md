# Synthetic library replay transcripts

Run every checked-in synthetic scenario with:

```bash
uv run python scripts/run_library_e2e_replay.py
```

The JSONL files are synthetic tool-call transcripts. The harness creates a
temporary project, invokes the real `circuit.mcp_server.call_tool` dispatcher,
checks declared result predicates and artifact hashes, and reports a hash
manifest before deleting that project. It never starts an agent or contacts a
manufacturer.

The `--live` flag is documented for callers that need to gate a surrounding
evaluation command; it is rejected unless `CIRCUIT_E2E_LIVE=1` is set. Even
when enabled, this script only replays the supplied JSONL files and does not
launch real agents.

`tps62130-happy.jsonl` uses only generated PDF and evidence fixtures. It is
explicitly not evidence about the commercial TPS62130. The other scenarios
exercise an NDA-blocked acquisition ending in an unavailable HumanRequest,
and a substitute-permission request that is first denied and later granted.
