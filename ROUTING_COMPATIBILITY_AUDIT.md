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
terminally paused before Coordinator. Concretely, the retained set is any Run
whose `preparation.json` contains a `preflight` member and whose completed
`preflight-input.json`, `preflight/input.json`, and `preflight/output.json` pass
the frozen v1 Preflight contract. Those directories are the only external data
that depend on this compatibility; routing v1 Plans and Acceptance Evidence
stores are not accepted.

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
