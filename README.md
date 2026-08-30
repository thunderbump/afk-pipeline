# AFK Pipeline

Small executable modules for running agent work, validating and reviewing its
result, responding to feedback, and coordinating the accepted sequence.

## Inference Runtime

`afk_inference` exposes a semantic invocation API: callers provide a purpose,
separate trusted instructions and untrusted data, one of `NO_TOOLS`,
`READ_ONLY`, or `WRITE`, an execution root, timeout, evidence directory, and a
trusted in-process response validator. The runtime supplies the fixed system
instructions for the requested capability; callers do not provide executable
paths, argument arrays, or system prompts.

The production `PiAdapter` maps those instructions to Pi's provider system
prompt and sends one task prompt containing separate trusted instructions and
base64-encoded JSON task data. Its argv, provider, JSON protocol, session and
retry behavior, working directory handling, and disabling flags are closed
runtime policy. `NO_TOOLS` uses `--no-tools`, `READ_ONLY` allows
`read,grep,find,ls`, and `WRITE` allows `read,bash,edit,write,grep,find,ls`.
Pi's provider-managed retries remain in one process event stream and share the
invocation deadline; malformed streams fail closed and there is no adapter
fallback.

The deterministic `FixtureAdapter` remains an immutable in-process test
adapter. Its frozen script is indexed only by the one-based attempt number. It
is not a sandbox. Validators likewise run directly as trusted pipeline code;
their rejection, failure, and duration are recorded, but the runtime does not
isolate or forcibly stop them.

Each invocation retains the exact structured and rendered private prompt,
adapter contract or fixture script, per-attempt event stream, stderr and
response, and an atomically sealed `receipt.json`. The receipt is written last
and binds identities, hashes, frozen adapter/model/thinking policy, timing,
process, protocol, validation, terminal response, and outcome. Run preparation
freezes Pi adapter family `pi` and contract version `1` in every inference role
setting; continuation consumes that frozen setting rather than mutable config.

## Acceptance Planner

Route one frozen Bead directly to the existing pipeline or propose a small
child-work graph without changing Beads:

```sh
python3 -m afk_plan planner.json /new/result-directory
```

The input contains `schema_version: 1`, a frozen `parent` with `id`, `title`,
`description`, `acceptance_criteria`, and `labels`, a trusted `catalog`, and a
`timeout_seconds` value from 1 through 3600. The catalog lists allowed Project
slugs and their allowed combinations of `execution`, `evidence_route`, and
`phase`:

```json
{
  "schema_version": 1,
  "parent": {
    "id": "central-123",
    "title": "Implement and verify the change",
    "description": "One frozen parent task.",
    "acceptance_criteria": "The change is tested and the live route returns 200.",
    "labels": ["project:example"]
  },
  "catalog": {
    "schema_version": 1,
    "projects": [
      {
        "slug": "example",
        "routes": [
          {
            "owner": "Example implementation agent",
            "execution": "agent",
            "evidence_route": "pipeline_run",
            "phases": ["implementation"]
          },
          {
            "owner": "Example operations service",
            "execution": "external",
            "evidence_route": "external_check",
            "phases": ["closure"]
          }
        ]
      }
    ]
  },
  "timeout_seconds": 300
}
```

The default tool-free Pi adapter uses `gpt-5.6-luna` with low thinking. Tests or
another caller may provide `AFK_PLAN_AGENT_COMMAND` as an exact JSON argv array;
the structured planning prompt is appended as the final argument. Inference
returns exactly one `direct` or `decompose` decision with ordered criterion
coverage. It neither creates Beads nor authorizes publication.

A direct proposal assigns every criterion to the unchanged source Bead and
contains no children. Its routes may use catalog-defined ownership and evidence
so the deterministic policy can visibly reject an incompatible proposal. A
decomposed proposal assigns every criterion to one child and retains the
existing project, owner, execution, evidence, phase, handoff, and dependency
contract. The Routing Contract rejects decomposition when every proposed child
route could instead use the source Project's existing agent implementation
pipeline.

