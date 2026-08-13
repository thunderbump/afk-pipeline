# AFK Attempt Executor

Run one structured assignment in a prepared Git workspace and retain an
inspectable Attempt Directory.

```sh
python3 -m afk_attempt assignment.json /new/attempt-directory
```

The destination must not exist. The executor creates it, records the accepted
Assignment as `input.json`, writes the runner's JSON-lines stdout and raw stderr
to `events.jsonl` and `stderr.log`, then atomically writes `output.json` last.

## Assignment

```json
{
  "schema_version": 1,
  "objective": "Make the requested change and commit it.",
  "workspace": "/absolute/path/to/prepared/checkout",
  "command": ["agent", "--mode", "json", "work instructions"],
  "timeout_seconds": 1800,
  "source": {"kind": "bead", "id": "example-123"}
}
```

`source` is optional metadata. `command` is an argv array and is executed
directly, without a shell. Credentials belong to the execution environment;
never place secrets in the Assignment because `input.json` is durable.
`objective` is the durable human-readable work description; the runner command
decides how to present that structured Assignment to its agent.

The caller prepares and selects the workspace branch. The executor only
observes before/after HEAD, branch, porcelain status, dirty state, and commits
reachable between the observed HEADs. A detached HEAD is recorded with a
`null` branch. An unavailable post-run Git observation is retained as a sealed
failure instead of discarding the other Attempt evidence.

## Outcomes

- `succeeded`: the command exited zero and its event stream ended with a
  non-error assistant message plus `agent_end`.
- `failed`: launch, process, or agent-protocol failure.
- `timed_out`: the configured deadline expired.
- `interrupted`: the executor received Ctrl-C while the runner was active.

The executor terminates the runner process group on timeout or interruption.
Exit status is `0` for success, `1` for a sealed non-success outcome, and `2`
for invalid invocation or Assignment input.

## Check

```sh
python3 -m unittest -v
```
