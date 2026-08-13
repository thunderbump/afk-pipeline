# PROTOTYPE — Validation Contract

Question: what is the smallest independent JSON file contract that lets a
repository define one authoritative validation command while the runner records
which checked-out commit it validated and rejects a command that moves `HEAD`?

Drive the three important outcomes interactively:

```sh
python3 prototypes/validation-contract/prototype.py
```

The screen shows the complete input, terminal output, stdout, and stderr after
each action. The demo uses disposable Git repositories and leaves no state
behind.

The same throwaway runner can exercise a hand-authored input directly:

```sh
python3 prototypes/validation-contract/prototype.py validation.json /new/result-directory
```

Proposed input:

```json
{
  "schema_version": 1,
  "workspace": "/absolute/path/to/prepared/checkout",
  "command": ["./scripts/validate"],
  "timeout_seconds": 1800
}
```

The workspace implicitly selects the branch or detached ref. The prototype
records before/after Git state and returns `failed` if the command exits nonzero
or changes `HEAD`. It deliberately omits production lifecycle hardening,
process-group termination, atomic writes, and tests. Delete it or absorb the
accepted contract before building the real module.
