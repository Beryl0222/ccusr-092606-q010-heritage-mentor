import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from heritage_mentor.domain import DomainError  # noqa: E402
from heritage_mentor.store import EventStore  # noqa: E402

SCHEMA_PATH = ROOT / "contracts/domain.schema.json"
T0 = "2026-09-01T09:00:00+08:00"


def agreement(event_id="e1") -> dict:
    return {
        "type": "version_agreement", "event_id": event_id,
        "agreement_ref": "A1", "terms_version": 1,
        "effective_from": "2026-09-01T00:00:00+08:00", "status": "active",
        "occurred_at": T0,
    }


class EventStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "journal.jsonl"

    def store(self) -> EventStore:
        return EventStore(self.path, self.schema)

    def test_replay_restores_state_after_restart(self) -> None:
        store = self.store()
        store.append_decision(agreement())
        reopened = self.store()  # 模拟服务重启
        self.assertIn("A1", reopened.state.agreements)
        self.assertEqual(1, reopened.state.aggregate_versions[("partnership_agreement", "A1")])

    def test_idempotent_command_writes_once(self) -> None:
        command = {**agreement(), "idempotency_key": "k1"}
        store = self.store()
        self.assertEqual(1, len(store.append_decision(command)))
        self.assertEqual(0, len(store.append_decision(command)))
        self.assertEqual(1, len(self.path.read_text(encoding="utf-8").splitlines()))

    def test_rejected_command_leaves_journal_untouched(self) -> None:
        store = self.store()
        store.append_decision(agreement())
        bad = {
            "type": "authorize_grant", "event_id": "bad1", "grant_id": "G1",
            "mentor_id": "MISSING", "agreement_ref": "A1",
            "scope": ["teach"], "content_refs": ["C1"], "grade_prerequisites": ["g3"],
            "issued_at": "2026-09-02T00:00:00+08:00", "expires_at": "2027-01-31T00:00:00+08:00",
            "occurred_at": T0,
        }
        with self.assertRaises(DomainError):
            store.append_decision(bad)
        self.assertEqual(1, len(self.path.read_text(encoding="utf-8").splitlines()))

    def test_every_persisted_line_is_schema_valid(self) -> None:
        store = self.store()
        store.append_decision(agreement())
        for line in self.path.read_text(encoding="utf-8").splitlines():
            event = json.loads(line)
            self.assertTrue(event["event_id"])
            self.assertEqual(1, event["version"])

    def test_corrupt_journal_is_rejected_on_load(self) -> None:
        self.store().append_decision(agreement())
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write('{"event_id": "x", "event_type": "NOPE"}\n')
        with self.assertRaises(DomainError) as ctx:
            self.store()
        self.assertEqual("journal_event_invalid", ctx.exception.code)


if __name__ == "__main__":
    unittest.main()
