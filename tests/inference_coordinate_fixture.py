"""Route fake Pi calls to the semantic-role CLI fixtures."""

import os
import sys
from pathlib import Path

root = Path(__file__).parent
review_scenario, assessment_scenario, response_scenario = sys.argv[1:4]
arguments = sys.argv[4:]
prompt_argument = arguments[-1]
prompt = (
    Path(prompt_argument[1:]).read_text()
    if prompt_argument.startswith("@")
    else prompt_argument
)
rendered = " ".join([*arguments[:-1], prompt])
if "implementation reviewer" in rendered:
    fixture, scenario = root / "fixture_review_agent.py", review_scenario
elif "finding assessor" in rendered:
    fixture, scenario = root / "fixture_assessment_agent.py", assessment_scenario
elif "feedback responder" in rendered or "validation repair worker" in rendered:
    fixture, scenario = root / "fixture_response_agent.py", response_scenario
else:
    raise SystemExit("unknown inference purpose")
os.execv(sys.executable, [sys.executable, str(fixture), scenario, *arguments])
