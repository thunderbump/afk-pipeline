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
module. The historical Acceptance Evidence classification package was also a v1
routing producer. Its contract, store, exporter ingestion and publication path,
tests, configuration tombstone, and documentation have now been deleted.

No operational routing artifacts are stored in this repository. Run artifact
and export directories are caller-owned external data; the repository has no
retention or replay promise for obsolete routing/Plan evidence. Any externally
retained obsolete artifact must be read with the historical revision that
created it and cannot be admitted to a current Run. There is consequently no
blocked removal condition or temporary compatibility layer.
