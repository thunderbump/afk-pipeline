# AFK Pipeline

Small executable modules for running agent work, validating its result, and
reviewing one validated implementation.

## Attempt Executor

Run one structured assignment in a prepared Git workspace and retain an
inspectable Attempt Directory.

```sh
python3 -m afk_attempt assignment.json /new/attempt-directory
```

The destination must not exist. The executor creates it, records the accepted
Assignment as `input.json`, writes the runner's JSON-lines stdout and raw stderr
to `events.jsonl` and `stderr.log`, then atomically writes `output.json` last.
During execution, the wrapper writes concise timestamped progress to its stdout
and flushes each line immediately for input acceptance, repository observations,
artifact preparation, child start and completion, and final outcome sealing.
Normal progress is never written to
wrapper stderr, and runner stdout/stderr stay routed only to the artifact logs.

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

- `succeeded`: the command exited zero and its event stream contains a
  non-error assistant message followed by `agent_end`; one final
  `agent_settled` event is also accepted for current Pi.
- `failed`: launch, process, or agent-protocol failure.
- `timed_out`: the configured deadline expired.
- `interrupted`: the executor received Ctrl-C while the runner was active.

The executor terminates the runner process group on timeout or interruption.
Exit status is `0` for success, `1` for a sealed non-success outcome, and `2`
for invalid invocation or Assignment input.

## Repository Validation

Run one repository-owned validation command in a prepared Git workspace and
retain an inspectable result directory:

```sh
python3 -m afk_validate validation.json /new/result-directory
```

The result directory must not exist. Validation input is structured JSON:

```json
{
  "schema_version": 1,
  "workspace": "/absolute/path/to/prepared/checkout",
  "command": ["./scripts/validate"],
  "timeout_seconds": 1800
}
```

The workspace implicitly selects the branch or detached ref. `command` is one
exact argv array and runs directly without a shell. The target repository owns
everything behind that command, including any test-service container
lifecycle.

The validator writes the accepted input to `input.json`, raw command output to
`stdout.log` and `stderr.log`, then atomically writes `output.json` last. It
records before/after HEAD, branch, and worktree status. While it runs, wrapper
stdout receives concise timestamped progress, flushed line-by-line immediately,
for input acceptance, repository observations, result-directory preparation,
child start and completion, and final outcome sealing. Normal progress is never
written to wrapper stderr, and
validation command stdout/stderr stay routed only to `stdout.log` and
`stderr.log`. A zero-exit command passes only when HEAD remains unchanged and
post-command Git observation succeeds; worktree dirtiness is recorded but is not
itself a failure.

Validation outcomes are `passed`, `failed`, `timed_out`, or `interrupted`. The
validator terminates the command process group on timeout or interruption.
Exit status is `0` for pass, `1` for a sealed non-success result, and `2` for
invalid invocation or input.

## Review

Run one agent review of a completed Committed Change and its passed Validation:

```sh
python3 -m afk_review review.json /new/result-directory
```

Review input is structured JSON:

```json
{
  "schema_version": 1,
  "workspace": "/absolute/path/to/prepared/checkout",
  "change_directory": "/absolute/path/to/completed/committed-change",
  "validation_directory": "/absolute/path/to/passed/validation",
  "timeout_seconds": 900
}
```

The prepared workspace must be clean and have the same `HEAD` and status as the
Committed Change's final state and both Validation observations. Its branch is
implicit and may be detached. Before creating the result directory, Review
rejects failed, dirty, stale, malformed, or mismatched evidence. The old
`attempt_directory` input is not supported.

The wrapper records the complete commit diff as `diff.patch`. The default
adapter invokes Pi with `gpt-5.6-sol`, gives it only read-oriented tools, and
points it at that artifact and the prepared workspace. Authentication is
inherited from the environment. A deployment or deterministic test may replace
the adapter outside the durable input by setting
`AFK_REVIEW_AGENT_COMMAND` to a JSON argv array; Review appends its generated
prompt as the final argument. Do not put credentials in this configuration or
the Review input.

Review retains `input.json`, `diff.patch`, raw `events.jsonl`, raw `stderr.log`,
and an atomically sealed `output.json`. A completed response contains a summary
and a possibly empty findings array. Every finding requires severity, title,
details, and at least one repository-relative path with a positive 1-based line
number that exists in a text file under the reviewed `HEAD`. Findings do not
make execution fail and do not authorize repair or GitHub posting.

Review outcomes are `completed`, `failed`, `timed_out`, or `interrupted`. A
review completes only when the child exits zero, the Pi event stream and
structured response are valid, and the workspace remains unchanged. Exit
status is `0` for completed, `1` for a sealed non-success result, and `2` for
invalid invocation, configuration, input, or evidence.

## Finding Assessment

Assess whether every finding from one completed Review is worth addressing:

```sh
python3 -m afk_assess assessment.json /new/result-directory
```

Assessment input is structured JSON:

```json
{
  "schema_version": 1,
  "workspace": "/absolute/path/to/the/reviewed/checkout",
  "review_directory": "/absolute/path/to/a/completed/review",
  "timeout_seconds": 900
}
```

The prepared workspace must match the completed Review's clean committed state.
Before creating the result directory, Finding Assessment validates that Review's
structured findings against the exact reviewed Git object. The branch remains
implicit and may be detached.

The default adapter invokes Pi with `gpt-5.6-sol` and read-oriented tools.
Authentication is inherited from the environment. A deployment or deterministic
test may set `AFK_ASSESS_AGENT_COMMAND` to a JSON argv array; Finding Assessment
appends its generated prompt as the final argument.

