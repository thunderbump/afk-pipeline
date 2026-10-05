# AFK Pipeline

Independent commands create a PR from a central Bead, validate and review its
result, respond to feedback, and explicitly finish accepted work. An optional
orchestrator calls those commands in sequence.

## Supported commands

The public `afk` launcher supports current PR commands `pr`, `evaluate`,
`review`, `respond`, `status`, `context`, `finish`, `assess`, `job`, `cleanup`,
`gc`, and optional `orchestrate`. Run `afk --help` or read
[Explicit PR passes](#explicit-pr-passes) for current usage. These commands
load current Beads and publication helpers without the old dispatcher or
standalone stage graph.

Standalone `run`, `continue`, `export` and stage commands have been removed.
Current host configuration is TOML; saved PR jobs remain available through its
`state_root`.

## Retained records

The read-only [afk_records](afk_records/) package authenticates existing Run
receipts for optional metrics reports and publication. It retains the structural
validators, continuation traversal, bounded no-follow reads, Review receipt and
context checks, and public-text sanitization those consumers require. Recorded
paths cannot expand caller authority, and repeated reads reject replacement or
metadata changes. It provides no stage entrypoints, exporter command, general
schema translation or migration writer.

Historical receipt schemas describe retained data rather than current execution.
Stored fixture bytes stay frozen. The bounded
[test-only bundle writer](tests/retained_bundle_fixture.py) constructs synthetic
reader fixtures; it is not a supported exporter.

## Inference Runtime

`afk_inference` exposes a semantic invocation API: callers provide a purpose,
separate trusted instructions and untrusted data, one of `NO_TOOLS`,
`READ_ONLY`, or `WRITE`, an execution root, timeout, evidence directory, and a
trusted in-process response validator. READ_ONLY and WRITE callers may additionally
supply `read_only_evidence`, at most sixteen absolute regular-file paths. The
runtime derives and records their read authority in its system instructions;
paths in untrusted task data cannot enlarge it. WRITE callers may read these
external evidence files without modifying them. NO_TOOLS cannot request file
reads. This is adapter/tool policy, not an operating-system sandbox.
The runtime supplies the system
instructions for the requested capability; callers do not provide executable
paths, argument arrays, or system prompts.

The production `PiAdapter` maps those instructions to Pi's provider system
prompt and renders one task prompt containing separate trusted instructions and
base64-encoded JSON task data. The runtime retains that prompt as a non-writable
`task-prompt.txt`, authenticates its content and file identity, and gives Pi an
`@/proc/self/fd/N` input backed by an inherited descriptor. Unbounded task data
therefore never enters argv, pathname replacement cannot change Pi's input, and
missing, replaced, writable, or unreadable prompt artifacts fail closed. Its
argv, provider, JSON protocol, session and retry behavior, working directory
handling, and disabling flags are closed runtime policy. `NO_TOOLS` uses
`--no-tools`, `READ_ONLY` allows
`read,grep,find,ls`, and `WRITE` allows `read,bash,edit,write,grep,find,ls`.
Pi's provider-managed retries remain in one process event stream and share the
invocation deadline; malformed streams fail closed and there is no adapter
fallback.

The deterministic `FixtureAdapter` remains an immutable in-process test
adapter. Its frozen script is indexed only by the one-based attempt number. It
is not a sandbox. Validators likewise run directly as trusted pipeline code;
their rejection, failure, and duration are recorded, but the runtime does not
isolate or forcibly stop them.

Each invocation retains the exact structured and rendered private prompt, the
Pi task-prompt artifact (when applicable), adapter contract or fixture script,
per-attempt event stream, stderr and response, and an atomically sealed
`receipt.json`. The receipt is written last
and binds identities, hashes, frozen adapter/model/thinking policy, timing,
process, protocol, validation, terminal response, and outcome. Production calls
select model/thinking policy by purpose in [the inference runtime](afk_inference/runtime.py).
Each invocation records the selected Pi adapter family `pi` and contract version
`1`; host TOML does not override that policy.

Implementation (`attempt`), review and feedback response use `gpt-6.1-sol` with
medium reasoning. Finding assessment keeps `gpt-5.6-sol` medium; completion
assessment, Bead evaluation, acceptance planning and parent acceptance review
keep `gpt-5.6-luna` low. Pi must resolve the exact selected model through
`openai-codex`; an unavailable model fails the invocation without a fallback.

## Optional retained-Run metrics

Generate a read-only local report from one or more retained prepared Runs:

```sh
python3 -m afk_metrics --destination /new/report-directory \
  /path/to/run-a /path/to/run-b
cat /new/report-directory/comparison.txt
jq . /new/report-directory/summary.json
```

This command is explicitly opt-in. The destination must not already exist and
must not equal or be nested beneath any source Run. It writes `summary.json`
(schema version 2) and `comparison.txt`; it does not mutate, seal, publish, or change the status
of source evidence. Replaying the same sealed inputs produces the same summary
(the report deliberately has no generation timestamp). Repeated source inputs
and shared continuation evidence are deduplicated by stable Run and invocation
identities. An integrity failure is retained as an `invalid` source with no
trusted inference totals, causes exit status 1, and never includes source paths
or source content in the human report. Existing destinations and invalid CLI
usage exit 2.

The projection uses the retained readers in [afk_records/source.py](afk_records/source.py)
and [afk_records/continuation.py](afk_records/continuation.py), including sealed invocation evidence retained by a
component later marked abandoned. Pi Inference Receipts and their hash-bound
event streams are authenticated by the retained reader. JSONL is
streamed one record at a time with an 8 MiB encoded per-record limit, including
the newline. Pi records can contain large message/tool content and cumulative
snapshots; the limit bounds parser allocation independently of execution and
retained artifact limits. Oversized records fail source integrity without trusted
totals; no events are skipped to claim complete coverage. Local report JSON,
human output and publication errors expose the offending line and byte limit
without event content or host paths. This optional metrics limit does not
constrain worker execution. Digest authentication still covers the whole stream, and reports contain no prompts, message text, tool payloads,
logs, credentials, or raw events. Finalized assistant `message_end` usage is counted
once by its stable message identity. Cumulative `message_update`, `turn_end`, and
`agent_end` copies are not summed. `compaction_end.result.usage` is shown as a
separate aggregate because it may represent several requests. Input, output,
cache-read, cache-write, total-token, and optional reasoning categories are
preserved; reasoning is not added to `totalTokens`. Retries, failed responses,
or compactions that prevent exact request accounting mark coverage partial.
Unsupported adapters and absent usage are unavailable and do not impose a new
adapter capability. Schema v2 also reports `inference.evidence_coverage`, deriving
expected inference stages from authenticated preparation and selected Coordinator
history rather than discovered receipts. It counts sealed receipts even when they
lack usage/cost, identifies missing or unsealed receipts by exact stage ownership,
and treats zero expected stages as complete. Missing expected evidence downgrades
Run usage and cost independently without estimating absent values.

`usage.cost` is labeled a **Pi-reported estimate from model rates**, not a billed
charge. Pi model-rate amounts are denominated in USD and represent API-equivalent
cost for comparison, including subscription-backed executions. The retained
event amount is frozen: no current rates are fetched and no historical estimate
is recomputed. Pi version and historical price-table date remain unknown.
Missing cost, or zero cost with nonzero/incomplete token usage, is unavailable;
explicit zero cost with complete zero usage remains a measured zero.

Timing distinguishes Run wall span, preparation, inference invocation elapsed,
trusted in-process response-validator time, and repository Validation time.
Invocation elapsed includes adapter, Pi runtime, tools, and related process work
and is not pure model latency. Nested response validation is not added to the
repository Validation total. The machine report qualifies both validator totals
with adjacent `response_validator_coverage` and
`repository_validation_coverage` values (`complete`, `partial`, or
`unavailable`), so a sum of only the retained measurements is never presented
as complete. Change and Iteration currently have no structured durations and are
explicitly unavailable. Continuation wait gaps and
publication are likewise unavailable when no timestamps establish them.
Unattributed time is only produced when known non-overlapping intervals fit in
the Run span; nested durations are not subtracted twice.

Pairwise comparison checks frozen objective digest, base commit, and Validation
conditions. Differences are warnings and suppress a ranking rather than
presenting a confounded result. The report carries terminal outcome,
Validation outcomes, repair/retry counts, and acceptance/integration fields when
available; it does not infer semantic acceptance from completion. These
observations do not prove output quality or lower code complexity.

Event interpretation follows Pi 0.84.2, upstream commit
[`914cf1472e715297caa30db4b9535d534a9eb718`](https://github.com/earendil-works/pi/tree/914cf1472e715297caa30db4b9535d534a9eb718),
notably
[`json-event.ts`](https://github.com/earendil-works/pi/blob/914cf1472e715297caa30db4b9535d534a9eb718/packages/coding-agent/src/modes/json-event.ts),
[`print-mode.ts`](https://github.com/earendil-works/pi/blob/914cf1472e715297caa30db4b9535d534a9eb718/packages/coding-agent/src/modes/print-mode.ts),
[`models.ts`](https://github.com/earendil-works/pi/blob/914cf1472e715297caa30db4b9535d534a9eb718/packages/ai/src/models.ts), and
[`compaction.ts`](https://github.com/earendil-works/pi/blob/914cf1472e715297caa30db4b9535d534a9eb718/packages/coding-agent/src/core/compaction/compaction.ts).
There is no telemetry, instrumentation, pricing fetch, model router, UI, or
Copilot-specific behavior in this report path.

Metrics do not infer completion acceptance or Git integration from datastore
publication. Those fields remain unavailable until independently supported
completion and integration evidence is consumed.

For an existing portable bundle and its matching retained source,
`python3 -m afk_metrics publish INPUT_JSON DESTINATION_JSON` produces a bound
metrics publication. It does not create the bundle. See the
[publication request and schema](tests/fixtures/metrics-publication/README.md)
for selection, binding, output and failure behavior.

## Check

Install the repository's Ruff commit hooks once per checkout:

```sh
pre-commit install
```

Run the complete repository check:

```sh
./scripts/validate
```

The pinned Ruff hooks check style without modifying tracked files. Formatting
errors fail validation and can enter the orchestrator's ordinary bounded repair
path. Validation that changes the candidate still requires operator attention.
To apply formatting before committing, use `ruff format .` with Ruff 0.16.0.

## Explicit PR passes

After one-time host setup, normal calls need only the task or PR:

```sh
./afk pr CENTRAL_BEAD_ID
./afk review https://github.com/OWNER/REPO/pull/123
./afk respond https://github.com/OWNER/REPO/pull/123
./afk context https://github.com/OWNER/REPO/pull/123
./afk status https://github.com/OWNER/REPO/pull/123
./afk review https://github.com/OWNER/REPO/pull/123 --fixtures-only
./afk review https://github.com/OWNER/REPO/pull/123 --retry-publication JOB_ID
./afk cleanup JOB_ID --dry-run
```

These commands run independently of the optional orchestrator. Python 3.11+
provides the TOML parser. The host needs Git, authenticated `gh`, central `bd`
for creation, the existing Pi/runtime dependencies for inference, a Git commit
identity, and Linux user systemd. Background workers use the host user's stored
authentication, not credentials exported only in the initiating shell. GitHub
HTTPS acquisition uses a command-scoped `gh auth git-credential` helper.

### Optional supervised orchestration

The independent commands also have an optional caller:

```sh
./afk orchestrate start CENTRAL_BEAD_ID
./afk orchestrate status RUN_ID
./afk job JOB_ID
```

`start` returns a run ID and starts a user systemd worker. It creates the PR,
waits for matching fixtures, reviews, and responds to structured findings or
completed candidate validation failures. Published fixture exit 1 uses the same
repair budget only when the job's `repairable_exit_codes` includes 1. Exit 2,
other exits, direct signals, stale or missing evidence, unpublished results,
timeouts, interruptions and uncertain execution pause. The response reads the
existing PR diagnostics. It stops at `ready_for_merge` only after a
clean review and passing fixtures. It allows at most five responses, configurable downward with
`--max-repairs 0..5`. Merge and Bead closure remain explicit `finish` operations.
No command reads orchestration state or requires this caller.

There is one run per Bead under the configured `run_root/orchestrations`.
Repeated `start` returns that run without restarting it. After inspecting a pause,
use independent commands to resolve the problem and then resume:

```sh
./afk orchestrate resume RUN_ID
# Explicitly choose the PR's current head after manual intervention:
./afk orchestrate resume RUN_ID --review-current-head
```

Ordinary resume retains the selected job and submission identity. Current-head
resume starts a fresh review and keeps the repair count. A running worker locks
its run; stop `afk-orchestrate-RUN_ID.service` with `systemctl --user stop` before
changing it. Stopping the driver does not stop detached PR jobs. Inspect them
before resuming. A ready result records the observed revision; it is not a
permanent statement about a changing PR.

For explicit scheduling or a bounded test, `start --no-start` saves state without
launching a worker, and `orchestrate step RUN_ID` performs one transition. All
orchestration operations accept `--config PATH`. Keep the configured run root and
configuration path available across restarts. No inference runs in the driver;
its worker polls independent commands every 30 seconds while jobs are pending.

### Configuration ownership

The host file is `$XDG_CONFIG_HOME/afk/config.toml`, or
`~/.config/afk/config.toml`. Copy and customize
[the host example](examples/pr-config/host.toml). `--config PATH` selects an
alternate host file for deliberate experiments; ordinary calls do not need it.

```toml
schema_version = 1
beads_workspace = "~/Projects/beads"

[projects.operations-webui]
repository = "https://github.com/thunderbump/operations-webui.git"
```

Project labels resolve to registered Git URLs, never pre-existing checkouts.
Unknown/duplicate repositories are errors. Beads supplies the title, description,
design, acceptance criteria and exactly one `project:<slug>` label. Only creation
needs the Beads workspace. The default password-file convention is
`<beads_workspace>/secrets/dolt_beads_password.txt`; optional
`[beads] password_file` changes that path. Its contents are passed only to `bd`.

State defaults to `$XDG_STATE_HOME/afk` or `~/.local/state/afk`.
`state_root` overrides it; `workspace_root` optionally places clones on another
disk. Jobs live under `state_root/pr-reviews/<id>`, while independent phase clones
live under `workspace_root/<id>/<phase>`, defaulting to `state_root/workspaces`.
Shared fixture resources must live outside disposable workspaces.

[Pipeline defaults](afk_pr/defaults.toml) set agent timeout to 1800 seconds,
fixture timeout to 900 and acquisition timeout to 600 per Git operation. Host
`agent_timeout_seconds` and `acquisition_timeout_seconds` can override their
values. Model/thinking choices remain in the existing inference runtime.
No acceptance-routing, assignment, coordinator response limit, publication-bundle
or separate legacy worktree-root setting is required for PR commands.

Commit a root `afk.toml` to each repository's trusted base:

```toml
schema_version = 1

[fixtures]
command = ["./scripts/validate"]
github_auth = true
```

`description`, `timeout_seconds`, `termination_grace_seconds` and
`repairable_exit_codes` are optional fixture settings. Grace defaults to 60 seconds
and accepts integers from 1 through 3600. The repair set defaults to `[1]`; `[]`
disables validation repairs. Repair codes must be unique integers from 1 through
255, but only candidate exit 1 can repair in the current PR supervisor. Listing
infrastructure exit 2 or another exit cannot override that classification. See the
[Operations](examples/pr-config/operations-afk.toml) and
[EQEmu](examples/pr-config/eqemu-afk.toml) examples. Without a command, a committed
executable `scripts/validate` is the only fallback. Missing or malformed fixture
policy is an error. `github_auth=true` resolves a token with `gh auth token` inside
the fixture child and exports GITHUB_TOKEN without persisting its value.

Creation reads policy at the GitHub default branch's captured SHA. Optional
`base_branch` chooses another creation branch once; policy still comes from that
captured default-branch commit. Review/respond read policy at the exact PR base
SHA. The candidate cannot choose its own fixture policy. Resolved argv, timeout,
cleanup grace, repair set, auth requirement and provenance are saved in the job,
and fixture children inherit
them. The managed fixture service reserves both slot wait and command timeout,
the ten possible acquisition command bounds, the four Git identity/status check
bounds, configured cancellation grace, process reap and the existing 900-second
publication margin. Its systemd stop allowance also includes configured grace,
reap and publication time. A managed service stop can terminate the Python worker
before it publishes a phase record; retained diagnostics then require inspection
and unpublished evidence cannot repair. A temporary `[projects.<slug>.fixtures]` host override supplies the same
fixture fields until repo policy reaches its trusted base. Its `host_override`
provenance is visible; it replaces the complete fixture selection, not an opaque
recursive merge. Remove it when the committed repo policy is available.

### Acquisition and shared fixtures

Each phase gets a full independent clone, detached at the recorded candidate SHA,
with recursive submodules initialized. Missing pinned objects receive one explicit
SHA fetch attempt; failure stops the phase instead of substituting a newer head.
There is no clone cache or dependency on a developer checkout. Failed acquisition
is retained with a private diagnostic log; no automatic execution retry occurs.

One fixture slot is shared per GitHub repository unless the host project selects
`fixture_resource`. EQEmu uses a named host resource with `worker_home`,
`stack_path`, and `workspace_cleanup=false`; see the host example. The adapter
sets VALIDATION_WORKER_HOME, AKKSTACK_DIR and job-local
VALIDATION_AFK_EVIDENCE_DIR. Existing repository worker/stack locks remain in use.
Different named resources can run independently. Queue wait and command execution
each have the fixture timeout, rather than one shared end-to-end deadline.
The EQEmu example selects the foreground `build-unit-v1` command with an outer
20400-second fixture timeout and 780 seconds for cooperative cancellation.

The per-job `cleanup` command keeps external resource cleanup disabled.
The project-wide `gc` command requires a host-selected resource adapter that
holds resource leases while checking release and deleting artifacts. In particular, an inactive local Compose client is not proof that a
Docker daemon-owned container has stopped. This configuration change does not
fix that existing EQEmu timeout concern or authorize unsafe stack reuse.

### Pass behavior and retained results

`pr` starts one direct implementation from a central Bead and opens a draft PR.
It rejects closed or ambiguously owned Beads, but does not add legacy readiness or
acceptance-planning gates. The host commits and pushes an absent
`afk-pr-BEAD_ID` branch using a create-only lease. A moved base/destination pauses
work. Repeating the command reports the retained job or matching PR instead of
starting another implementation. A paused job remains attached to the Bead; inspect
its explanation and workspace, or create a successor Bead for a new attempt.

`review` captures the description, commits, ordinary comments, all reviewers,
inline discussion, checks/annotations and statuses. It launches independent
read-only inference and deterministic fixture services. `--fixtures-only` omits
AFK inference, leaving existing reviewers to supply feedback. Review posts a
COMMENT review and never pushes code. Multiple-reviewer expansion remains possible;
the current default is one `afk` reviewer.

`respond` reads the captured PR story and edits an independent clone. It may
repair, disagree, defer, seek clarification or make no changes. The host verifies
that the PR head/base and branch remain unchanged before pushing normally. Fork
response branches remain unsupported. New pushes schedule fixtures at that exact
commit; no-change responses post an explanation without another fixture run.
No pass merges or automatically invokes another model pass.

Fixture results have a commit status and updateable summary with bounded redacted
log excerpts. Detailed logs, inference evidence, task snapshots and progress stay
in the durable job directory. A completed creation/response is not a passing
fixture result. Feedback arriving after capture is available to the next explicit
pass; there is no mandatory per-comment disposition schema.

`context` reads GitHub without host configuration. `status` reads GitHub and the
selected state root without needing a checkout or Beads access. Publication retry
uses retained job facts and phase locks; it never repeats inference, commits or
pushes. Creation retry can recover/create a PR and schedule fixtures when no child
was recorded. Response retry only posts the summary. An uncertain push or failed
recorded child still requires inspection; use an explicit fixtures-only review
when appropriate. Stopped workers are not automatically resumed.

### Cleanup and retained jobs

`cleanup JOB_ID --dry-run` explains eligibility. Without dry-run it removes only
owned new-layout independent clones for successful, published, inactive jobs.
Required fixture children must pass and publish. Shared lifecycle locks protect
workers/retries while cleanup holds an exclusive lock. Missing evidence, active
or unobservable workers, external resources, failed/paused work, uncertain pushes,
changed HEAD or unexpected files retain the workspace. No-change responses do not
need a fictitious push. Ignored dependency/build scratch is disposable.

A deletion receipt permits an interrupted cleanup to finish on a later explicit
call. Job evidence remains, and old workers cannot restart after deletion begins.
No automatic age-based cleanup, forced deletion or durable-evidence expiry exists.
Historical linked worktrees are excluded. Do not store required artifacts only
inside clones or as symlinks into them.

`afk gc --project PROJECT` previews bounded retention across that project's PR
jobs. Add `--apply` to remove eligible artifacts. `--keep N` defaults to two and
must be at least one. It also retains each PR's latest candidate workspace and
latest passed and failed fixture jobs. It checks worker inactivity, clean clones,
published terminal records and candidate push receipts before deleting anything.
Old failed fixtures can be collected; failed inference stays available for inspection.
The JSON report includes per-job reasons, targets and allocated bytes. Retained
bytes cover known job/workspace/validation roots, not a full host storage scan.
Preview reports current retained bytes; apply reports bytes remaining afterward.

External fixture resources may set `cleanup_adapter` to an absolute path to an
operator-trusted Python module in host TOML. The module exports a context manager
`cleanup_targets(directory, job, *, apply=False, resume=False)` that yields owned generated
directories, holding resource leases until the context exits. It must refuse
uncertain release and protect baseline data and external references. AFK never
selects this executable from candidate policy or job metadata. Resource identity
must still match the host registration. Missing adapters retain external jobs.
The EQEmu adapter lives in its repository at `scripts/afk_cleanup.py`.

GC retains job records, logs and action receipts, and writes `cleanup.json` before
removal so old workers cannot restart. It does not prune Docker or remove legacy
linked worktrees. An interrupted deletion resumes only its recorded target list, after rechecking
worker inactivity and resource ownership. New eligibility checks include local
refs and reflogs, including submodules, to retain unpublished commits. The EQEmu
adapter recovers its own interrupted GC leases while holding both worker guards;
ordinary validation leases still require the repository recovery command.
No automatic scheduling is installed by this command.

Current PR and orchestration commands accept host TOML only, including job status,
cleanup and publication retry. Existing saved jobs remain available through the
TOML `state_root`; no JSON translator or fallback is used. A new state root cannot
discover an old unpublished local job: inspect old roots before changing the
default, keep active work on its original root and never delete records to bypass
duplicate detection.

PR commands ignore unrelated repository `[validation]` settings; they may coexist
with `[fixtures]`. Configure PR fixtures explicitly or provide the conventional
executable entrypoint.

### Bead evaluation

`afk evaluate BEAD_ID` runs one advisory evaluation in the foreground and prints
JSON containing the report, observed repository context and retained directory.
It uses the same discovered host TOML and central Beads credentials as `pr`.
No readiness label, fixture policy, Git commit identity or previous project
checkout is required. The command does not change the Bead or post to GitHub.

The evaluator reads the Bead's exact acceptance text, notes and direct dependency
summaries. It acquires an independent clone of the registered repository's
GitHub default branch and uses read-only inference. This is default-branch
context, not an open PR's implementation. Missing ownership, registration or
repository access is recorded explicitly; evaluation can still run without
tools using the frozen Bead alone. Missing Beads or invalid host configuration
are command errors.

The report recommends readiness, identifies material gaps and asks useful
clarification questions. It distinguishes repository ownership from examples,
implementation choices from ambiguity, and repository work from host-only
verification. It does not decompose work, assign criteria, authorize execution,
merge, close tasks or start another command. Recommendations are judgment,
not deterministic guarantees or mandatory gates.

Evidence is retained under `state_root/evaluations/ID`: `bead.json`,
`context.json`, `evaluation.json`, `report.md` on success and runtime evidence
under `inference/`. Repository context lives under
`workspace_root/evaluations/ID/evaluation`. Existing `cleanup JOB_ID` applies
only to PR jobs; evaluation evidence/clones are retained for now. Calls are
independent, with no automatic resume or deduplication. Exit 0 means a report
was produced, including reports recommending clarification; failures exit 1.

The tool-free fallback inherits the inference runtime's 64 KiB task-data limit.
Oversized fallback input fails explicitly; it is not silently shortened.
Default-branch context can lag active work, so reports need human judgment.

### Explicit PR finish

`afk finish PR_URL` creates a read-only preview and prints JSON with its ID,
head, target branch, merge method and Bead association hints. It makes no
GitHub or Beads changes. Add `--close-bead BEAD_ID` to explicitly select one
closure target, and optionally `--method squash|rebase` instead of the default
merge commit. Unsupported methods fail through GitHub, without fallback.

Execute that exact preview with `afk finish PR_URL --apply PREVIEW_ID`.
Changing its method or closure target requires a new preview. The selected
Bead must still be readable before any merge request. GitHub handles its
native merge policies and queues; the command never requests admin bypass,
branch deletion, local cleanup, inference, or fixture execution.

A queued merge returns promptly as pending. Repeat the apply command later.
Every attempt re-reads external state: already merged PRs skip the merge
request, and only an explicitly selected Bead closes after a fresh merge
observation. Already closed Beads are left alone. Merge success followed by
closure failure is retained as partial progress and can be retried.

Evidence lives in `state_root/finishes/PREVIEW_ID/preview.json`, with one
private attempt subdirectory per execution containing `result.json` and
native command diagnostics. This is an audit record, not completion authority.
Exit 0 means preview created or finish completed; pending, unknown and failed
outcomes exit 1. No worker waits for a queue or automatically retries.

GitHub enforces the expected head at merge time. The target branch is checked
before and after the request, but that check is not atomic with merging.
A concurrent retarget can be detected too late to prevent a merge. GitHub and
Beads do not share a transaction; this command does not claim otherwise.
Repository protections still depend on repository settings and caller privileges.

### Remaining-scope assessment

`afk assess PR_URL [--bead BEAD_ID]` runs an optional foreground scope check
against the selected central Bead. Without `--bead`, the PR body must contain
exactly one `<!-- afk-bead:ID -->` marker. Explicit selection supports child Beads
and partial or multi-PR delivery. It does not infer closure from that marker.

The assessor reads the execution summary first, then the frozen Bead, full PR
story and repository metadata. It may inspect an exact-head clone to answer a
specific scope question. It does not run tests, fetch artifacts, repeat code
review, post feedback, create work, merge or close anything. Missing repository
access is recorded and the pass can continue using the remaining evidence.

Reports contain four nonempty Markdown sections in order: `Remaining
requirements`, `Deferrals`, `Uncertainty`, and `Evidence`. They describe what
remains and cite supplied evidence, with no overall readiness or approval
verdict. A deferred acceptance requirement remains unmet. Matching-head terminal
execution records take precedence over older prose about that run being pending.
The host validates report shape and length, not the truth of the model's prose
or citations. A report is advice for the caller, never a gate for `finish`.

Evidence lives under `state_root/assessments/ID`, or the configured run root;
clones live under `workspace_root/assessments/ID/assessment`. Files include
`bead.json`, `context.json`, `execution-summary.json`, `repository.json`,
`assessment.json`, successful `report.md` and private inference receipts.
The host rechecks head/base/base branch and flags changed or unknown freshness.
Exit 0 means a report completed; exit 1 means execution failed.

Contract version 2 replaces the former free-form ready/remaining-work/insufficient-
evidence response with the four sections. The outer command JSON and `report.md`
remain unchanged. Historical reports are retained; consumers must not parse old
readiness words as an approval signal. The internal inference purpose remains
`completion_assessment` for model configuration compatibility.

### Git identity and diagnostics

Before creation or response inference, the worker checks `git var GIT_AUTHOR_IDENT`
and `git var GIT_COMMITTER_IDENT` in its actual clone. Git uses that worker's
configuration and environment. If either fails, the phase fails before model
invocation with setup guidance. Normally configure `user.name` and `user.email`
globally for the OS user running the worker; valid Git identity environment
overrides also work. Read-only review does not require commit identity.

Failed identity checks and nonzero Git commands caught by a PR worker retain
raw stderr in private `PHASE.git.log` with mode 0600. Published phase results
contain safe summaries, not raw diagnostics. The check establishes identity
resolution only; later commit hooks, signing, permissions or changed config
can still fail. There is no automatic retry or host configuration change.

### Repository public fixture diagnostics

A fixture command may write `fixture-evidence/public-summary.json` beneath its AFK job.
For configured fixture resources this is `$VALIDATION_AFK_EVIDENCE_DIR/public-summary.json`.
AFK accepts at most 8192 bytes and requires exactly these version-1 fields:

```json
{
  "schema_version": 1,
  "head": "<exact job commit>",
  "profile": "tier1-migration-tier3",
  "status": "failed",
  "step": "upgraded_assertions",
  "diagnostic_codes": ["actor_events.event_json_constraint"],
  "timings_ms": {"validation": 1500, "restore": null}
}
```

Profile, optional step and up to 24 diagnostic codes must be lowercase identifiers
of at most 96 characters, using letters, digits, underscores, dots or hyphens.
Status is `passed` or `failed`; timings are null or integer milliseconds from zero
through seven days. Head must match the job. Unknown keys or versions are rejected.

A valid summary replaces the existing top-level log excerpts in the fixture comment.
AFK still applies its public-log redactor and HTML escaping. Missing, malformed,
oversized, symlinked or wrong-head summaries fall back to existing excerpts without
changing execution state. The repository owns safe diagnostic content; this contract
is not an arbitrary-data secrecy guarantee. Do not write private values as codes.
AFK's own outcome and exit code remain authoritative. No nested logs are uploaded.

#### Failure evidence and observation retries

Repositories may write `fixture-evidence/diagnostic-files.json` with
`{"schema_version":1,"head":"<40-character candidate SHA>","files":["relative/path.log"]}`.
The manifest is private, limited to 8 KiB and twelve file paths. Responses admit
regular files under that evidence directory only when the retained fixture job
matches the observed failed status, PR, head and base. Absolute paths, parent
traversal, symlinks and escaped files are excluded. These paths precede the
existing wrapper logs within the fourteen-file evidence allowance. AFK does not
parse repository-specific diagnostics or publish their raw contents.

A failed read-only job/status command gets three total attempts, thirty seconds
apart in the worker. Failure counts survive worker restarts, reset when that
command succeeds, and do not consume response attempts. Ordinary resume starts
a new observation allowance. Failed submissions and invalid evidence still
pause immediately. Bounded stdout/stderr tails and exit/error metadata are
retained as mode-0600 JSON under the run's private `command-errors/` directory;
events refer to their paths, not their contents.

When a phase has a terminal result but publication is pending, `afk job` probes
its existing worker and re-reads the phase record. `afk status` waits for active
publication of successful results. The supervisor also waits for active
publication of failed fixtures before selecting a repair. Inactive or unknown
publishers and failed publication still require attention; no second publisher
or inference worker is launched by observation.