The deterministic Routing Contract requires ordered criterion source chunks whose
whitespace-normalized concatenation exactly reproduces the parent acceptance
criteria. The source must have exactly one `project:<slug>` label present in the
catalog. Every contiguous criterion id receives exactly one ordered route. The
contract binds that routing record to canonical source and catalog SHA-256
digests.

For decompose, the existing Plan Contract also requires every criterion to
belong to exactly one child. Child Projects, owners, and
execution/evidence/phase combinations must exist in the trusted catalog;
dependencies must form a DAG; and closure children must follow implementation
when implementation work exists. External work must include a handoff whose
authority matches the trusted owner, commit and/or environment subject, and an
`external_check` completion-record
type. Legacy v1 human handoffs are rejected by the current contract. The Plan
Contract still derives
`ready-for-agent`/`ready-for-human` and computes the canonical plan digest used
by the Child Graph Publisher.

A valid result contains the canonical `routing` record. Direct results set
`plan: null`; decomposed results also contain the unchanged canonical Plan. A
valid routing exits zero. Process, event-protocol, or invalid-proposal outcomes
seal a failed, timed-out, or interrupted output with neither routing nor Plan
and exit `1`. Invalid invocation or input and an existing destination exit `2`
without replacing evidence. The new result directory contains accepted
`input.json`, raw `events.jsonl`, raw `stderr.log`, and atomically sealed
`output.json`. The Planner does not read or write Beads, prepare worktrees,
publish children, or validate completion.

## Acceptance Routing Policy

Accept pipeline-compatible direct routing or one unambiguous canonical Plan
without writing Beads:

```sh
python3 -m afk_plan_accept acceptance.json /new/result-directory
```

Input contains exactly `schema_version: 1`, the original validated
`planner_input`, and either its canonical `routing` for direct work or canonical
`plan` for decomposed work. The pure policy reruns the corresponding contract.

A direct record is accepted only when every route targets the unchanged source
Bead in its source Project, uses agent execution in the implementation phase,
and requests `pipeline_run` or `repository_check` evidence. External,
cross-project, closure-phase, or ambiguous valid direct records seal
`decision: needs_human` and cannot enter the pipeline. Accepted direct output
uses policy `pipeline-compatible-direct-v1`, contains no Plan, and has no Child
Graph Publisher authority.

The decomposed path retains the existing behavior. It accepts any Plan with
`status: proposed`, an empty ambiguity list, and contract-derived
`authorization: null`. The policy does not judge whether one valid split is
better than another. Split quality remains a Planner prompt concern.

An accepted decomposed record wraps the unchanged Plan and repeats its parent,
catalog, and Plan identities. It keeps policy `contract-valid-proposed-v1` and
`basis: structural_validity_only`, then computes the same canonical acceptance
digest used by the Child Graph Publisher. The component does not itself create
children or start work. An unaccepted routing or Plan exits `1`. Malformed or
tampered evidence exits `2` before creating a result. Accepted input is copied
to `input.json`, and `output.json` is sealed last. The component performs no
inference, process launch, Git work, or Beads access.

## Child Graph Publisher

Publish one immutable accepted plan as central Beads children and dependency
relationships:

```sh
python3 -m afk_plan_publish publish.json /new/result-directory
```

`publish.json` contains exactly:

```json
{
  "schema_version": 1,
  "acceptance_directory": "/absolute/accepted-plan-result",
  "beads_workspace": "/absolute/central-beads-workspace",
  "command": ["bd"],
  "timeout_seconds": 30
}
```

The command inherits its environment. Authentication belongs there or in a
caller-owned command wrapper; do not put secrets in the argv because accepted
input and command transcripts are durable evidence. The Acceptance evidence,
Beads workspace, and new result directory must be physically separate.

