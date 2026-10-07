# Automation templates

Ready-made automation requests for the plugin preset
(`POST /v1/preset/plugin` on the automation service, or the Automations
screen in Agent Canvas). Each file is a request body; copy it, adjust the
cron schedule and the workspace paths, and submit it. Automations run with
the plugin attached, so `circuit` tools and hooks work exactly as in an
interactive conversation — including the deterministic gates.

- `reverify-weekly.automation.json` — re-runs the ERC/DRC gates on a
  checked-in board project every week and reports drift between the
  recorded design-report verdicts and a fresh run.

Keep `plugins` pointing at the released plugin path (`plugins/circuit` on
`main`) unless you are testing a development branch. `model` names the LLM
profile for runs; leave it out to use the active profile. `repos` clones
the workspace repository that carries the design inputs.
