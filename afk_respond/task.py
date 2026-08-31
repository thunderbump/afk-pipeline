"""Authoritative feedback-response inference task contracts."""

import json
from pathlib import Path

from afk_inference import Capability, ResponseRejected, TaskContract
from afk_respond.contract import validate_response

RESPONSE_INSTRUCTIONS = """Act as an implementation feedback responder. Modify only the prepared workspace to address every supplied actionable assessed finding, create a clean Git commit, and return the required JSON response. Do not address dismissed findings, run external orchestration, or publish feedback.

Return only one JSON object with this exact shape:
{"summary":"concise description of the completed response","finding_responses":[{"finding_index":0,"response":"what changed for this finding"}]}
Return exactly one response for every supplied finding_index, with no duplicate or omitted indices. Do not wrap the JSON in Markdown."""

REPAIR_INSTRUCTIONS = """Act as a repository validation repair worker. Modify only the prepared workspace to repair the supplied ordinary failed Validation, create a clean Git commit, and return the required JSON response. This is failed Validation evidence, not an accepted Review finding. Do not invent a Review finding, run external orchestration, or publish feedback.

Return only one JSON object with this exact shape:
{"summary":"concise description of the validation repair","finding_responses":[]}
Do not wrap the JSON in Markdown."""


def build_task(
    response_input: dict[str, object],
    selected: list[dict[str, object]],
    objective: str,
) -> TaskContract:
    """Select, build, and bind the v1 response or validation-repair task."""
    repair = "validation_directory" in response_input
    data = {"objective": objective, "actionable_findings": selected}
    if repair:
        validation_directory = Path(response_input["validation_directory"])
        data["failed_validation"] = {
            "directory": str(validation_directory),
            "input": json.loads((validation_directory / "input.json").read_text()),
            "output": json.loads((validation_directory / "output.json").read_text()),
            "stdout": (validation_directory / "stdout.log").read_text(),
            "stderr": (validation_directory / "stderr.log").read_text(),
        }

    def validate(value: object):
        try:
            if not isinstance(value, str):
                raise TypeError("feedback response must be JSON text")
            return validate_response(selected, json.loads(value))
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise ResponseRejected(str(error)) from error

    return TaskContract(
        purpose="feedback_response",
        contract_version=1,
        trusted_instructions=REPAIR_INSTRUCTIONS if repair else RESPONSE_INSTRUCTIONS,
        untrusted_data=data,
        capability=Capability.WRITE,
        validator=validate,
    )