Before mutation, the publisher revalidates the complete Acceptance Plan Policy
record and requires the current parent Bead's frozen title, description,
acceptance criteria, and labels. It identifies children with stable
`afk-plan:<plan_sha256>:<local_id>` external references. Missing children are
created with exact project/readiness labels and a parent relationship; existing
children must still match the accepted plan. Planned `depends_on` edges become
Beads `blocks` relationships.

External children receive a fixed completion handoff section after their actual
Bead ID is known. It names the parent, plan digest, child, criteria, expected
authority, required subject fields, and Completion Record shape. Inference never
receives Beads write authority.

Each invocation uses a new result directory. Success seals `decision: published`;
an exact replay seals `decision: replayed` without duplicate
children or relationships. An operational failure after publication begins
seals `outcome: failed`, the known partial mapping, and command transcripts;
rerunning with a new result directory reconciles through the stable external
references. Malformed, tampered, unaccepted, or stale parent evidence exits `2`
before Beads mutation. The publisher does not start child work, validate child
completion, close children, or accept the parent.

## Child Completion Record Validator

Validate one scoped Completion Record against its immutable accepted Plan and
Child Graph Publisher result without mutating Beads or external state:

```sh
python3 -m afk_complete completion.json /new/result-directory
```

`completion.json` contains exactly:

```json
{
  "schema_version": 1,
  "acceptance_directory": "/absolute/accepted-plan-result",
  "publication_directory": "/absolute/child-publication-result",
  "expected_subject": {
    "commit": "<expected commit>",
    "environment": "<expected environment>"
  },
  "record": {
    "schema_version": 1,
    "child": "central-child-id",
    "parent_plan": "<canonical plan digest>",
    "outcome": "satisfied",
    "producer": {
      "kind": "external_check",
      "identity": "Deployment verifier"
    },
    "criteria": ["criterion-2"],
    "subject": {
      "commit": "<expected commit>",
      "environment": "<expected environment>"
    },
    "evidence": ["<bounded evidence reference>"],
    "accepted_at": "<UTC timestamp>"
  }
}
```

The deterministic validator revalidates the complete Acceptance Policy record,
the successful Publisher envelope, the exact planned-child mapping, criterion
coverage, producer identity and kind, required subject fields, caller-frozen
subject values, evidence references, and UTC timestamp. Producer kinds are
`pipeline_run`, `repository_check`, and `external_check`. The producer kind must
match the child's accepted evidence route. Every valid
record requires `outcome: satisfied` and seals `decision: satisfied` with
`satisfies_criteria: true`.
Changing the child, Plan digest, criteria, authority, or expected
commit/environment fails before result creation. A valid result contains
`input.json` and atomically sealed `output.json`. The module performs no
inference, evidence retrieval, Beads access, work execution, child closure, or
parent acceptance review.

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
  "acceptance_routing": {
    "timeout_seconds": 300,
    "catalog": {
      "schema_version": 2,
      "projects": [
        {
          "slug": "example",
          "routes": [
            {
              "owner": "AFK Run",
              "executor": "afk_run",
              "evidence_route": "pipeline_run",
              "phases": ["implementation"]
            },
            {
              "owner": "Caller agent",
              "executor": "caller_agent",
              "evidence_route": "external_check",
              "phases": ["implementation", "closure"]
            },
            {
              "owner": "Credential holder",
              "executor": "outside_help",
              "outside_help_reason": "missing_credentials",
              "evidence_route": "external_check",
              "phases": ["implementation", "closure"]
            }
          ]
        }
      ]
    }
  },
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

`acceptance_routing` freezes the complete trusted v2 capability catalog and the
Planner timeout. The catalog must include the source Project and may include
other Projects used by decomposed work. An `outside_help` route means the agent
system lacks the named capability and always uses `external_check` evidence of
the work performed outside that system; it is not an attestation route. The Run
Preparer retains the exact
Planner input/output and deterministic Policy input/output in the Run root.
`acceptance_routing` is the only Run admission configuration. Run Preparer
rejects the retired `classification_store` field. The retired `attestation`
section is also rejected with instructions to use capability-based
`outside_help`. Historical v1 Run and
Preflight evidence remains readable through the exporter and Operations WebUI.

