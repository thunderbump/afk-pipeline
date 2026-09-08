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

`summary` is the complete schema-1 local report Run object, unchanged: `source_identity`, `integrity`, `run_identity`, `work`, `outcome`, `inference`, and `timing`. See the fixture for nested report fields. `source_identity` is the key used by comparisons.

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
