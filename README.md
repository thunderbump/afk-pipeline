# AFK Pipeline

Small executable modules for running agent work, validating and reviewing its
result, responding to feedback, and coordinating the accepted sequence.

## Run Preparer

From any working directory, prepare and execute one central Bead as a local AFK
Coordinator Run:

```sh
/path/to/afk-pipeline/afk run central-123
# Non-default/test installation:
/path/to/afk-pipeline/afk run central-123 --config /absolute/path/to/config.json
```

The default configuration is `~/.config/afk/config.json`. It uses this minimal
schema:

```json
{
  "schema_version": 1,
  "beads_workspace": "/absolute/path/to/central-beads",
  "run_root": "/absolute/caller-owned/path/to/runs",
  "worktree_root": "/absolute/caller-owned/path/to/worktrees",
  "assignment": {
    "command": ["agent", "--mode", "json", "Read", "{assignment_path}"],
    "timeout_seconds": 1800
  },
  "coordinator": {
    "agent_timeout_seconds": 900,
    "max_responses": 2
  },
  "publication": {
    "command": [
      "node",
      "/absolute/path/to/operations-webui/scripts/admit-bundle.mjs",
      "{bundle_path}",
      "/absolute/path/to/operations-webui-store"
    ],
    "timeout_seconds": 120
  },
  "projects": {
    "example": {
      "repository": "/absolute/path/to/example-repository",
      "base_ref": "main",
      "validation": {
        "command": ["./scripts/validate"],
        "evidence": "Ruff checks and the complete repository unit-test suite.",
        "timeout_seconds": 1800
      }
    }
  }
}
```

`publication` is optional. When present, its command must contain exactly one
argv element equal to `{bundle_path}`. The preparer replaces that element with
a private temporary Publication Bundle path and invokes the command without a
shell. The current adapter is Operations Datastore Admission, but the preparer
depends only on its small versioned JSON result contract.

The trusted host process runs `bd show <bead-id> --json` only in the configured
central Beads workspace and requires exactly one `project:<slug>` label. It
resolves that project locally, freezes the base ref to a commit, and creates a
new flat `afk-<bead-id>-<run-id>` branch and isolated worktree. The flat name
cannot conflict with a bootstrap branch such as `afk/<bead-id>`, and preparation
preflights exact, ancestor, and descendant branch-ref namespace collisions. It
never fetches, clones, reuses, or replaces a destination. Beads connection
settings remain in the preparer environment and are not forwarded to Coordinator
workers or written to durable evidence.

The assignment command must contain exactly one argv element equal to
`{assignment_path}`. During preparation that element is replaced, without a
shell, by the generated absolute `assignment.json` path. Embedded or repeated
placeholders, commands without the placeholder, and commands that use the
Assignment path as their executable are rejected before any run destination is
created. This lets a configurable worker read the frozen Bead objective while
keeping Beads lookup in the trusted preparer.

Each project validation mapping also requires bounded `evidence` text describing
what its exact repository-owned command proves. Run Preparer uses that trusted
metadata, the exact validation argv, existing AFK evidence capabilities, and the
operator handoff boundary to build the acceptance-evidence catalog. The Bead
does not supply or modify those capabilities.

Every accepted preparation has a unique `<run_root>/<bead-id>/<run-id>/`
artifact root. It contains value-safe `bead.json`, `assignment.json`,
`preflight-input.json`, `coordinator-request.json`, versioned
`preparation.json`, and reserved `preflight/` and `coordinator/` directories.
After the worktree and inputs are frozen, Run Preparer invokes Acceptance
Evidence Preflight before Coordinator. A valid `proceed` hands the unchanged
Assignment to Coordinator. A valid `pause`, failed classifier, or malformed
Preflight evidence exits `1`, leaves Coordinator `not_started`, and starts no
implementation Attempt. A valid `pause` retains its classified request ledger.
A failed classifier or malformed result retains the accepted input, raw agent
streams, and sealed pause outcome without claiming a valid request ledger.
Progress prints every validated request category and route plus the terminal
Preflight decision.

A paused or failed Preflight is terminal for that Run; Run Preparer never
reclassifies or resumes it in place. After correcting the Bead, configuration,
or transient failure, invoke `afk run <bead-id>` again. The retry creates a new
Run ID, worktree, and branch while retaining the earlier Run and worktree for
inspection.

For a validated sealed Coordinator output, `preparation.json` records `stop` or
`exhausted` in `coordinator.decision`; failed or malformed output leaves that
field null. The JSON evidence is authoritative. Preparation failures after
artifact creation are sealed with categorized errors. If worktree creation
fails, any state left by Git at the worktree destination is preserved for
inspection rather than being automatically removed. An accepted `stop` exits
zero, while `exhausted` exits `1` so actionable findings are visible to command
line callers. Nonzero Coordinator exits are propagated, a zero exit without a
valid completed output becomes `1`, and preparation/input refusal exits `2`.