Inference roles can be selected without rebuilding adapter commands. For example,
`"inference_roles": {"review": {"model": "gpt-5.6-terra", "thinking": "low"}}`
changes only Review; omitted roles and fields keep their defaults. The preparer
freezes all four effective settings in Run evidence. Exact-argv `AFK_*_AGENT_COMMAND`
environment overrides still take precedence, and `assignment.command` remains the
implementation-worker seam.

The trusted host process runs `bd show <bead-id> --json` only in the configured
central Beads workspace. Run admission requires the Bead to have exactly one
`ready-for-agent` label and no `ready-for-human` label. Missing, duplicate, or
conflicting readiness labels exit `2` with instructions to update triage; this
check occurs before project ownership resolution, repository inspection, Git
mutation, worktree creation, or durable Run artifacts. There is no readiness
bypass flag. This is the readiness gate for Runs later exposed through the
current Operations WebUI publication interface.

For capability routing, Acceptance Planner runs after the isolated worktree is
prepared. An accepted direct `afk_run` route starts Coordinator immediately;
Acceptance Evidence Preflight is not invoked or written. An accepted child Plan,
`outside_help`, or `needs_clarification` seals its exact routing evidence and
returns before Coordinator. Planner, policy, protocol, launch, and interruption
failures also stop before Coordinator and seal a failed preparation.

After readiness admission, the preparer requires exactly one `project:<slug>`
label. It resolves that project locally, freezes the base ref to a commit, and
creates a new flat `afk-<bead-id>-<run-id>` branch and isolated worktree. The
flat name cannot conflict with a bootstrap branch such as `afk/<bead-id>`, and
preparation preflights exact, ancestor, and descendant branch-ref namespace
collisions. It never fetches, clones, reuses, or replaces a destination. Beads
connection settings remain in the preparer environment and are not forwarded to
Coordinator workers or written to durable evidence.

The assignment command must contain exactly one argv element equal to
`{assignment_path}`. During preparation that element is replaced, without a
shell, by the generated absolute `assignment.json` path. Embedded or repeated
placeholders, commands without the placeholder, and commands that use the
Assignment path as their executable are rejected before any run destination is
created. This lets a configurable worker read the frozen Bead objective while
keeping Beads lookup in the trusted preparer.

Each project validation mapping also retains bounded `evidence` text describing
what its exact repository-owned command proves. Acceptance Routing uses only the
configured v2 catalog; the Bead cannot add an executor, owner, evidence route,
or outside-help reason.

Every accepted preparation has a unique `<run_root>/<bead-id>/<run-id>/`
artifact root. It contains value-safe `bead.json`, `assignment.json`,
`coordinator-request.json`, versioned `preparation.json`, a deterministic
`related-work.jsonl`, and a reserved `coordinator/` directory. The bounded
related-work snapshot contains only safe planning fields for the subject, its
parent and siblings, direct blockers and dependents, and short ancestor
breadcrumbs. Its count, byte size, SHA-256 digest, and media type are bound into
Preparation, Assignment, and Coordinator evidence; exceeding either limit
refuses preparation rather than publishing partial context. Implementer, Review,
and Finding Assessment receive the same path and may query it with `jq` or `rg`
only for scope or ownership orientation. The Assignment remains authoritative,
related prose is data rather than instructions, and snapshot content is not
inserted into prompts. Continuations revalidate and reuse the frozen reference.
Runs also contain `planner-input.json`,
`policy-input.json`, and complete `planner/` and `policy/` results. Run Preparer
fails closed before Coordinator when that admission evidence is incomplete or
malformed. Historical Runs may retain `preflight-input.json` and `preflight/`,
but new Runs never create them.

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
preparer exports the Run, invokes the publication command exactly once, and
removes the temporary bundle. Publication exposes the validated related-work
JSONL as an unchanged `application/x-ndjson` artifact with the same digest, so it
is viewable and downloadable without another content transform. The preparer
writes raw adapter streams to
`publication.stdout` and `publication.stderr`, then atomically seals
`publication.json` last. A valid
`accepted` or `replayed` result records publication success. Conflict,
rejection, timeout, launch, malformed adapter output, temporary-storage failure,
or export failure records categorized evidence without changing the Run's
terminal facts. A publication failure changes an otherwise successful `afk run`
exit to `1`; an already-unsuccessful Coordinator retains its existing exit
behavior. Without `publication`, the command behaves exactly as before and
creates no publication artifacts. The standalone exporter still accepts
validated historical Runs that ended in a completed Preflight pause, preserving
their empty Coordinator history without making that a new-Run production path.

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

