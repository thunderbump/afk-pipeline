"""PROTOTYPE: pure validation for the proposed structured Review response."""

SEVERITIES = {"high", "medium", "low"}


def validate_review(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TypeError("review must be an object")
    if not isinstance(value.get("summary"), str):
        raise TypeError("review summary must be a string")
    findings = value.get("findings")
    if not isinstance(findings, list):
        raise TypeError("review findings must be an array")
    for finding in findings:
        validate_finding(finding)
    return value


def validate_finding(finding: object) -> None:
    if not isinstance(finding, dict):
        raise TypeError("each finding must be an object")
    if finding.get("severity") not in SEVERITIES:
        raise ValueError("finding severity must be high, medium, or low")
    for field in ("title", "details"):
        if not isinstance(finding.get(field), str):
            raise TypeError(f"finding {field} must be a string")
        if not finding[field].strip():
            raise ValueError(f"finding {field} must be a non-empty string")
    locations = finding.get("locations")
    if not isinstance(locations, list):
        raise TypeError("finding locations must be an array")
    if not locations:
        raise ValueError("finding locations must not be empty")
    for location in locations:
        if not isinstance(location, dict) or not isinstance(location.get("path"), str):
            raise TypeError("each finding location needs a path")
        if not location["path"].strip() or location["path"].startswith("/"):
            raise ValueError("finding location path must be repository-relative")
        line = location.get("line")
        if not isinstance(line, int) or isinstance(line, bool):
            raise TypeError("finding location line must be an integer")
        if line < 1:
            raise ValueError("finding location line must be a positive integer")
