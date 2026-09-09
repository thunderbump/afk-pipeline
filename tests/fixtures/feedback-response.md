# Feedback Response regression and evaluation case

## Retained evidence-binding sequence

The September 8 investigation of central-h1mt.1 recorded Reviews 06, 18, 24,
30, 36, 42 and 48 revisiting evidence-observation binding as repairs introduced
successive special cases. Later output-cleanup repairs alternated between
removing potentially foreign files and retaining unwanted links. The Operations
worklog `2026-09-08-review-cycle-analysis` records the retained observations.
This is an evaluation case, not a claim that every finding shares one cause or
that the historical Assessment was always correct.

The governing requirement is that a metrics publication binds its measurements
and semantic bundle comparison to the same verified source observation. The
Response should inspect that invariant across directly affected readers/callers,
then choose the smallest owned repair. A special-case check on only the latest
reported path is insufficient if another supported path demonstrably violates
the same invariant. Removing redundant observation machinery may be simpler.
Do not expand this into arbitrary filesystem hardening or resolve ambiguous
publication guarantees implicitly; central-354d owns that contract decision.

For a future paired replay, select the same frozen pre-repair candidate,
objective, assessed findings, model and budget for old/new instructions. Retain
the candidate and supplied evidence unchanged, using isolated write workspaces
and external receipts. Assess whether regression evidence distinguishes the old
failure from the repair and covers directly affected supported variants. Count
new regressions and unnecessary scope expansion as well as repeated findings.
No such live comparison was run for this implementation, and the expected
improvement in convergence/cost remains unmeasured.

## Deterministic transport control

The public Response CLI fixture starts with Review claiming unknown ownership.
Assessment confirms the defect and independently assigns current scope with its
own rationale. Response must receive the original Review claim, the defect
rationale and the final scope/rationale together. A dismissed finding and a
confirmed finding with unknown ownership must stay excluded. Original Review
and Assessment output bytes must remain unchanged after the worker finishes.
Existing scope contract tests also exclude confirmed related work.

The same CLI tests exercise the separate validation-repair path: version 1,
failed Validation evidence, no actionable findings, and no invented Review
finding. The assessed-feedback path uses version 2. Output validation still
requires exactly one nonempty response per selected index. These checks prove
handoff and routing behavior, not the quality of the model's repair reasoning.