## Historical Acceptance Evidence Preflight

The standalone v1 producer remains available for retained fixtures and evidence
compatibility. Run Preparer no longer invokes it. To reproduce a historical
classification outside a Run:

```sh
python3 -m afk_preflight preflight.json /new/result-directory \
  --classification-store /absolute/caller-owned/path/to/classifications
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
The prompt prefers a supplied repository or pipeline route when that route can
prove evidence produced by the requested implementation; absence before work
does not by itself make that evidence unsupported. A classification contains
at most 256 requests.

The caller-owned Classification Store must be an absolute path and must not
overlap the new result directory. A per-key lock serializes concurrent first
calls. Preflight atomically creates the first valid record and never replaces an
existing record. A malformed existing record or unavailable store fails closed;
Preflight does not invoke inference to repair or overwrite it. Lock files remain
as small store-owned coordination records. Interruption while waiting for a
lock seals an interrupted pause. A failed, timed-out, or interrupted classifier
never publishes a reusable record. Retention is outside this component.

The new result directory contains accepted `input.json`, raw `events.jsonl`, raw
`stderr.log`, and an atomically sealed `output.json`. `classifier.source` says
`inferred`, `reused`, or `unavailable` and includes the key, complete policy
identity, and safe record name when a record exists. A reused result has no
agent or process claim and empty raw streams. Valid `proceed` and `pause`
classifications both have
`outcome: completed` and exit zero because the standalone classification
succeeded. Launch, process, event-protocol, structured-output, stored-record, or
store failure seals a non-completed outcome with `decision: pause` and exits
`1`. Invalid invocation, input, or overlapping paths exit `2` without replacing
evidence. The component never accesses Beads, modifies a workspace, rewrites
acceptance criteria, starts Coordinator, or runs validation.

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

The exporter validates terminal Coordinator evidence, a terminal `pause`
from Preflight, or sealed Acceptance Routing that intentionally stopped before
Coordinator. A paused or routing-only export has an empty history; it never
invents Coordinator invocations. Legacy Runs without Preflight remain
exportable.

Publication Bundle v2 is the default producer output. It retains the readable
normalized Run fields and adds the Preflight request ledger, a bounded
`acceptance_routing` stage, and a semantic `artifacts` inventory. The routing
stage records Planner outcome, Policy outcome and decision, a direct-route or
child-route summary, and the exact contract reason for outside help or
clarification. Typed `planner` and `policy` artifacts retain only the validated
output envelopes; model event streams and policy inputs are deliberately not
published. Each descriptor identifies a safe Run-relative source, scope, kind,
media type, publication state, public size and SHA-256, sanitization status, and
an explicit fixed reason when bytes are unavailable. Accepted JSON, JSONL,
UTF-8 logs, and diffs are written only as deterministic derived copies below
`artifacts/`; private source files are never rewritten. Known host paths are
redacted, as are recognized credential forms. In a schema-validated Preflight
output, the classifier `key` field alone is replaced with an explicit public
marker; the remaining Preflight fields and the private source record are
preserved. Artifact state is `downloadable`,
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

The event interpreter also accepts up to three Pi model auto-retries. Each
intermediate `agent_end` must set `willRetry: true` and be followed immediately
by a valid `auto_retry_start` and a new `agent_start`. A successful final segment
includes its matching `auto_retry_end`, ends with `willRetry: false`, and settles
once. Intermediate assistant errors never replace the final response text.
Malformed seams, incomplete retries, and events after the final settlement fail
closed.

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
and an atomically sealed `output.json`. Before responding, the reviewer is
instructed to inspect the objective, its stated acceptance criteria, the complete
reviewed diff, and all supplied evidence, then return every actionable finding
discovered in that audit together. This applies equally to code and documentation
work. A completed response contains a summary, a possibly empty findings array,
and exactly this ordered audit declaration:

```json
{"completed":true,"scopes":["objective","acceptance_criteria","reviewed_diff","supplied_evidence"]}
```

The declaration records that the reviewer performed those inspection scopes; it
is not mechanical proof that every possible defect was found. It neither maps
findings to scopes nor assigns identities to acceptance criteria that the input
does not provide. Missing, extra, reordered, or malformed audit fields and values
invalidate the Review and its downstream use. Every finding requires severity,
title, details, and at least one repository-relative path with a positive 1-based
line number that exists in a text file under the reviewed `HEAD`. Findings do not
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

Coordinator may instead supply `validation_directory`, the originating
Attempt/Feedback Response `source`, and the frozen `objective`. This validation
repair form accepts only an ordinary positive nonzero command exit with sealed
input/output, regular `stdout.log` and `stderr.log`, identical clean repository
observations, and a workspace still at that state. It rejects malformed or
missing evidence, launch and observation errors, timeouts, interruptions,
signals, dirty results, and repository drift before creating a result. Its
prompt identifies all four Validation artifacts and explicitly distinguishes a
validation repair from an accepted Review finding; its structured result uses
an empty `finding_responses` array.

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
state because it cannot infer whether the worker is alive. An ordinary nonzero
Validation with stable, clean, inspectable repository evidence may consume one
remaining Feedback Response allowance. That repair receives the failed
Validation input, output, stdout, and stderr, then returns through Validation,
Committed Change, and Review in normal order. Every completed repair counts
against `max_responses`; a repeated failure with no allowance is terminal.
Malformed or missing evidence, timeout, interruption, launch or observation
error, signals, dirty output, and repository drift remain terminal and never
allocate repair.

After an operator or execution substrate confirms that worker is gone, run:

```sh
python3 -m afk_coordinate run.json /existing/run-directory --abandon-active
```

The abandoned invocation remains in history and the retry receives a new
numbered directory. This flag is an external liveness assertion; the
coordinator does not inspect or terminate the old process.

An exhausted run can receive a new, additive Feedback Response allowance:

```sh
python3 -m afk_coordinate run.json /existing/run-directory \
  --continue-exhausted ADDITIONAL_RESPONSES
