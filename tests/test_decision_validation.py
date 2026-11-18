from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from evaluation.standalone.decision_validation import SCOPE, validate_continue
from lcc import ContinuityError, Scope, recover
from lcc.source import canonical_json


FIXTURES = Path(__file__).with_name("fixtures") / "decision-validation"
PREFIX = (Path(__file__).resolve().parents[1] / "evaluation" / "standalone" / "frozen"
          / "prefixes" / "stale_verification.jsonl")
ARCHIVED_HASHES = {
    "pilot-stale": "bcd13e5a5598dd9d3bf152eb7303b2ba624c281213260f2e49fad50775385f10",
    "diagnostic-stale": "56d1c5b91123e3c2aa8f71155a618bc280e91cfe408573255b7d57f0b16f1b4e",
    "diagnostic-current-pass": "3f9e96f8068bf9a9889f2fd39715d934db69d50eac5d95d9a14f94f3cb803fd2",
}


def event(event_id, kind, origin, *, scope=SCOPE, **fields):
    value = {"id": event_id, "kind": kind, "origin": origin, "scope": asdict(scope)} | fields
    return canonical_json(value).encode() + b"\n"


def start(operation="Verify-B", *, artifact="beacon.cfg", scope=SCOPE):
    return event("V-START-" + operation.removeprefix("Verify-"), "tool_started", "runtime",
                 scope=scope, operation=operation, artifact=artifact, revision="B")


def complete(operation="Verify-B", *, scope=SCOPE, verified=True):
    return event("V-PASS-" + operation.removeprefix("Verify-"), "tool_completed", "tool",
                 scope=scope, operation=operation, receipt={"suite": "beacon-settings", "passed": 4},
                 verified=verified)


