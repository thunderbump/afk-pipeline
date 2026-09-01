# Routing compatibility audit

The repository supports only capability routing (`schema_version: 2`). The audit
covered planner and policy inputs and outputs, the child publisher, completion
validation, parent-review fan-in, Run preparation, export normalization, fixture
producers, tests, and README examples. The separately versioned v1 publication
bundle documented by `afk_export` is not a routing contract or routing artifact.

No live producer or consumer requires the retired routing contract. Run
preparation already constructs only capability catalogs, and every in-repository
fixture producer now emits capability routes. Planner, policy, publisher, and
fan-in loaders reject a routing or Plan input whose schema version is not `2`.
The old `execution`, handoff, human-ambiguity, and v1 policy branches therefore
had no current caller and were removed rather than moved to a compatibility
module. The historical Acceptance Evidence classifier and store were also v1
routing producers and have been deleted.

No operational routing artifacts are stored in this repository, and obsolete
routing/Plan evidence cannot be admitted to a current Run. One retained-data
dependency does remain: the documented exporter accepts caller-owned historical
Run directories containing a completed v1 Preflight, including Runs that
terminally paused before Coordinator.

The retained set is exactly the otherwise exporter-admissible terminal Runs that
satisfy all of these shared Preflight bindings (not merely Runs with three
contract-valid files):

- `preparation.json` contains completed Preflight facts naming `preflight/` and
  `preflight/output.json`, with status and outcome `completed`, exit code zero,
  and a decision equal to the output decision;
- `preflight-input.json` and `preflight/input.json` are equal after validation by
  the frozen v1 input contract;
- `preflight/output.json` passes the frozen v1 output contract against that
  input, including matching source, completed outcome, and a decision derived
  from its request ledger; and
- the input/output source is the Bead identified by the validated Run
  preparation and agrees with any Bead assertion in the export request.

Within that set, a prepared Coordinator Run must have preparation status
`prepared`, pass the normal terminal Run and Coordinator evidence checks, and
bind both its preparation facts and Preflight output to `decision: proceed`. A
terminally paused Run must instead have `preparation_status: paused`, pass the
paused-Run preparation, identity, assignment, and terminal timestamp checks,
bind both decisions to `pause`, record Coordinator as `not_started` with null
exit code, outcome, and decision, and contain an existing empty `coordinator/`
directory. A directory that fails any shared or branch-specific condition is not
in the retained migration cohort. These Runs are the only external data that
depend on this compatibility; routing v1 Plans and Acceptance Evidence stores
are not accepted.

The retention window for that set ends at `2027-03-01T00:00:00Z` (exclusive).
Before that instant, owners must either export each retained Run to a Publication
Bundle v3 or migrate it to storage that does not invoke the current exporter on
the Run directory. On or after that instant, the first exporter release may
remove Preflight Run ingestion, `afk_preflight.contract`, its sanitization path,
and its regression fixtures without inventorying undisclosed caller data.
Publication Bundle readers are outside this removal condition and remain governed
by their separately documented schema support. This date and the structural set
above are the authoritative retirement condition; removal does not wait for a
future announcement or for confirmation that every caller acted.

Until that deadline, the exporter keeps only the v1 Preflight data contract,
strict ingestion, sanitized publication, and regression tests; it cannot produce
or store new Preflight evidence. Callers do not need the removed routing v1
producer or any compatibility path into current Run admission.
