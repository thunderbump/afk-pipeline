# Current command and retained-record boundary

The supported public process is the independent PR workflow and its optional
orchestrator. [The launcher](afk) dispatches to [current PR commands](afk_pr/__main__.py)
and [orchestration](afk_orchestrate/__main__.py). Standalone Run, continuation,
export and stage producers have been removed. Host configuration is TOML;
current command status and publication retry read saved PR jobs through its
`state_root`. No host JSON fallback or routing compatibility layer remains.

The former planner, policy, child publication, completion and parent-review
walkthroughs do not describe supported execution. The original routing audit
and its proposed migration deadline remain in Git history. They impose no
current exporter, migration or compatibility obligation.

## Retained records

Optional [metrics reports](afk_metrics/report.py) and
[metrics publication](afk_metrics/publication.py) read existing Run receipts
through [afk_records](afk_records/). This package contains only their required
read, validation, authentication and normalization closure. It includes frozen
capability-routing and Preflight record validators when required to authenticate
a retained source. Those historical schema numbers identify stored records;
they do not admit new work through the removed stage process.

Retained readers keep bounded no-follow filesystem access, frozen source and
continuation identity checks, Review receipt/context validation, digest binding
and fail-closed public-text sanitization. Static receipts and portable bundle
fixtures are preserved without rewriting their schemas or artifact bytes.
Metrics publication consumes an existing bundle and its matching source; it does
not regenerate a bundle.

[The synthetic bundle helper](tests/retained_bundle_fixture.py) exists only for
reader regressions. It does not provide a production exporter, generic schema
translator or data migration service. [Retained reader tests](tests/test_retained_records.py),
[authority and replacement guards](tests/test_retained_access.py), and
[publication controls](tests/test_metrics_publication.py) exercise this boundary.

## Validation

`./afk --help` and `./afk orchestrate --help` show the supported command surface.
`./scripts/validate` runs the existing Ruff hooks and full unittest discovery.
The removed stages no longer read `AFK_STAGE_EVIDENCE_ROOTS`, so validation no
longer clears that obsolete environment variable before running tests.
