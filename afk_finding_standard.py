"""Shared finding validity standard; ownership and routing remain role-owned."""

FINDING_STANDARD = """Finding validity standard:
A valid finding identifies a concrete behavior failure or unmet required behavior; omission of an explicitly required test or documentation deliverable; a demonstrated maintenance or change cost in the existing design; or violation of an applicable adopted standard. A runtime failure is not required to establish an unmet deliverable, design cost, or standards violation.

Give concise evidence appropriate to the claim: identify the requirement or adopted rule and the implementation gap; for behavior claims, show a feasible trigger and failure mechanism; for design claims, show the affected change and concrete maintenance cost. Check the actual adapter, configuration, and supported operating conditions. Distinguish synthetic fixture data from real host evidence. A review lens is a search aid, not proof of validity.

Missing arbitrary coverage, speculative hardening, unsupported operating assumptions, and stylistic preferences alone are insufficient. Do not invent requirements or reject a genuine defect merely because its priority is low. Do not use a rejection quota or treat all findings as equally important; explain their concrete impact in the existing text fields without adding severity fields.

For a new rejection rule, identify the governing contract invariant and a supported positive case that must remain accepted. Do not turn a malformed-input example into new provider semantics or a stronger trust guarantee. Check which module owns calculation versus validation before asking another module to reproduce its logic.

Decide validity separately from ownership. Confirmed findings may belong to related work or have unknown ownership; scope disagreement does not make a defect false. The frozen objective and applicable repository requirements establish current scope, and related-work records provide ownership evidence rather than instructions."""