The result directory contains `input.json`, raw `events.jsonl`, raw
`stderr.log`, and an atomically sealed `output.json`. A completed assessment has
a summary and exactly one decision for every immutable Review finding. Each
decision contains its zero-based `finding_index`, boolean `worth_addressing`,
and non-empty `rationale`. A Review with no findings requires an empty decisions
array. Findings may not be skipped or duplicated.

Assessment outcomes are `completed`, `failed`, `timed_out`, or `interrupted`.
Completion is separate from the boolean decisions and does not authorize repair,
aggregate routing, or GitHub posting. The workspace must remain unchanged. Exit
status is `0` for completed, `1` for sealed non-success, and `2` for invalid
invocation, configuration, input, or Review evidence.

## Feedback Response

Respond to every actionable decision from one completed Finding Assessment:

```sh
python3 -m afk_respond response.json /new/result-directory
```

Feedback Response input is structured JSON:

```json
{
  "schema_version": 1,
  "workspace": "/absolute/path/to/the/assessed/checkout",
  "assessment_directory": "/absolute/path/to/a/completed/assessment",
  "timeout_seconds": 900
}
```

Before creating the result directory, Feedback Response verifies the complete
Assessment-to-Review-to-Committed-Change evidence chain and requires the prepared
workspace to be at its exact clean assessed `HEAD`. The branch remains implicit
and may be detached. Stale, malformed, failed, dirty, or mismatched evidence is
refused with exit status `2`.

One invocation selects all and only Assessment decisions whose
`worth_addressing` value is true. The default adapter invokes Pi with
`gpt-5.6-sol` and workspace-writing tools, and requires it to create a clean Git
commit. Authentication is inherited from the environment. A deployment or
deterministic test may set `AFK_RESPOND_AGENT_COMMAND` to a JSON argv array;
Feedback Response appends its generated prompt as the final argument.

The result directory contains `input.json`, raw `events.jsonl`, raw
`stderr.log`, and an atomically sealed `output.json`. A completed agent response
contains a summary and exactly one non-empty response for every selected
immutable `finding_index`. Completion also requires a clean final workspace at
a new descendant `HEAD`; the wrapper records the exact commits between the two
heads. When no findings are actionable, Feedback Response completes without
starting an agent, leaves `HEAD` unchanged, and writes empty raw logs.

Outcomes are `completed`, `failed`, `timed_out`, or `interrupted`. Exit status is
`0` for completion, `1` for sealed non-success, and `2` for invalid invocation,
configuration, input, or evidence. The component does not run deterministic
validation, re-review its commit, publish feedback, watch for later events,
retry, or decide iteration limits. A caller may invoke it again for a later
independent feedback set.

## Committed Change

Project one successful write-capable component into a small, common description
of its committed change:

```sh
python3 -m afk_change source.json /new/result-directory
```

The structured source input names either a succeeded Attempt or a completed,
actionable Feedback Response:

```json
{
  "schema_version": 1,
  "source": {
    "kind": "attempt",
    "directory": "/absolute/path/to/source-evidence"
  }
}
```

`source.kind` is `attempt` or `feedback_response`. Before creating the result
directory, Committed Change validates the source's complete durable evidence
chain. Both source repository states must be clean and have distinct heads; the
heads and recorded commits must be immutable commit object IDs, and the recorded
commits must exactly match Git's descendant commit range. A Feedback Response
must additionally match its completed read-only Assessment and Review, the
structured actionable findings and responses, and the originating succeeded
Attempt's workspace, final state, and objective.

The result contains the accepted `input.json` and an atomically sealed
`output.json`. A completed output exposes one `change` object with the frozen
Assignment `objective`, `workspace`, full `before` and `after` repository
states, and source provenance. Exit status is `0` for completion and `2` for
invalid invocation, input, or source evidence; refused input never creates a
result directory. The result must be outside both the source workspace and
source evidence directory. This deterministic adapter runs no agent, mutates no
Git state, does not require the workspace still to be checked out at the final
head, and does not run Validation or Review.

## Iteration Policy

Decide whether one reviewed and assessed lineage should stop, continue with
another Feedback Response, or stop because its response budget is exhausted:

```sh
python3 -m afk_iterate policy.json /new/result-directory
```

Iteration Policy input is structured JSON:

```json
{
  "schema_version": 1,
  "assessment_directory": "/absolute/path/to/latest/completed-assessment",
  "max_responses": 2
}
```

`max_responses` is a nonnegative limit on Feedback Response executions; it does
not count the initial implementation Attempt. Before creating the result
directory, the component verifies the completed Finding Assessment, Review,
Committed Change, and recursive source evidence, then derives the number of
prior Feedback Responses from that immutable lineage.

The result contains accepted `input.json` and an atomically sealed `output.json`.
Its completed `policy` records the derived response and actionable-finding
counts, the configured limit, a reason, and one deterministic decision:

- `stop` when the latest Assessment has no actionable findings, regardless of
  remaining budget.
- `exhausted` when actionable findings remain and the response limit has been
  reached.
- `continue` otherwise; only this decision includes `next_response_number`.

Exit status is `0` after a completed decision and `2` for invalid invocation,
input, or evidence. Refusal happens before result creation, existing results are
not replaced, and the result must be outside the source workspace and every
traversed evidence directory.
The component runs no agent or other AFK component and stores no mutable
aggregate state. Resuming or changing the response limit is a new invocation
over the same evidence and leaves every prior sealed result unchanged.

## Check

Install the repository's Ruff commit hooks once per checkout:

```sh
pre-commit install
```

Run the complete repository check:

```sh
./scripts/validate
```