```

`ADDITIONAL_RESPONSES` must be a positive integer. The coordinator starts with
the terminal Finding Assessment, does not repeat Attempt, and keeps the
original `state.json` and `output.json` unchanged. Each continuation records its
accepted allowance, effective limit, checkpoint, and terminal output under
`continuations/NN/`. Repeating the command with the same allowance resumes an
active continuation. A continuation that exhausts its allowance can receive a
later additive allowance in the next numbered directory.

For a prepared Run with configured Publication Bundle admission, use the
repository-owned continuation and publication entry point:

```sh
/path/to/afk-pipeline/afk continue /absolute/path/to/sealed-run \
  ADDITIONAL_RESPONSES --config /absolute/path/to/config.json
```

This validates the original terminal and every numbered continuation before
executing work and leaves the original `state.json` and `output.json`
byte-for-byte unchanged. It publishes every retained sealed continuation oldest
to newest through the same private v2 Publication Bundle and fail-closed
Admission seam as `afk run`, stopping at the first failed or rejected identity.
Publication streams and `publication.json` are retained in each attempted
continuation directory. The bundle Run ID appends `.continuation.NN`, giving
each terminal continuation an immutable Admission identity while preserving
deterministic replay. Already admitted predecessors can report `replayed`
without being changed. A publication failure remains unsuccessful without
changing Coordinator facts. Repeating the command after a clean stop replays
the complete retained lineage and returns the newest terminal result; an
exhausted newest continuation instead receives the next additive allowance.
`--abandon-active` carries the same explicit liveness assertion as Coordinator.

If an active continuation worker is confirmed gone before sealing output, the
caller can apply the same liveness assertion used by an original run:

```sh
python3 -m afk_coordinate run.json /existing/run-directory \
  --continue-exhausted ADDITIONAL_RESPONSES --abandon-active
