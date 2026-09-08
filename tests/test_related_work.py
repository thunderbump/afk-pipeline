import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from afk_assess.__main__ import related_work_guidance as assessment_guidance
from afk_coordinate.__main__ import assessment_input, review_input
from afk_export import ExportError, derive_public_artifact
from afk_related_work import (
    RelatedWorkError,
    build_snapshot,
    canonical_bytes,
    reference,
    snapshot_ids,
    validate_snapshot,
)
from afk_review.__main__ import related_work_guidance as review_guidance


class RelatedWorkSnapshotTest(unittest.TestCase):
    def setUp(self):
        self.records = {
            "task": {
                "id": "task",
                "title": "Current",
                "status": "open",
                "parent": "epic",
                "blockers": [{"id": "block"}],
                "dependents": [{"id": "follow"}],
                "comments": ["private note"],
                "notes": "private task note",
                "history": [{"event": "private history"}],
                "run_notes": "private run note",
                "credential": "STRUCTURAL_CREDENTIAL_VALUE",
                "labels": ["project:private", "unrelated-label"],
            },
            "epic": {
                "id": "epic",
                "title": "Local parent",
                "parent": "root",
                "children": ["task", "sibling"],
                "notes": "do not publish",
            },
            "sibling": {
                "id": "sibling",
                "title": "Migrate callers",
                "description": "Sibling-owned migration.",
                "design": "Move each caller after the interface lands.",
            },
            "block": {"id": "block", "title": "Required first"},
            "follow": {"id": "follow", "title": "Uses current work"},
            "root": {
                "id": "root",
                "title": "Breadcrumb",
                "children": ["epic", "unrelated"],
            },
            "unrelated": {"id": "unrelated", "title": "Must not recurse"},
        }

    def test_selection_filtering_order_digest_and_validation(self):
        raw, facts = build_snapshot(self.records["task"], self.records.__getitem__)
        rows = [json.loads(line) for line in raw.splitlines()]
        self.assertEqual(
            [(row["id"], row["relationship"]) for row in rows],
            [
                ("task", "subject"),
                ("epic", "parent"),
                ("sibling", "sibling"),
                ("block", "blocker"),
                ("follow", "dependent"),
                ("root", "ancestor"),
            ],
        )
        self.assertNotIn("unrelated", raw.decode())
        self.assertNotIn("private note", raw.decode())
        self.assertNotIn("private history", raw.decode())
        self.assertNotIn("private run note", raw.decode())
        self.assertNotIn("STRUCTURAL_CREDENTIAL_VALUE", raw.decode())
        self.assertNotIn("unrelated-label", raw.decode())
        self.assertEqual(facts["sha256"], hashlib.sha256(raw).hexdigest())
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "related-work.jsonl"
            path.write_bytes(raw)
            value = reference(path, facts)
            self.assertEqual(validate_snapshot(path, value), raw)

    def test_only_blocking_dependency_types_are_selected(self):
        records = {
            "task": {
                "id": "task",
                "dependencies": [
                    {"id": "block", "dependency_type": "blocks"},
                    {"id": "related", "dependency_type": "related"},
                    {"id": "found", "dependency_type": "discovered-from"},
                ],
                "dependents": [
                    {"id": "follower", "dependency_type": "blocks"},
                    {"id": "peer", "dependency_type": "related"},
                ],
            },
            "block": {"id": "block"},
            "follower": {"id": "follower"},
        }
        raw, _ = build_snapshot(records["task"], records.__getitem__)
        self.assertEqual(
            [
                (row["id"], row["relationship"])
                for row in map(json.loads, raw.splitlines())
            ],
            [("task", "subject"), ("block", "blocker"), ("follower", "dependent")],
        )

    def test_allowed_planning_prose_is_preserved_exactly(self):
        subject = {
            "id": "task",
            "description": "Document token=ghp_example_value literally.",
            "design": "Parse -----BEGIN PRIVATE KEY----- as example text.",
            "acceptance_criteria": "Keep https://name:password@example.test intact.",
            "notes": "This field is structurally private.",
        }

        raw, facts = build_snapshot(subject, lambda _identifier: subject)
        record = json.loads(raw)

        self.assertEqual(record["description"], subject["description"])
        self.assertEqual(record["design"], subject["design"])
        self.assertEqual(record["acceptance_criteria"], subject["acceptance_criteria"])
        self.assertNotIn("notes", record)
        self.assertEqual(facts["sha256"], hashlib.sha256(raw).hexdigest())

    def test_large_epic_reserves_required_context_and_reports_omissions(self):
        records = {
            "task": {"id": "task", "parent": "epic", "blockers": ["closed-00"]},
            "epic": {"id": "epic", "children": ["task"]},
        }
        for index in range(67):
            key = f"closed-{index:02d}" if index < 59 else f"active-{index:02d}"
            records[key] = {"id": key, "status": "closed" if index < 59 else "open"}
            records["epic"]["children"].append(key)
        raw, facts = build_snapshot(records["task"], records.__getitem__)
        rows = list(map(json.loads, raw.splitlines()))
        ids = {row["id"] for row in rows}
        self.assertEqual(facts["record_count"], 64)
        self.assertTrue({"task", "epic", "closed-00"} <= ids)
        self.assertTrue({f"active-{i:02d}" for i in range(59, 67)} <= ids)
        self.assertEqual(rows[0]["selection"], {"version": 1, "omitted_records": 5})
        records["epic"]["children"].reverse()
        self.assertEqual(build_snapshot(records["task"], records.__getitem__)[0], raw)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.jsonl"
            path.write_bytes(raw)
            self.assertEqual(validate_snapshot(path, reference(path, facts)), raw)

    def test_optional_bytes_skip_whole_records_and_keep_smaller_records(self):
        records = {
            "task": {"id": "task", "parent": "epic"},
            "epic": {"id": "epic", "children": ["large", "small"]},
            "large": {"id": "large", "description": "x" * 1000},
            "small": {"id": "small", "description": "retained exactly"},
        }
        raw, _ = build_snapshot(records["task"], records.__getitem__, max_bytes=400)
        rows = list(map(json.loads, raw.splitlines()))
        self.assertEqual({row["id"] for row in rows}, {"task", "epic", "small"})
        self.assertEqual(rows[0]["selection"]["omitted_records"], 1)
        self.assertEqual(rows[-1]["description"], "retained exactly")

    def test_required_dependencies_deduplicate_before_optional_selection(self):
        records = self.records
        records["task"]["blockers"] = ["sibling", "block", "sibling"]
        raw, _ = build_snapshot(records["task"], records.__getitem__, max_records=5)
        rows = list(map(json.loads, raw.splitlines()))
        self.assertEqual(
            {row["id"] for row in rows}, {"task", "epic", "sibling", "block", "follow"}
        )
        self.assertEqual(rows[0]["selection"]["omitted_records"], 1)

    def test_active_siblings_then_nearest_ancestors_then_closed_history(self):
        records = self.records
        records["sibling"]["status"] = "closed"
        raw, _ = build_snapshot(records["task"], records.__getitem__, max_records=5)
        self.assertEqual(
            {row["id"] for row in map(json.loads, raw.splitlines())},
            {"task", "epic", "block", "follow", "root"},
        )
        records["sibling"]["status"] = "in_progress"
        raw, _ = build_snapshot(records["task"], records.__getitem__, max_records=5)
        self.assertIn(
            "sibling", {row["id"] for row in map(json.loads, raw.splitlines())}
        )
        self.assertNotIn(
            "root", {row["id"] for row in map(json.loads, raw.splitlines())}
        )

    def test_omission_metadata_is_digest_bound_and_not_a_record_identity(self):
        raw, facts = build_snapshot(
            self.records["task"], self.records.__getitem__, max_records=4
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.jsonl"
            path.write_bytes(raw)
            ref = reference(path, facts)
            self.assertEqual(snapshot_ids(ref), {"task", "epic", "block", "follow"})
            rows = list(map(json.loads, raw.splitlines()))
            rows[0]["selection"]["omitted_records"] += 1
            path.write_bytes(canonical_bytes(rows))
            with self.assertRaisesRegex(RelatedWorkError, "digest"):
                validate_snapshot(path, ref)
            for selection in (
                {"version": 2, "omitted_records": 1},
                {"version": 1, "omitted_records": True},
                {"version": 1, "omitted_records": 0},
                {"version": 1, "omitted_records": 1, "ids": ["fake"]},
            ):
                rows[0]["selection"] = selection
                changed = canonical_bytes(rows)
                path.write_bytes(changed)
                changed_ref = {
                    **ref,
                    "bytes": len(changed),
                    "sha256": hashlib.sha256(changed).hexdigest(),
                }
                with self.assertRaisesRegex(RelatedWorkError, "selection metadata"):
                    validate_snapshot(path, changed_ref)

    def test_limits_fail_closed(self):
        with self.assertRaisesRegex(RelatedWorkError, "record limit"):
            build_snapshot(
                self.records["task"], self.records.__getitem__, max_records=2
            )
        with self.assertRaisesRegex(RelatedWorkError, "byte limit"):
            build_snapshot(self.records["task"], self.records.__getitem__, max_bytes=10)

    def test_roles_share_only_the_reference_and_scope_policy(self):
        raw, facts = build_snapshot(self.records["task"], self.records.__getitem__)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "related-work.jsonl"
            path.write_bytes(raw)
            related = reference(path, facts)
            request = {
                "agent_timeout_seconds": 10,
                "related_work": related,
            }
            assignment = {"workspace": str(root)}
            review_state = {
                "history": [
                    {
                        "component": "change",
                        "outcome": "completed",
                        "directory": "03-change",
                    },
                    {
                        "component": "validation",
                        "outcome": "passed",
                        "directory": "02-validation",
                    },
                ]
            }
            review = review_input(request, assignment, review_state, root)
            assessment_state = {
                "history": [
                    {
                        "component": "review",
                        "outcome": "completed",
                        "directory": "04-review",
                    }
                ]
            }
            assessment = assessment_input(request, assignment, assessment_state, root)
            self.assertEqual(review["related_work"], related)
            self.assertEqual(assessment["related_work"], related)
            for guidance in (review_guidance(review), assessment_guidance(assessment)):
                self.assertIn("authoritative", guidance)
                self.assertIn("may omit records", guidance)
                self.assertIn("jq or rg", guidance)
                self.assertNotIn("Sibling-owned migration", guidance)

    def test_publication_uses_the_exact_validated_jsonl(self):
        for limit in (4, 64):
            with self.subTest(max_records=limit):
                self.check_publication(limit)

    def check_publication(self, limit):
        raw, facts = build_snapshot(
            self.records["task"], self.records.__getitem__, max_records=limit
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "related-work.jsonl"
            path.write_bytes(raw)
            descriptor, published = derive_public_artifact(
                {
                    "root": root,
                    "source": path.name,
                    "scope": "run",
                    "kind": "related_work",
                    "media_type": facts["media_type"],
                    "priority": 0,
                    "unsafe_path": False,
                    "validated_raw": raw,
                },
                frozenset(),
            )
            self.assertEqual(published, raw)
            self.assertEqual(descriptor["public_sha256"], facts["sha256"])
            self.assertEqual(descriptor["sanitization_status"], "unchanged")

    def test_publication_fails_if_validated_snapshot_becomes_unavailable(self):
        raw, facts = build_snapshot(self.records["task"], self.records.__getitem__)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "related-work.jsonl"
            candidate = {
                "root": root,
                "source": path.name,
                "scope": "run",
                "kind": "related_work",
                "media_type": facts["media_type"],
                "priority": 0,
                "unsafe_path": False,
                "validated_raw": raw,
            }
            path.write_bytes(raw)
            path.unlink()
            with self.assertRaisesRegex(ExportError, "cannot be published: missing"):
                derive_public_artifact(candidate, frozenset())

            path.write_bytes(raw)
            with (
                mock.patch(
                    "afk_export.read_bytes", side_effect=PermissionError("unreadable")
                ),
                self.assertRaisesRegex(ExportError, "cannot be published: unavailable"),
            ):
                derive_public_artifact(candidate, frozenset())

            path.write_bytes(raw + b" ")
            with self.assertRaisesRegex(ExportError, "snapshot changed"):
                derive_public_artifact(candidate, frozenset())


if __name__ == "__main__":
    unittest.main()
