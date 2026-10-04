"""Retained-record validation policy validation and projection."""

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