class ContinueDecisionTests(unittest.TestCase):
    def setUp(self):
        temp = self.enterContext(TemporaryDirectory())
        self.source = Path(temp) / "source.jsonl"
        self.checkpoint = Path(temp) / "checkpoint.sqlite"
        self.stale = PREFIX.read_bytes()

    def commit(self, source):
        self.source.write_bytes(source)
        return recover(self.source, self.checkpoint, SCOPE)

    def decide(self, refs=(), *, proposal=None, scope=SCOPE):
        if proposal is None:
            proposal = {"next_action": "continue", "evidence_ids": list(refs)}
        return validate_continue(self.source, self.checkpoint, scope, proposal)

    def archived(self, name):
        content = (FIXTURES / (name + ".json")).read_bytes()
        self.assertEqual(hashlib.sha256(content).hexdigest(), ARCHIVED_HASHES[name])
        return json.loads(content)

    def test_archived_stale_and_passing_answers(self):
        self.commit(self.stale)
        for name in ("pilot-stale", "diagnostic-stale"):
            with self.subTest(name=name):
                proposal = self.archived(name)
                self.assertEqual(proposal["next_action"], "continue")
                self.assertEqual(proposal["state"]["current_revision"], "B")
                self.assertEqual(proposal["state"]["last_passing_revision"], "A")
                decision = self.decide(proposal=proposal)
                self.assertEqual((decision.status, decision.reason),
                                 ("blocked", "current_verification_missing_pass"))
                self.assertEqual(decision.current_revision, "B")
                self.assertEqual(decision.evidence_ids, ("V-EDIT-B", "V-START", "V-PASS"))

        control = self.stale + start() + complete()
        self.assertEqual(hashlib.sha256(control).hexdigest(),
                         "b5587e28c36192f5e44ca98ba71e29d4c8283e4c1412a8ad9c1289872e335788")
        snapshot = self.commit(control)
        database_before = self.checkpoint.read_bytes()
        files_before = sorted(self.source.parent.iterdir())
        decision = self.decide(proposal=self.archived("diagnostic-current-pass"))
        self.assertEqual(decision.status, "allowed")
        self.assertEqual(decision.current_revision, "B")
        self.assertEqual(decision.source_id, "m3-stale-verification")
        self.assertEqual(decision.cursor, snapshot.cursor)
        self.assertEqual(decision.evidence_ids, ("V-EDIT-B", "V-START-B", "V-PASS-B"))
        self.assertEqual(self.source.read_bytes(), control)
        self.assertEqual(self.checkpoint.read_bytes(), database_before)
        self.assertEqual(sorted(self.source.parent.iterdir()), files_before)

    def test_model_success_cannot_supply_a_pass(self):
        self.commit(self.stale)
        claim = {"next_action": "continue", "evidence_ids": ["V-PASS"],
                 "state": {"current_revision": "B", "last_passing_revision": "B",
                           "operation_id": "Verify-B", "operation_status": "succeeded"}}
        decision = self.decide(proposal=claim)
        self.assertEqual((decision.status, decision.reason),
                         ("blocked", "current_verification_missing_pass"))

    def test_pending_unknown_wrong_scope_and_unverified_receipts_block(self):
        foreign = Scope(SCOPE.task, "other-session", SCOPE.workflow)
        timeout = event("V-TIMEOUT-B", "tool_timeout", "runtime", operation="Verify-B")
        cases = (
            ("pending", start(), "current_verification_incomplete"),
            ("unknown", start() + timeout, "current_verification_incomplete"),
            ("unverified", start() + complete(verified=False), "current_verification_missing_pass"),
            ("wrong scope", start(scope=foreign) + complete(scope=foreign),
             "current_verification_missing_pass"),
            ("wrong artifact", start(artifact="other.cfg") + complete(),
             "current_verification_missing_pass"),
        )
        for name, tail, reason in cases:
            with self.subTest(case=name):
                checkpoint = self.source.with_name(name + ".sqlite")
                self.source.write_bytes(self.stale + tail)
                recover(self.source, checkpoint, SCOPE)
                decision = validate_continue(self.source, checkpoint, SCOPE, {"next_action": "continue"})
                self.assertEqual((decision.status, decision.reason), ("blocked", reason))
        self.assertEqual(self.decide(scope=foreign).reason, "scope_outside_policy")

    def test_references_must_support_current_proof(self):
        self.commit(self.stale + start() + complete())
        for ref in ("V-PASS", "V-ASK", "Noise-10-R", "invented"):
            with self.subTest(ref=ref):
                decision = self.decide(["V-PASS-B", ref])
                self.assertEqual((decision.status, decision.reason), ("blocked", "unsupported_references"))
                self.assertEqual(decision.unsupported_ids, (ref,))
        self.assertEqual(self.decide(["V-EDIT-B", "V-PASS-B"]).status, "allowed")
        self.assertEqual(self.decide(proposal={"next_action": "repeat_operation"}).status, "insufficient_evidence")

    def test_conflicting_receipts_do_not_allow_continue(self):
        self.commit(self.stale + start() + complete() + start("Verify-B-check")
                    + event("V-FAIL-B-check", "tool_failed", "tool", operation="Verify-B-check", receipt={}))
        decision = self.decide(["V-PASS-B"])
        self.assertEqual((decision.status, decision.reason), ("blocked", "ambiguous_current_operations"))
        self.assertIn("V-FAIL-B-check", decision.evidence_ids)
        with self.source.open("ab") as source:
            source.write(event("V-FAIL-B", "tool_failed", "tool", operation="Verify-B", receipt={}))
        with self.assertRaises(ContinuityError):
            recover(self.source, self.checkpoint, SCOPE)
        self.assertEqual(self.decide().status, "insufficient_evidence")

    def test_currentness_requires_a_consumed_source(self):
        before_b = b"".join(self.stale.splitlines(keepends=True)[:-1])
        self.commit(before_b)
        self.assertEqual(self.decide(["V-PASS"]).status, "allowed")
        self.source.write_bytes(self.stale)
        decision = self.decide(["V-PASS"])
        self.assertEqual((decision.status, decision.reason), ("insufficient_evidence", "source_not_at_checkpoint"))
        recover(self.source, self.checkpoint, SCOPE)
        self.assertEqual(self.decide(["V-PASS"]).status, "blocked")

    def test_checkpoint_projection_must_agree_with_source(self):
        self.commit(self.stale)
        with sqlite3.connect(self.checkpoint) as connection:
            row = json.loads(connection.execute("SELECT payload FROM checkpoint").fetchone()[0])
            row["projection"]["artifacts"]["beacon.cfg"] = {"revision": "A", "event_id": "V-EDIT-A"}
            payload = canonical_json(row)
            connection.execute("UPDATE checkpoint SET payload=?, sha256=?",
                               (payload, hashlib.sha256(payload.encode()).hexdigest()))
        decision = self.decide(["V-PASS"])
        self.assertEqual((decision.status, decision.reason),
                         ("insufficient_evidence", "checkpoint_projection_mismatch"))
