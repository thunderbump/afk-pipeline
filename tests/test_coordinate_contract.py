import unittest

import afk_run
from afk_coordinate import __main__ as coordinate_cli
from afk_coordinate.contract import validate_output


class CoordinatorTerminalContractTest(unittest.TestCase):
    def test_executables_share_the_non_cli_terminal_contract(self):
        self.assertIs(coordinate_cli.validate_output, validate_output)
        self.assertIs(afk_run.validate_coordinator_output, validate_output)

    def test_accepts_a_topologically_valid_failed_terminal_output(self):
        output = {
            "schema_version": 1,
            "outcome": "failed",
            "failed_component": "attempt",
            "component_outcome": "failed",
            "exit_code": 1,
            "history": [
                {
                    "sequence": 1,
                    "component": "attempt",
                    "directory": "01-attempt",
                    "input_from": {"assignment": "assignment.json"},
                    "outcome": "failed",
                }
            ],
        }

        self.assertIs(validate_output(output), output)

    def test_validation_failure_remains_terminal_after_abandoned_repair(self):
        output = {
            "schema_version": 1,
            "outcome": "failed",
            "failed_component": "validation",
            "component_outcome": "failed",
            "exit_code": None,
            "history": [
                {
                    "sequence": 1,
                    "component": "attempt",
                    "directory": "01-attempt",
                    "input_from": {"assignment": "assignment.json"},
                    "outcome": "succeeded",
                },
                {
                    "sequence": 2,
                    "component": "validation",
                    "directory": "02-validation",
                    "input_from": {
                        "workspace": "assignment.json",
                        "change": "01-attempt",
                    },
                    "outcome": "failed",
                },
                {
                    "sequence": 3,
                    "component": "response",
                    "directory": "03-response",
                    "input_from": {"validation": "02-validation"},
                    "outcome": "abandoned",
                },
            ],
        }

        self.assertIs(validate_output(output), output)

    def test_rejects_terminal_output_with_forged_topology(self):
        output = {
            "schema_version": 1,
            "outcome": "completed",
            "decision": "stop",
            "history": [
                {
                    "sequence": 1,
                    "component": "iteration",
                    "directory": "01-iteration",
                    "input_from": {"assessment": "elsewhere"},
                    "outcome": "completed",
                }
            ],
        }

        with self.assertRaisesRegex(ValueError, "invalid coordinator checkpoint"):
            validate_output(output)

    def test_rejects_abandoned_invocation_as_a_terminal_failure(self):
        output = {
            "schema_version": 1,
            "outcome": "failed",
            "failed_component": "attempt",
            "component_outcome": "abandoned",
            "exit_code": None,
            "history": [
                {
                    "sequence": 1,
                    "component": "attempt",
                    "directory": "01-attempt",
                    "input_from": {"assignment": "assignment.json"},
                    "outcome": "abandoned",
                }
            ],
        }

        with self.assertRaisesRegex(ValueError, "invalid coordinator checkpoint"):
            validate_output(output)


if __name__ == "__main__":
    unittest.main()
