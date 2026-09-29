"""Optional caller-owned validation policies, shared by execution and readers."""

POLICY_FIELDS = {"termination_grace_seconds", "repairable_exit_codes"}


def validate_policy(value):
    if "termination_grace_seconds" in value:
        grace = value["termination_grace_seconds"]
        if type(grace) is not int or not 1 <= grace <= 3600:
            raise ValueError(
                "termination_grace_seconds must be an integer from 1 through 3600"
            )
    if "repairable_exit_codes" in value:
        codes = value["repairable_exit_codes"]
        if (
            not isinstance(codes, list)
            or len(codes) > 255
            or any(type(code) is not int or not 1 <= code <= 255 for code in codes)
            or len(set(codes)) != len(codes)
        ):
            raise ValueError(
                "repairable_exit_codes must be a list of unique integers from 1 through 255"
            )


def require_policy_match(recorded, expected):
    """Absence preserves legacy behavior, so removing an explicit policy is drift."""
    validate_policy(recorded)
    validate_policy(expected)
    if {k: recorded[k] for k in POLICY_FIELDS if k in recorded} != {
        k: expected[k] for k in POLICY_FIELDS if k in expected
    }:
        raise ValueError("Validation policy disagrees with configured policy")
