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

## Historical transport control

The retired standalone Response CLI fixture started with Review claiming unknown
ownership. Assessment confirmed the defect and independently assigned current
scope with its own rationale. Its expected Response input contained the original
Review claim, the defect rationale and the final scope/rationale together.
Dismissed findings and confirmed findings with unknown or related ownership were
excluded; original Review and Assessment bytes stayed unchanged. Those
standalone fixture and scope tests have retired.

Those retired CLI tests also exercised the separate validation-repair path,
version 1, with failed Validation evidence, no actionable findings and no
invented Review finding. The assessed-feedback path used version 2. Its output
contract required exactly one nonempty response per selected index. These
historical checks described handoff and routing behavior, not the quality of
the model's repair reasoning. Current PR `respond` reads the captured PR story
and does not depend on standalone Review/Assessment stages.
