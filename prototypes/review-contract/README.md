# PROTOTYPE — Review Contract

Question: what is the smallest independent JSON file contract that lets an
agent review one successful implementation Attempt plus its passed Validation,
while keeping reviewer execution separate from the findings it reports?

Drive the important terminal shapes interactively:

```sh
python3 prototypes/review-contract/prototype.py
```

Run the fixed Pi prototype adapter against real evidence:

```sh
python3 prototypes/review-contract/prototype.py review.json /new/review-directory
```

Proposed input:

```json
{
  "schema_version": 1,
  "workspace": "/absolute/path/to/prepared/checkout",
  "attempt_directory": "/absolute/path/to/succeeded/attempt",
  "validation_directory": "/absolute/path/to/passed/validation",
  "timeout_seconds": 900
}
```

The prototype checks that the workspace, Attempt, and Validation all identify
the same reviewed `HEAD`. A completed reviewer returns a `summary` and a
possibly empty `findings` array in `output.json`. Findings do not change the
top-level execution outcome and do not authorize repair.

The prototype adapter invokes the locally installed Pi (0.84.2 in this proof)
with `gpt-5.6-sol`. That adapter is not part of the proposed input contract.
This prototype deliberately omits production lifecycle hardening and tests.
Delete it or absorb only the accepted contract before building a real Review
module.
