# AFK metrics publication schema v1

These fixtures are synthetic and were generated from `ExportCliTests.sealed_preparer`; they contain no host Run evidence. `bundle-v2/` and `bundle-v3/` are the matching export bundles. `valid-publication.json` is a complete two-Run consumer fixture. `invalid-publication.json` has a deliberately incorrect first `workflow_run_sha256` and must be rejected by a consumer that is given the bundle.

## Producing a snapshot

```text
python3 -m afk_metrics publish INPUT_JSON DESTINATION_JSON
```

The input is exactly:

```json
{"schema_version":1,"project":"project-slug","runs":[{"source":"/absolute/local/run","bundle":"/absolute/local/bundle","selection":"original"}]}
```

`selection` is `original`, `latest`, or the exact numeric retained continuation directory identifier (for example `01`). There must be 1–25 entries. Source and bundle paths are local producer inputs and never occur in output. The destination must be a new absolute file outside every source and bundle.

The caller owns a stable output parent and stable source/bundle root topology for the whole call. The existing parent must support local regular-file hard links. Stable symlink aliases are supported. Cooperating publishers may race to create the same final name but must not replace or delete each other's entries. Directory relocation, entry substitution and changing mount aliases are outside this contract.

The producer serializes and bounds the complete object before allocating a private staging file in the output parent. It writes, flushes, syncs and closes that file, then creates the final hard link without overwrite and removes staging. A successful return leaves only the final file. A pre-commit failure does not create the final name; an existing winner remains untouched. If staging cleanup fails after commit, the complete final file remains and the error explicitly says `committed; staging cleanup failed`. Inspect that output before retrying. Crash cleanup and power-loss durability are not guaranteed. There is no direct-write fallback on filesystems without hard links.

The existing local report command remains `python3 -m afk_metrics SOURCES... --destination DIRECTORY`. Its `--destination` option selects legacy report mode, including when a source directory is named `publish`.

## Envelope (frozen v1)

A publication object has **exactly** these fields:

* `schema_version`: integer `1`.
* `kind`: string `afk-metrics-publication`.
* `project`: the common Project slug.
* `producer`: object with exactly `calculator` (`afk_metrics.report`), `report_schema_version` (`1`), and `source_revision` (the exact 40–64 lowercase-hex Git object revision when known, otherwise `null`).
* `runs`: 1–25 Run objects, in input order.
* `comparisons`: every unordered Run pair once, in Run order (at most 300). This is the unchanged report comparison shape: `left` and `right` source identities, boolean `equivalent_frozen_conditions`, string-array `warnings`, and `ranking` enum `observational_only | not_provided`.
* `limitations`: string array of producer limitations.

The UTF-8 encoding is at most 16 MiB and the publication has at most 10,000 total stage rows. Bounds are fail-closed: output is never truncated.

## Run and bundle binding

Each Run object has exactly `binding`, `summary`, and `stages`.

`binding` has exactly:

* `project`: exact Project slug.
* `run_id`: exact selected Run identity. A continuation remains a distinct ID such as `run.continuation.01`; consumers must not strip its suffix or join on Bead ID.
* `bundle_schema_version`: integer enum `2 | 3`.
* `workflow_run_sha256`: lowercase SHA-256 of the exact verified `workflow-run.json` bytes.

The producer verifies the manifest identity and the workflow file's declared size/hash. It independently normalizes the selected source observation and compares all semantic Run fields. Only bundle `artifacts`, v3 `inference_sessions`, and operational publication delivery fields are excluded. Schema version is normalized to the verified bundle version. Caller-provided digests are not accepted.

`summary` is the complete schema-1 local report Run object, unchanged: `source_identity`, `integrity`, `run_identity`, `work`, `outcome`, `inference`, and `timing`. The nested contract is listed below. `source_identity` is the key used by comparisons.

## Nested summary contract

A published summary always has `integrity: {"status":"verified"}`. Invalid evidence rejects the publication rather than emitting an invalid summary. `source_identity` is the SHA-256 of compact, sorted-key JSON containing the authenticated `identity` and frozen `assignment`. This differs from the hash of the exported workflow bytes.

* `run_identity`: `project`, selected `run_id`, and `bead_id`, which can be `null` where unavailable.
* `work`: `objective_sha256`, nullable `base_commit`, and `validation_conditions_sha256`. The hashes describe frozen conditions, not measured quality.
* `outcome`: `terminal` is `completed | failed`; `coordinator_status` is `completed | failed`; `coordinator_decision` is `stop | exhausted | null`. `validation_results` lists retained Validation outcomes, `passed | failed | timed_out | interrupted`. `repair_count` counts Response components and `retry_count` counts observed inference retries. `completion_acceptance` and `integration_status` are currently the literal `unavailable`.
* `inference`: `invocations` and `totals`, described below.
* `timing`: the fields described below.

