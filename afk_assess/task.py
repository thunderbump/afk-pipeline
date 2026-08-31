"""Authoritative finding-assessment inference task contract."""

import json
from pathlib import Path

from afk_assess.contract import validate_assessment
from afk_inference import Capability, ResponseRejected, TaskContract

ASSESSMENT_INSTRUCTIONS = """Act as a read-only finding assessor. Decide whether each supplied Review finding is worth addressing. Inspect the reviewed repository and supplied evidence. Mark worth_addressing true only for a concrete, reachable problem relevant to the implementation objective. The current implementation objective is authoritative. Treat related-work records only as ownership context, and do not mark a finding worth addressing when it merely asks this objective to include work owned by a sibling task. Do not modify files or prescribe a repair. Use each finding's immutable zero-based array position as finding_index.

Return only one JSON object with this exact shape:
{"summary":"concise assessment conclusion","decisions":[{"finding_index":0,"worth_addressing":true,"rationale":"why the finding is or is not worth addressing"}]}
Return exactly one decision for every finding with no duplicates or omissions, or an empty decisions array when there are no findings. Do not wrap the JSON in Markdown."""


def build_task(
    assessment_input: dict[str, object],
    review: dict[str, object],
    objective: str,
    workspace: Path,
) -> TaskContract:
    """Build and bind the v1 Finding Assessment task."""
    review_directory = Path(assessment_input["review_directory"])
    related = assessment_input.get("related_work")
    related_records = (
        [json.loads(line) for line in Path(related["path"]).read_text().splitlines()]
        if related is not None
        else []
    )
    data = {
        "objective": objective,
        "findings": review["findings"],
        "review": review,
        "reviewed_diff": (review_directory / "diff.patch").read_text(),
        "related_work": related_records,
    }

    def validate(value: object):
        try:
            if not isinstance(value, str):
                raise TypeError("finding assessment response must be JSON text")
            return validate_assessment(review, json.loads(value))
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise ResponseRejected(str(error)) from error

    return TaskContract(
        purpose="finding_assessment",
        contract_version=1,
        trusted_instructions=ASSESSMENT_INSTRUCTIONS,
        untrusted_data=data,
        capability=Capability.READ_ONLY,
        validator=validate,
    )
