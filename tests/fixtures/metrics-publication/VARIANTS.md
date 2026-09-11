# Metrics publication variant matrix

The original bundles remain frozen at their original delivery; their portable publication examples are advanced to the same current schema. `populated/` adds synthetic publications produced by the real exporter and metrics publisher. No host Run evidence is copied. All publication fixtures use the single current metrics publication schema v2.

| Case | Allowed fields and meaning | Proof |
| --- | --- | --- |
| Pi measured Review, v3 | `adapter_family: pi`, `observed_identities`, finalized request/identity fields, full cost with provenance. Configured model and observed provider/model can differ. Cache and compaction remain separate usage objects. Cost includes measured compaction cost. | Populated Review, sequence 4; `test_committed_and_regenerated_cases_have_bound_measured_variants` |
| Pi measured zero | Full Pi shape, token categories and estimate amount explicitly zero; currency remains null and billed charge false. | Assessment, sequence 5, same test |
| Pi unavailable | Full Pi shape still required; usage empty, amount null, provenance fields null. Minimal cost is invalid for Pi. | `populated-v3` Attempt, sequence 1, same test |
| Pi partial | Only observed token categories are present; unmeasured compaction keeps coverage partial. Missing price stays unavailable. | `populated-partial` Attempt, sequence 1, same test |
| Repository Validation unavailable or zero | Legacy missing duration becomes null; measured zero remains zero. Neither is inference response validation. | `populated-partial` versus `populated-v3` sequence 2, same test |
| Unsealed abandoned invocation | Nullable identity, no observed identities or Pi counters; reason `unsealed_abandoned_invocation`, minimal unavailable cost. Can be published when the observed abandoned directory has no receipt. | `bundle-abandoned`, same test |
| Unsupported sealed adapter | Local report can use reason `unsupported_adapter` and minimal cost; current Exporter rejects non-Pi receipt contracts. Not a valid publication fixture. | `tests/test_metrics.py` unsupported-adapter tests and Exporter receipt validation |
| Original and continuations | A retained stage referenced by continuation history is counted once in the selected Run. Stage ownership uses the exact selected Run ID. A distinct sealed receipt at another retained path is not an alias and is invalid. | schema-v2 `evidence-coverage-variants.json` shared-continuation Run and matching bundle; `test_each_exhausted_continuation_adds_a_fresh_response_allowance`; `test_continuation_alias_rejects_a_distinct_authenticated_receipt` |
| Totals | Cost has currency and billed-charge fields but no invocation provenance; usage and compaction usage remain separate. | Populated totals and `invalid-mutations.json` |
| Empty stage | Minimal unavailable cost is permitted; no inferred model or fabricated timing. | Original baseline stages |
| Evidence coverage complete | All three expected Attempt/Review/Assessment stages have authenticated receipts, independent of usage/cost availability. | `populated-partial` and `populated-v3` |
| Command Attempt omitted | A started command-worker Attempt remains expected and is listed with `missing_receipt`; Review measurements make coverage partial. | `producer-only-v2.json` |
| Unsealed abandoned | The abandoned Response is listed with `unsealed_receipt`; a later failed Response without evidence remains `missing_receipt`. | `bundle-abandoned` |
| Verified no-action Response | A started Response with authenticated no-agent, unchanged-repository output is excluded from expected inference. Absence of a receipt alone never establishes this case. | `evidence-coverage-variants.json` verified-no-action case; `test_verified_no_action_response_is_not_expected_inference` |
| Review producer topology | Omitted mode is combined. Split retains empty lens results and duplicate findings in source order, publishes no aggregate or Assessment after a partial failure, and keeps split mode without partial reuse across abandonment/continuation. | Generated portable `populated/review-variants.json`; `test_generated_review_variant_matrix_preserves_failure_and_provenance` |

`populated/evidence-coverage-variants.json` is a regenerated schema-v2 publication for verified no-action and shared-continuation Runs, with matching exported bundles committed beside it. `populated/invalid-mutations.json` describes independent changes to a fresh copy of the populated valid publication. Each must fail consumer intake. Paths use JSON object keys and array indices; `-1` means the last array row. Mutations cover Pi discriminator/field disagreement, minimal Pi cost, invocation provenance on totals, unknown token categories, invalid Run purpose, and Pi fields on abandoned evidence. They are test inputs, not a second validator or a schema change.

## Known bundle compatibility gaps

Both populated measured cases use bundle schema v3. `producer-only-v2/` and `producer-only-v2.json` reproduce current exporter output that Operations rejects because its v2 inference artifact vocabulary does not include the current section kinds. This is not a valid consumer baseline. `central-f5ie` owns deciding whether to repair or retire populated v2 export. The original empty-inference v2 baseline remains valid and unchanged.

The abandoned case in the supported set uses an abandoned Response followed by a sealed failed Response. A different case, failed Validation followed by an abandoned Response with Validation remaining the terminal cause, exposed a consumer history mismatch tracked in `central-1ck5`. It is retained in the initial producer delivery at commit `28a9880`, rather than being misrepresented as supported intake. Metrics shape support does not imply that every enclosing bundle history is admitted.

## Reproduction and adoption

From the AFK repository root, run:

```sh
python3 -m tests.metrics_publication_fixtures /absolute/new/directory
python3 -m unittest tests.test_metrics_publication_fixtures tests.test_coordinate_cli.CoordinatorCliTest.test_each_exhausted_continuation_adds_a_fresh_response_allowance
```

The destination must not exist. Generation uses temporary synthetic evidence and leaves only portable bundles and a publication. Source revision is deliberately null in synthetic output. Private temporary paths affect authenticated opaque hashes, so regeneration is measurement-equivalent rather than byte-identical. Every generated bundle and publication is bound using its own actual hashes. The committed fixture bytes, once delivered, are the consumer baseline. Invalid mutations are maintained separately and can be applied to either generated or committed cases.

Consumers copy `populated/` and this matrix from the pushed producer delivery, recording its exact SHA in their upstream provenance. Preserve the old baseline. Admit all three matching bundles before publishing the complete populated snapshot. Exercise the invalid mutations through the same public intake used for valid cases. Do not adapt fixtures by guessing nested fields or by weakening intake.
