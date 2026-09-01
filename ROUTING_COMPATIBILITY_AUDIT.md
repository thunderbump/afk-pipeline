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
terminally paused before Coordinator. The exporter therefore keeps only the v1
Preflight data contract, strict ingestion, sanitized publication, and regression
tests; it cannot produce or store new Preflight evidence. This compatibility can
be removed after the exporter API's historical-Run support is retired and all
caller-owned Preflight Runs within its announced retention window have been
exported or migrated. Until then, callers do not need the removed routing v1
producer or any compatibility path into current Run admission.