All coverage fields use `complete | partial | unavailable`. All token objects contain only measured non-negative numeric fields from `input`, `output`, `cacheRead`, `cacheWrite`, `totalTokens`, and `reasoning`. Missing keys do not mean zero. Counts are non-negative integers. Seconds are non-negative numbers or `null` unless a legacy sentinel is explicitly stated below.

`inference.totals` has `elapsed_seconds`, `usage`, `compaction_usage`, `usage_coverage`, and `cost`. Cost has `status: reported_estimate | partial | unavailable`, `kind: pi_reported_estimate | unavailable`, nullable numeric `amount`, `currency: null`, and `billed_charge: false | null`. A partial amount is the measured subtotal. No currency or invoice charge is inferred.

Each invocation has:

* `source_event_identity`: authenticated invocation hash or an opaque stable fallback for unsealed abandoned evidence.
* `purpose`: `acceptance_planning | attempt | review | finding_assessment | feedback_response`.
* Nullable string `adapter`, `adapter_family`, `provider`, and `model`. Pi invocations also have `observed_identities`, an array of objects with nullable `provider` and `model`.
* `outcome`: `succeeded | response_rejected | adapter_failed | validator_failed | timed_out | interrupted`; nullable `attempt_count`.
* `elapsed`: `kind: invocation_adapter_elapsed_not_pure_inference`, nullable `seconds`, and nullable timestamp strings `started_at` and `ended_at`.
* `response_validator_seconds`, `response_validator_coverage`, and `metrics`.

Invocation `metrics` always has `coverage`, `usage`, `compaction` with `aggregate_count` and `usage`, `retry_count`, and `cost`. Pi metrics additionally have `finalized_requests`, boolean `request_count_exact`, and `identity_coverage`. Their cost has the totals cost fields plus `provenance` with `calculator: "Pi model rates" | null`, `pi_version: null`, and `price_table_date: null`. Unsupported or unsealed invocations instead include `reason: unsupported_adapter | unsealed_abandoned_invocation` and the minimal unavailable cost object with `status`, `kind`, and `amount: null`. These are local-report variants; publication additionally requires a bundle authenticated by the current Exporter, whose receipt artifact contract currently supports Pi. This feature does not extend adapter export support.

`timing` has:

* Nullable second values `run_wall_span_seconds`, `preparation_seconds`, `publication_seconds`, `inference_invocation_seconds`, `repository_validation_seconds`, `response_validator_seconds`, and `unattributed_seconds`.
* `repository_validation_coverage` and `response_validator_coverage`.
* `active_execution_intervals`: objects with `kind: preparation | inference_invocation | repository_validation | publication` and measured `seconds`. Publication entries also have `nonoverlapping_seconds`. These are measured duration rows, not a disjoint timeline and not values to sum into wall time.
* `deterministic_steps`: `Validation` is measured seconds or the string `unavailable`; `Change` and `Iteration` are the string `unavailable`.
* `continuation_wait_gaps`: the string `unavailable`.
* `overlap_note`: explanatory string. Unattributed time subtracts the union of authenticated intervals from wall span; nested response validation is not subtracted twice.

## Stage rows

Every stage row has `ownership`, `outcome`, and these named metric fields:

* `elapsed_seconds`: non-negative number or `null`.
* `elapsed_kind`: `invocation_adapter_elapsed_not_pure_inference`, one of the Run purposes below, or `null`. Invocation elapsed includes adapter/runtime/tool work and is **not** pure model latency.
* `usage`, `compaction_usage`: token-field objects; `{}` means no supported measurement, not zero use.
* `usage_coverage`: enum `complete | partial | unavailable`.
* `retry_count`: non-negative observed retry count.
* `cost`: the existing report cost object. Its `status` is `reported_estimate | partial | unavailable`; an absent amount is `null`, never zero-filled. Reported estimates are not billed charges.
* `response_validator_seconds`: number or `null` and `response_validator_coverage`: `complete | partial | unavailable`. This is inference response validation, not repository testing.
* `repository_validation_seconds`: number or `null` and `repository_validation_coverage`: `complete | partial | unavailable`. This is populated on each authenticated Coordinator Validation component row from that component's already-verified output.

Missing or unsupported measurements use `null`, `{}`, and an explicit `unavailable`/`partial` marker. Zero is emitted only when zero was measured. Pure model latency is never derived.

Component ownership is exactly:

```json
{"kind":"component","project":"project-slug","run_id":"exact-run-id","sequence":2,"component":"validation"}
```

`component` uses the Coordinator component enum: `attempt | validation | change | review | assessment | response | iteration`. `sequence` is the exact positive Coordinator sequence. `outcome` is the Coordinator history outcome.

Run ownership is exactly:

```json
{"kind":"run","project":"project-slug","run_id":"exact-run-id","purpose":"preparation"}
```

The bounded `purpose` enum is `acceptance_planning | preparation | publication | run_wall_span | unattributed`. Run timing rows have `outcome: null`; acceptance-planning invocation rows carry their authenticated invocation outcome. Run-level unavailable timings remain `null`.
