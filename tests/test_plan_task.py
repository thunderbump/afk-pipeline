import copy
import json
import unittest

from afk_inference import ResponseRejected
from afk_plan.contract import validate_direct_routing, validate_plan
from afk_plan.task import build_task
from tests.test_plan_contract import capability_input


class FrozenCriterionTaskTest(unittest.TestCase):
    def task(self, text):
        request = capability_input()
        request["parent"]["acceptance_criteria"] = text
        return request, build_task(request)

    def proposal(self, task):
        criteria = [
            {"id": item["id"], "statement": "Verify scanner failures safely."}
            for item in task.untrusted_data["source_criteria"]
        ]
        return {
            "schema_version": 2,
            "decision": "direct",
            "criteria": criteria,
            "direct_routes": [
                {
                    "criterion": item["id"],
                    "project": "afk-pipeline",
                    "owner": "AFK Run",
                    "phase": "implementation",
                    "executor": "afk_run",
                    "evidence_route": "pipeline_run",
                }
                for item in criteria
            ],
            "children": [],
            "ambiguities": [],
        }

    def test_model_paraphrase_cannot_change_frozen_source(self):
        text = "1. Test scanner failure.\n2. Keep last-good data."
        request, task = self.task(text)
        routing, plan = task.validator(json.dumps(self.proposal(task)))
        self.assertIsNone(plan)
        self.assertEqual("".join(c["source_text"] for c in routing["criteria"]), text)
        self.assertIn("scanner failures", routing["criteria"][0]["statement"])
        self.assertIn("scanner failure.", routing["criteria"][0]["source_text"])
        validate_direct_routing(request, routing)
        changed = copy.deepcopy(request)
        changed["parent"]["acceptance_criteria"] += " Another requirement."
        with self.assertRaises(ValueError):
            validate_direct_routing(changed, routing)

    def test_source_units_preserve_every_character_and_boundaries(self):
        cases = [
            ("  1. First.\n     More detail.\n  2. Second.\n", 2),
            ("1) First.\n  1. Nested detail.\n2) Second.", 2),
            ("Free form. Another requirement.\nMore detail.", 1),
            ("Introduction.\n1. First.\n2. Second.", 1),
            ("1. First.\n3. Missing second.", 1),
            ("- First.\n- Second.", 1),
            ("\n".join(f"{i}. Requirement." for i in range(1, 130)), 1),
        ]
        for text, count in cases:
            with self.subTest(text=text):
                _, task = self.task(text)
                units = task.untrusted_data["source_criteria"]
                self.assertEqual(len(units), count)
                self.assertEqual("".join(c["source_text"] for c in units), text)
                routing, _ = task.validator(json.dumps(self.proposal(task)))
                self.assertEqual(
                    "".join(c["source_text"] for c in routing["criteria"]), text
                )

    def test_unknown_duplicate_missing_reordered_and_quoted_criteria_are_rejected(self):
        _, task = self.task("1. First.\n2. Second.")
        for kind in ("unknown", "duplicate", "missing", "reordered", "quoted"):
            value = self.proposal(task)
            if kind == "unknown":
                value["criteria"][0]["id"] = "criterion-3"
            elif kind == "duplicate":
                value["criteria"][1]["id"] = "criterion-1"
            elif kind == "missing":
                value["criteria"].pop()
            elif kind == "reordered":
                value["criteria"].reverse()
            else:
                value["criteria"][0]["source_text"] = "Test scanner failures."
            with self.subTest(kind=kind), self.assertRaises(ResponseRejected):
                task.validator(json.dumps(value))

    def test_decomposition_keeps_canonical_plan_and_route_coverage(self):
        request, task = self.task("1. Implement.\n2. Verify.")
        value = self.proposal(task)
        value.update(
            decision="decompose",
            direct_routes=[],
            children=[
                {
                    "local_id": "implementation",
                    "title": "Implement and verify",
                    "objective": "Implement and verify",
                    "criteria": ["criterion-1"],
                    "project": "afk-pipeline",
                    "owner": "AFK Run",
                    "phase": "implementation",
                    "executor": "afk_run",
                    "evidence_route": "pipeline_run",
                    "depends_on": [],
                }
            ],
        )
        value["children"].append(
            {
                "local_id": "verify",
                "title": "Verify on host",
                "objective": "Verify on host",
                "criteria": ["criterion-2"],
                "project": "afk-pipeline",
                "owner": "Caller automation",
                "phase": "closure",
                "executor": "caller_agent",
                "evidence_route": "external_check",
                "depends_on": ["implementation"],
            }
        )
        _, plan = task.validator(json.dumps(value))
        validate_plan(request, plan)
        value["children"][0]["criteria"].pop()
        with self.assertRaises(ResponseRejected):
            task.validator(json.dumps(value))