```

The accepted allowance must match the active continuation. Coordinator retains
the orphaned invocation as `abandoned` and allocates a new numbered retry.

Only a clean exhausted run can continue. Stopped and failed runs are immutable,
and continuation refuses if the workspace no longer matches the repository
state captured by the terminal Finding Assessment. This keeps manual repairs or
other external commits from being silently folded into old review evidence.

`stop` and `exhausted` Iteration Policy decisions atomically seal terminal
`output.json` with `outcome: completed`. A sealed non-success from any module
seals `outcome: failed`. Exit status is `0` for a completed run, `1` for a
failed run or unresolved active invocation, and `2` for invalid invocation,
input, checkpoint, or evidence. Re-invoking a terminal run is idempotent.

The coordinator does not access Beads, prepare a workspace, choose a branch,
infer worker liveness, retry component failures, publish feedback, manage
containers, or implement a scheduler or general state-machine framework.

## Parent Acceptance Review

Judge whether a completed child graph collectively satisfies its accepted
parent Plan:

```sh
python3 -m afk_parent_review review.json /new/result-directory
```

The structured input names the immutable accepted-Plan and Child Graph
Publisher result directories, a caller-frozen child graph, exactly one sealed
Completion Validator result per planned child, a current caller-frozen subject,
typed terminal evidence, and a timeout. Every child must be closed. Its
`parent-child` and `blocks` relationships must exactly match the accepted Plan.
Completion results must cover every published child exactly once and retain the
accepted Plan, child, criteria, producer, current subject, evidence references,
and satisfaction state.

Pipeline terminal evidence is a prepared Run that must belong to the published
child, end at Coordinator `completed/stop`, contain a valid Committed Change,
and match the current commit subject. Repository-check evidence must be a passed,
unchanged-head Validation result at that commit. `external_check` remains visibly
typed as Completion Record evidence instead of being relabeled as deterministic
verification. Completion timestamps cannot predate its terminal evidence.

These deterministic checks finish before result creation or inference. The
default adapter then invokes Pi with `gpt-5.6-luna`, low thinking, and no tools.
`AFK_PARENT_REVIEW_AGENT_COMMAND` may contain a JSON argv override for testing
or deployment. The model sees only the verified fan-in summary; it does not
read repositories, retrieve evidence, mutate Beads, or run work.

The immutable result directory contains `input.json`, the deterministic
`fan-in.json`, raw `events.jsonl`, raw `stderr.log`, and an atomically sealed
`output.json`. A completed result decides `accepted` or `incomplete`, gives one
decision per canonical parent criterion, and lists exactly one gap per
incomplete criterion. An incomplete result also proposes one advisory follow-up
child using the existing Plan child shape: project, owner, phase, execution,
evidence route, dependencies, and handoff are checked against the trusted
catalog before sealing. That proposal has no publication or work authority.

Exit status is `0` only for an accepted parent, `1` for incomplete or sealed
agent failure, and `2` for invalid invocation, configuration, or evidence. A
new evidence set produces a new attempt; existing evidence and attempts are
never rewritten.

## Check

Install the repository's Ruff commit hooks once per checkout:

```sh
pre-commit install
```

Run the complete repository check:

```sh
./scripts/validate
```
