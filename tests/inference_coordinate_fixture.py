"""Route fake Pi calls to the semantic-role CLI fixtures."""

import os
import sys
from pathlib import Path

root = Path(__file__).parent
review_scenario, assessment_scenario = sys.argv[1:3]
arguments = sys.argv[3:]
rendered = " ".join(arguments)
if "implementation reviewer" in rendered:
    fixture, scenario = root / "fixture_review_agent.py", review_scenario
elif "finding assessor" in rendered:
    fixture, scenario = root / "fixture_assessment_agent.py", assessment_scenario
else:
    raise SystemExit("unknown inference purpose")
os.execv(sys.executable, [sys.executable, str(fixture), scenario, *arguments])
