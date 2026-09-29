import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from heritage_mentor.contracts import validate_event


class ContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = json.loads((ROOT / "contracts/domain.schema.json").read_text(encoding="utf-8"))
        cls.sample = json.loads((ROOT / "data/sample.json").read_text(encoding="utf-8"))

    def test_sample_is_valid(self) -> None:
        self.assertEqual([], validate_event(self.sample, self.schema))

    def test_missing_fields_are_stable(self) -> None:
        issues = validate_event({}, self.schema)
        self.assertEqual(sorted(x.field for x in issues), [x.field for x in issues])

    def test_time_and_version_boundaries(self) -> None:
        event = dict(self.sample, occurred_at="2026-09-25T10:00:00", version=0)
        codes = {(x.field, x.code) for x in validate_event(event, self.schema)}
        self.assertIn(("occurred_at", "timezone_required"), codes)
        self.assertIn(("version", "positive_integer"), codes)

    def test_event_payload_is_required(self) -> None:
        event = dict(self.sample, event_type="MENTOR_AUTHORIZED", payload={})
        self.assertIn(("payload.scope", "required"), [(x.field, x.code) for x in validate_event(event, self.schema)])

    def test_unknown_event_is_rejected(self) -> None:
        issues = validate_event(dict(self.sample, event_type="UNKNOWN"), self.schema)
        self.assertIn(("event_type", "unsupported_value"), [(x.field, x.code) for x in issues])

    def test_payload_deadline_requires_timezone(self) -> None:
        event = dict(
            self.sample,
            event_type="MENTOR_AUTHORIZED",
            aggregate_type="mentor_grant",
            payload={
                "grant_id": "g", "mentor_id": "m", "agreement_ref": "a",
                "scope": ["teach"], "content_refs": ["c"], "grade_prerequisites": ["g3"],
                "issued_at": "2026-09-02T00:00:00+08:00",
                "expires_at": "2027-01-31T00:00:00",
            },
        )
        self.assertIn(
            ("payload.expires_at", "timezone_required"),
            [(x.field, x.code) for x in validate_event(event, self.schema)],
        )

    def test_event_must_belong_to_declared_aggregate(self) -> None:
        event = dict(
            self.sample,
            event_type="LESSON_SCHEDULED",
            aggregate_type="mentor_grant",
            payload={},
        )
        self.assertIn(
            ("aggregate_type", "aggregate_mismatch"),
            [(x.field, x.code) for x in validate_event(event, self.schema)],
        )

    def test_every_sample_journal_event_is_valid(self) -> None:
        schema = self.schema
        invalid = []
        for line_no, line in enumerate((ROOT / "data/journal.sample.jsonl").read_text(encoding="utf-8").splitlines(), 1):
            issues = validate_event(json.loads(line), schema)
            if issues:
                invalid.append((line_no, [(i.field, i.code) for i in issues]))
        self.assertEqual([], invalid)


if __name__ == "__main__":
    unittest.main()
