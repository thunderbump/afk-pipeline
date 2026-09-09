# Finding validity evaluation cases

These cases specify expected judgments for Review/Assessment evaluation. They
are not inference results and are not included in production prompts. Retained
cases summarize the September 8 review-cycle investigation and context replay;
synthetic controls isolate the rule. Use the frozen objective, candidate and
actual evidence when replaying a retained case. Do not use an old Assessment as
ground truth. The Operations worklogs `2026-09-08-review-cycle-analysis` and
`2026-09-08-review-context-experiment` contain the investigation and adjudication.

The deterministic tests verify actual prompt delivery, schemas and independent
validity/ownership routing. They do not prove that a model makes these judgments.
For a future live evaluation, keep model and context fixed, preserve responses,
and compare validity and ownership separately against the expectations below.

## Required tests omitted, retained h1mt.1 case

Objective: implement metrics publication with tests for original, latest and
explicit continuation selection. Evidence: Reviews 06 and 12 identified missing
required selection coverage; Assessments 07 and 13 acknowledged the requirement
but rejected it because the omission did not show a reachable runtime defect.
The final candidate's publication fixtures still selected latest only.

Expected: report/confirm the explicit missing test deliverable, current scope.
Name the acceptance requirement and missing scenarios. No runtime failure is
needed, and merely claiming that passing tests prove completion is insufficient.
Missing required evidence-reader tests were also confirmed in central-43zn.68;
the same standard applies in both cases.

## Extra coverage requested, synthetic negative control

Objective: serialize a bounded local report; no concurrency support or mutation
stress-test deliverable is required. Evidence: a reviewer requests exhaustive
concurrent mutation tests without identifying required behavior or a feasible
failure in supported use.

Expected: do not report; reject if supplied for Assessment. Current scope can
still be recorded, but a coverage preference is not an established defect.

## Observable behavior failure, synthetic positive control

Objective: sum token usage across supported fixture-adapter retry attempts.
Evidence: two supported attempts report 10 and 20 tokens; the implementation
returns 20 because it replaces the accumulator on each attempt.

Expected: report/confirm, current scope. The supported trigger, incorrect total
and mechanism establish the defect independently of any required test list.

## Impossible adapter mechanism, retained replay negative control

Objective: calculate metrics from Pi invocation evidence. Claim: multiple runtime
attempts in one Pi invocation lose earlier usage. Evidence: the actual Pi adapter
allows one runtime attempt; provider retry segments occur inside that attempt.

Expected: reject this mechanism. Do not reject the analogous fixture-adapter
case above, whose adapter actually permits multiple runtime attempts. Inspect
provider retry accounting separately if the evidence demonstrates a real defect.

## Demonstrated design cost, synthetic positive control

Objective: add an adopted report field through the existing shared serializer.
Evidence: the patch duplicates field selection in two callers; the documented
field addition now requires editing both copies, and one already omits the new
field. The finding identifies both paths and the concrete change they duplicate.

Expected: report/confirm, current scope. The demonstrated maintenance/change
cost supports a design finding even before a user-visible crash. A generic
request to introduce classes or a preferred pattern would not.

## Required documentation omitted, synthetic positive control

Objective: deliver a publication envelope and document every field and enum.
Evidence: executable fixtures emit nested summary availability values, but the
required contract document omits those fields and values.

Expected: report/confirm, current scope. Cite the required contract and missing
entries. The consumer's ability to guess them does not satisfy the deliverable.

## Adopted standard versus preference, synthetic paired control

Objective: extend a CLI under an adopted rule requiring value-safe error output.
Evidence: a new exception prints the supplied credential-bearing value.

Expected: report/confirm the applicable rule violation, current scope. In
contrast, reject a request to rename a compliant helper solely because the
reviewer prefers another naming style. Do not invent an adopted standard.

## Independent ownership, synthetic control

Objective: publish metrics; frozen related record `sibling` owns the console
renderer. Review claims a demonstrated console formatting defect is current.
Evidence: publication bytes satisfy the current contract; only the renderer
misinterprets them, and the sibling explicitly owns that behavior.

Expected: confirm validity but override ownership to related, naming `sibling`
and giving an independent scope rationale. The existing route excludes it from
current-work Response. If ownership cannot be established, use unknown rather
than fabricating a record. Neither related nor unknown makes a real defect false.

## Synthetic fixture paths, retained replay negative control

Objective: publish sanitized host evidence and repository-owned synthetic fixtures.
Claim: placeholder paths in the synthetic fixture prove a private host-path leak.
Evidence: the paths are authored example values, not extracted from host Runs.

Expected: reject the unsupported leak claim. A reproduced host path in an actual
public artifact would be a different, potentially valid finding. Low impact is
not itself a rejection reason, and no case implies a required rejection quota.
