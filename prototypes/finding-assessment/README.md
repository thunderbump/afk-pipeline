# PROTOTYPE — Review Finding Assessment

Question: what is the smallest independent JSON/file contract that lets an
agent decide whether each finding from one completed Review is worth
addressing, without allowing that assessment to perform or prescribe repair?

Drive the important terminal shapes interactively:

```sh
python3 prototypes/finding-assessment/prototype.py
```

Proposed input:

```json
{
  "schema_version": 1,
  "workspace": "/absolute/path/to/the/reviewed/checkout",
  "review_directory": "/absolute/path/to/a/completed/review",
  "timeout_seconds": 900
}
```

The Review directory is the durable source of the immutable findings and their
evidence. The workspace lets the assessor inspect the exact reviewed code. A
production component would refuse mismatched Review evidence before creating
its result directory, following the same prepared-workspace boundary as
Review.

A completed assessment contains a summary and exactly one decision for every
Review finding:

```json
{
  "summary": "One finding should be addressed.",
  "decisions": [
    {
      "finding_index": 0,
      "worth_addressing": true,
      "rationale": "The reported behavior is reachable and violates the objective."
    }
  ]
}
```

`finding_index` refers to the immutable position in the completed Review's
`findings` array. A boolean is enough for the current decision; the rationale
makes the judgement inspectable. Findings may not be skipped or duplicated.
An empty Review produces an empty decisions array. Assessment completion stays
separate from its decisions, just as Review completion stays separate from its
findings.

This prototype deliberately omits Pi invocation, lifecycle hardening,
confidence scoring, repair instructions, GitHub fields, and aggregate routing
policy. Its Python and terminal shell are prototype-only and must be deleted or
absorbed after Brian accepts the contract.