After sealing terminal Coordinator facts in `preparation.json`, a configured
preparer exports the Run, invokes the publication command, and removes the
temporary bundle. It writes raw adapter streams to `publication.stdout` and
`publication.stderr`, then atomically seals `publication.json` last. A valid
`accepted` or `replayed` result records publication success. Conflict,
rejection, timeout, launch, malformed adapter output, or export failure records
a categorized failure. Publication never rewrites Coordinator evidence. A
publication failure changes an otherwise successful `afk run` exit to `1`;
an already-unsuccessful Coordinator retains its existing exit behavior.
Without `publication`, the command behaves exactly as before and creates no
publication artifacts.

Run and worktree roots, their existing ancestors, and the repository are trusted
local-host infrastructure. The preparer takes advisory directory locks to
serialize cooperating preparers, creates Bead/run entries relative to open root
descriptors, writes initial evidence through its owned artifact descriptor, and
revalidates artifact and worktree identities before Coordinator handoff. Thus a
supported concurrent preparer cannot race destination ownership, and an
intermediate symlink swap is detected without redirecting evidence outside the
configured root. Uncooperative local processes must not replace these paths,
especially after handoff; the locks are not a sandbox or protection from a
privileged or malicious host user. Refusals before exclusive artifact ownership
produce no run evidence; an owned failure is sealed in the directory actually
created.

The project mapping is the trusted local resolver seam. A future resolver may
produce the same repository, canonical base commit, validation, and Coordinator
selection without changing the Coordinator or worker contracts.

## Acceptance Evidence Preflight

Classify one Bead's requested acceptance evidence without running an
implementation agent:

```sh
python3 -m afk_preflight preflight.json /new/result-directory
```

Input is one structured JSON object:

```json
{
  "schema_version": 1,
  "source": {"kind": "bead", "id": "central-123"},
  "title": "Change the fixture",
  "acceptance_criteria": "Tests pass and the deployment responds over HTTP.",
  "evidence_catalog": [
    {
      "category": "repository_validation",
      "route": "repository validation",
      "can_prove": "Ruff and repository tests from ./scripts/validate."
    },
    {
      "category": "pipeline_evidence",
      "route": "AFK committed change and Review",
      "can_prove": "Committed implementation and Review findings."
    },
    {
      "category": "operator_external",
      "route": "operator handoff",
      "can_prove": "Deployment, live service, and HTTP behavior."
    }
  ],
  "timeout_seconds": 900
}
```

The default adapter invokes authenticated Pi with `gpt-5.6-luna`, low thinking,
JSON event mode, and no tools, context files, extensions, skills, prompt
templates, themes, or fallback model. A deterministic fixture may set
`AFK_PREFLIGHT_AGENT_COMMAND` to an exact JSON argv array; Preflight appends its
prompt as the final argument.

The classifier splits free-form acceptance criteria into ordered requests. Each
request contains bounded request text, one category, an exact catalog route or
`human clarification`, and a rationale. Allowed categories are
`repository_validation`, `pipeline_evidence`, `operator_external`,
`unsupported`, and `ambiguous`. Inference cannot authorize execution: local
contract validation derives `proceed` only when every request belongs to the
first two categories. Every other valid classification returns `pause`.

The new result directory contains accepted `input.json`, raw `events.jsonl`, raw
`stderr.log`, and an atomically sealed `output.json`. Valid `proceed` and `pause`
classifications both have `outcome: completed` and exit zero because the
standalone classification succeeded. Launch, process, event-protocol, or
structured-output failure seals a non-completed outcome with `decision: pause`
and exits `1`. Invalid invocation or input exits `2` without replacing evidence.
The component never accesses Beads, modifies a workspace, rewrites acceptance
criteria, starts Coordinator, or runs validation.

## Workflow Run Exporter

Export one sealed Run Preparer result to a new portable Publication Bundle:

```sh
/path/to/afk-pipeline/afk export /path/to/sealed-run /path/to/new-bundle
```

A direct Coordinator directory does not carry Project or Run identity, so its
caller must provide those facts explicitly:

```sh
/path/to/afk-pipeline/afk export /path/to/coordinator /path/to/new-bundle \
  --project example --run-id run-123
```

`--project`, `--run-id`, and `--bead-id` act as assertions when sealed evidence
already carries the corresponding identity. The destination parent must exist
and the destination itself must not. Source and destination may not overlap.

The exporter validates terminal Coordinator evidence, or a terminal `pause`
from Preflight before Coordinator was started. A paused export has an empty
history; it never invents Coordinator invocations. Legacy Runs without
Preflight remain exportable.

