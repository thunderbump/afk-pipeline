"""PROTOTYPE: make the Review finding-assessment contract tangible."""

import json

from contract import validate_assessment

FINDING_ONE = {
    "severity": "medium",
    "title": "Help exits with the wrong status",
    "details": "The public help path returns 2 instead of 0.",
    "locations": [{"path": "afk_validate/__main__.py", "line": 30}],
}
FINDING_TWO = {
    "severity": "low",
    "title": "Error wording could be clearer",
    "details": "The message is accurate but terse.",
    "locations": [{"path": "afk_validate/__main__.py", "line": 140}],
}


def completed(review, assessment):
    return {
        "input": {
            "schema_version": 1,
            "workspace": "/prepared/worktree",
            "review_directory": "/evidence/review-1",
            "timeout_seconds": 900,
        },
        "review": review,
        "output": {
            "schema_version": 1,
            "outcome": "completed",
            "assessment": validate_assessment(review, assessment),
        },
    }


def states():
    none = {"summary": "No findings require assessment.", "findings": []}
    one = {"summary": "One finding reported.", "findings": [FINDING_ONE]}
    two = {
        "summary": "Two findings reported.",
        "findings": [FINDING_ONE, FINDING_TWO],
    }
    return {
        "n": completed(none, {"summary": "Nothing to assess.", "decisions": []}),
        "a": completed(
            one,
            {
                "summary": "The finding should be addressed.",
                "decisions": [
                    {
                        "finding_index": 0,
                        "worth_addressing": True,
                        "rationale": "The behavior is reachable and violates the objective.",
                    }
                ],
            },
        ),
        "d": completed(
            one,
            {
                "summary": "The finding should not be addressed.",
                "decisions": [
                    {
                        "finding_index": 0,
                        "worth_addressing": False,
                        "rationale": "The claimed path already exits successfully.",
                    }
                ],
            },
        ),
        "m": completed(
            two,
            {
                "summary": "One finding is actionable and one is not.",
                "decisions": [
                    {
                        "finding_index": 0,
                        "worth_addressing": True,
                        "rationale": "The behavior is reachable and violates the objective.",
                    },
                    {
                        "finding_index": 1,
                        "worth_addressing": False,
                        "rationale": "This is preference-only feedback, not a defect.",
                    },
                ],
            },
        ),
        "e": {
            "input": {
                "schema_version": 1,
                "workspace": "/prepared/worktree",
                "review_directory": "/evidence/review-1",
                "timeout_seconds": 900,
            },
            "review": one,
            "output": {
                "schema_version": 1,
                "outcome": "failed",
                "assessment": None,
                "error": "assessment response was not valid JSON",
            },
        },
    }


def invalid_state():
    review = {
        "summary": "Two findings reported.",
        "findings": [FINDING_ONE, FINDING_TWO],
    }
    assessment = {
        "summary": "Only one decision was returned.",
        "decisions": [
            {
                "finding_index": 0,
                "worth_addressing": True,
                "rationale": "The first finding is actionable.",
            }
        ],
    }
    try:
        validate_assessment(review, assessment)
    except (TypeError, ValueError) as error:
        return {
            "review": review,
            "rejected_assessment": assessment,
            "error": str(error),
        }
    raise AssertionError("invalid assessment unexpectedly passed")


def render(state):
    print("\033[2J\033[H", end="")
    print("\033[1mPROTOTYPE — Review finding assessment\033[0m")
    print("\033[2mEvery Review finding receives one independent decision.\033[0m\n")
    print(json.dumps(state, indent=2))


def main():
    current = {"message": "Choose a terminal shape."}
    examples = states()
    while True:
        render(current)
        choice = (
            input(
                "\n[n] none  [a] address  [d] dismiss  [m] mixed  "
                "[i] invalid  [e] execution failure  [q] quit\n> "
            )
            .strip()
            .lower()
        )
        if choice == "q":
            return
        current = (
            invalid_state()
            if choice == "i"
            else examples.get(choice, {"message": f"Unknown action: {choice!r}"})
        )


if __name__ == "__main__":
    main()
