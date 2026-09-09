# Metrics publication variant matrix

The original `valid-publication.json`, `invalid-publication.json`, and bundles remain frozen at their original delivery. `populated/` adds synthetic publications produced by the real exporter and metrics publisher. No host Run evidence is copied. The schema remains v1.

| Case | Allowed fields and meaning | Proof |
| --- | --- | --- |
| Pi measured Review, v2 and v3 | `adapter_family: pi`, `observed_identities`, finalized request/identity fields, full cost with provenance. Configured model and observed provider/model can differ. Cache and compaction remain separate usage objects. Cost includes measured compaction cost. | Populated Review, sequence 4; `test_committed_and_regenerated_cases_have_bound_measured_variants` |
| Pi measured zero | Full Pi shape, token categories and estimate amount explicitly zero; currency remains null and billed charge false. | Assessment, sequence 5, same test |
| Pi unavailable | Full Pi shape still required; usage empty, amount null, provenance fields null. Minimal cost is invalid for Pi. | Run-level acceptance planning, same test |
| Pi partial | Only observed token categories are present; unmeasured compaction keeps coverage partial. Missing price stays unavailable. | Attempt, sequence 1, same test |
| Repository Validation unavailable or zero | Legacy missing duration becomes null; measured zero remains zero. Neither is inference response validation. | v2 versus v3 sequence 2, same test |
| Unsealed abandoned invocation | Nullable identity, no observed identities or Pi counters; reason `unsealed_abandoned_invocation`, minimal unavailable cost. Can be published when the observed abandoned directory has no receipt. | `bundle-abandoned`, same test |
| Unsupported sealed adapter | Local report can use reason `unsupported_adapter` and minimal cost; current Exporter rejects non-Pi receipt contracts. Not a valid publication fixture. | `tests/test_metrics.py` unsupported-adapter tests and Exporter receipt validation |
| Original and continuations | A source invocation identity can occur in several selected Runs. Uniqueness is within each Run. Stage ownership always uses the exact selected Run ID. | `test_each_exhausted_continuation_adds_a_fresh_response_allowance` builds one combined publication over real synthetic Coordinator lineage |
| Totals | Cost has currency and billed-charge fields but no invocation provenance; usage and compaction usage remain separate. | Populated totals and `invalid-mutations.json` |
| Empty stage | Minimal unavailable cost is permitted; no inferred model or fabricated timing. | Original baseline stages |

`populated/invalid-mutations.json` describes independent changes to a fresh copy of the populated valid publication. Each must fail consumer intake. Paths use JSON object keys and array indices; `-1` means the last array row. Mutations cover Pi discriminator/field disagreement, minimal Pi cost, invocation provenance on totals, unknown token categories, invalid Run purpose, and Pi fields on abandoned evidence. They are test inputs, not a second validator or a schema change.

## Reproduction and adoption

From the AFK repository root, run:

```sh
python3 -m tests.metrics_publication_fixtures /absolute/new/directory
python3 -m unittest tests.test_metrics_publication_fixtures tests.test_coordinate_cli.CoordinatorCliTest.test_each_exhausted_continuation_adds_a_fresh_response_allowance
```

The destination must not exist. Generation uses temporary synthetic evidence and leaves only portable bundles and a publication. Source revision is deliberately null in synthetic output. Private temporary paths affect authenticated opaque hashes, so regeneration is measurement-equivalent rather than byte-identical. Every generated bundle and publication is bound using its own actual hashes. The committed fixture bytes, once delivered, are the consumer baseline. Invalid mutations are maintained separately and can be applied to either generated or committed cases.

Consumers copy `populated/` and this matrix from the pushed producer delivery, recording its exact SHA in their upstream provenance. Preserve the old baseline. Admit all three matching bundles before publishing the complete populated snapshot. Exercise the invalid mutations through the same public intake used for valid cases. Do not adapt fixtures by guessing nested fields or by weakening intake.
