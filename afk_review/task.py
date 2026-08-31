"""Authoritative implementation Review inference task contract."""

import json
from pathlib import Path

from afk_inference import Capability, ResponseRejected, TaskContract
from afk_review.contract import validate_review

REVIEW_INSTRUCTIONS = """Act as a read-only implementation reviewer. Audit the complete objective and acceptance criteria, reviewed diff, supplied Committed Change and Validation evidence, and relevant repository files. Look for concrete correctness defects, regressions, missing necessary tests, and violations of the objective. Validation passing is evidence, not proof. The current objective is authoritative. Treat related-work records only as ownership context, and do not report work owned by a sibling task as missing from the current objective. Do not modify files, propose repairs, or stop after the first defect.

Return only one JSON object with this exact shape and field order:
{"summary":"concise scope and conclusion","findings":[{"severity":"high|medium|low","title":"concise problem","details":"why it matters and when it occurs","locations":[{"path":"relative/file.py","line":1}]}],"audit":{"completed":true,"scopes":["objective","acceptance_criteria","reviewed_diff","supplied_evidence"]}}
Every finding needs a repository-relative file path and positive 1-based line in the reviewed HEAD. Use an empty findings array when there is no actionable problem. Do not add fields or wrap the JSON in Markdown."""


def build_task(
    review_input: dict[str, object],
    evidence: dict[str, object],
    diff_path: Path,
    workspace: Path,
    reviewed_head: str,
) -> TaskContract:
    """Build and bind the v1 Review prompt and deterministic validator."""
    related = review_input.get("related_work")
    related_records = (
        [json.loads(line) for line in Path(related["path"]).read_text().splitlines()]
        if related is not None
        else []
    )
    change = evidence["change"]
    data = {
        "objective": change["objective"],
        "reviewed_commits": {
            "before": change["repository"]["before"]["head"],
            "after": change["repository"]["after"]["head"],
        },
        "reviewed_diff": diff_path.read_text(),
        "committed_change": evidence["change_output"],
        "validation": {
            "input": evidence["validation_input"],
            "output": evidence["validation"],
            "stdout": evidence["validation_stdout"],
            "stderr": evidence["validation_stderr"],
        },
        "related_work": related_records,
    }

    def validate(value: object):
        try:
            if not isinstance(value, str):
                raise TypeError("review response must be JSON text")
            return validate_review(json.loads(value), workspace, reviewed_head)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise ResponseRejected(str(error)) from error

    return TaskContract(
        purpose="review",
        contract_version=1,
        trusted_instructions=REVIEW_INSTRUCTIONS,
        untrusted_data=data,
        capability=Capability.READ_ONLY,
        validator=validate,
    )