Publication Bundle v2 is the default producer output. It retains the readable
normalized Run fields and adds the Preflight request ledger plus a
semantic `artifacts` inventory. Each descriptor identifies a safe Run-relative source, scope, kind,
media type, publication state, public size and SHA-256, sanitization status, and
an explicit fixed reason when bytes are unavailable. Accepted JSON, JSONL,
UTF-8 logs, and diffs are written only as deterministic derived copies below
`artifacts/`; private source files are never rewritten. Known host paths are
redacted, as are recognized credential forms. Artifact state is `downloadable`,
`empty`, `oversized`, `unsafe`, or `unavailable`; only `downloadable` records
carry a payload path. Invalid, non-UTF-8, empty, missing, unsafe, and oversized
optional sources remain explicit without public bytes. Structured payloads and
logs are admitted before events. The allowlist covers the frozen Bead,
Assignment, Coordinator request, Preparation record, Preflight records,
Coordinator records, and each Component Invocation input, output, and declared
artifact. The limits are 25 MiB per uncompressed artifact, 32 MiB for the
complete bundle, 128 payload files, and a 64 KiB manifest. This allows useful
event streams above the old 8 MiB bundle limit.

Pass `--schema-version 1` only when a producer must emit the legacy v1
contract. It writes `manifest.json`, one `workflow-run.json`, and only
inventoried, nonempty UTF-8 `stdout`, `stderr`, and Review `diff` files. Raw Pi
`events.jsonl` is represented by digest, size, line count, and event counts but
is not copied. Its limits remain 1 MiB per included file and 8 MiB total.
Existing datastore readers continue to accept v1 bundles.

Both formats reject private-key headers, URL credentials, and common
authorization, cloud-secret, token, password, and API-key forms. Export is
deterministic for unchanged evidence. It stages beside the destination and
atomically renames the complete directory; refusal leaves no destination.
Success prints an `exported` JSON result and exits zero. Invalid or unsealed
evidence prints `invalid_run` and exits one; missing direct-Run identity exits
two. The exporter writes no datastore state, performs no network activity, and
does not invoke an agent.

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
`timeout_seconds` is the child execution deadline, not a wall-clock cap on the
wrapper. All components that run children share the same bounded shutdown: they
send SIGTERM and wait up to 2 seconds, then send SIGKILL and wait up to 2 more
seconds for the child to become waitable. Thus a timeout can take up to 4
additional seconds of shutdown grace, plus ordinary wrapper overhead. If the
child still cannot be reaped, the component records a process error and
continues sealing its `timed_out` or `interrupted` outcome rather than waiting
indefinitely.

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

## Coordinator

Create or resume one synchronous run across the seven existing modules:

```sh
python3 -m afk_coordinate run.json /new-or-existing/run-directory
```

Coordinator input is structured JSON:

```json
{
  "schema_version": 1,
  "assignment_path": "/absolute/path/to/assignment.json",
  "validation": {
    "command": ["./scripts/validate"],
    "timeout_seconds": 1800
  },
  "agent_timeout_seconds": 900,
  "max_responses": 2
}
```

On first invocation, the run directory must not exist and must be outside the
Assignment workspace. The coordinator freezes accepted input as `input.json`
and the Assignment as `assignment.json`, then invokes the existing modules in
zero-padded directories. It atomically replaces `state.json` before and after
each invocation. The checkpoint exposes run status, next sequence and module,
an optional active invocation, ordered history, and terminal facts.

The same command resumes an existing run. If an active module has sealed its
`output.json`, the coordinator consumes it and continues without repeating the
module. If no sealed output exists, the coordinator exits `1` without changing
state because it cannot infer whether the worker is alive. After an operator or
execution substrate confirms that worker is gone, run:

```sh
python3 -m afk_coordinate run.json /existing/run-directory --abandon-active
```

The abandoned invocation remains in history and the retry receives a new
numbered directory. This flag is an external liveness assertion; the
coordinator does not inspect or terminate the old process.

`stop` and `exhausted` Iteration Policy decisions atomically seal terminal
`output.json` with `outcome: completed`. A sealed non-success from any module
seals `outcome: failed`. Exit status is `0` for a completed run, `1` for a
failed run or unresolved active invocation, and `2` for invalid invocation,
input, checkpoint, or evidence. Re-invoking a terminal run is idempotent.

The coordinator does not access Beads, prepare a workspace, choose a branch,
infer worker liveness, retry component failures, publish feedback, manage
containers, or implement a scheduler or general state-machine framework.

## Check

Install the repository's Ruff commit hooks once per checkout:

```sh
pre-commit install
```

Run the complete repository check:

```sh
./scripts/validate
```
