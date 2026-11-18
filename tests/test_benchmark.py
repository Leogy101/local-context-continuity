"""Regressions for benchmark oracle strictness and end-to-end evidence."""

from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from benchmarks.run import FIXTURES, _exercise, _negatives
from benchmarks.scoring import score
from lcc import Scope, build_packet, recover


class BenchmarkTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory(prefix="lcc-test-benchmark-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.fixtures = json.loads((FIXTURES / "oracle.json").read_bytes())

    def test_type_role_ordinal_and_artifact_claims_cannot_pass_the_oracle(self):
        fixture = self.fixtures["pending_tool"]
        source = FIXTURES / "pending_tool.jsonl"
        checkpoint = self.root / "checkpoint.sqlite"
        scope = Scope(**fixture["scope"])
        context = {
            "kind": "caller", "provenance": "Synthetic test",
            "context_text": "Determine the pending tool outcome.",
            "scope": fixture["scope"], "required_event_ids": ["E15"],
        }
        prefix = source.read_bytes()
        header, *lines = prefix.splitlines(keepends=True)
        events = {record["id"]: record for record in map(json.loads, lines)}
        info = {"source_id": json.loads(header)["source_id"],
                "fingerprint": sha256(header).hexdigest()}
        cursor = f"v1.{len(prefix):x}.{sha256(prefix).hexdigest()}"
        visible_context = {k: v for k, v in context.items() if k != "scope"} | {
            "status": "caller-asserted"
        }
        recover(source, checkpoint, scope)
        accepted = json.loads(build_packet(source, checkpoint, scope, context, 8192).packet)

        def evaluate(packet):
            return score(packet, fixture["after"], fixture["scope"], cursor, info,
                         events, visible_context, "lcc")

        self.assertTrue(evaluate(accepted)["passed"])
        for mutation in ("numeric_type", "role", "source_order", "artifact"):
            with self.subTest(mutation=mutation):
                modified = deepcopy(accepted)
                terminal = next(event for frame in modified["frames"]
                                for event in frame["events"] if event["id"] == "E15")
                if mutation == "numeric_type":
                    terminal["receipt"]["failed"] = False
                elif mutation == "role":
                    terminal["role"] = "user"
                elif mutation == "source_order":
                    terminal["source_order"] = 1
                else:
                    modified["artifacts"]["unsafe.cfg"] = "invented"
                self.assertIn("consistency", evaluate(modified)["failed_checks"])

    def test_late_publication_reopens_the_pending_boundary_in_another_process(self):
        cells = _exercise("interrupted_publication", self.fixtures["interrupted_publication"],
                          1, self.root)
        candidate = next(cell for cell in cells if cell["strategy"] == "lcc")
        before, after = candidate["steps"]
        self.assertEqual(candidate["outcome"], "SUCCESS")
        self.assertTrue(before["fresh_process_equal"])
        self.assertEqual(before["oracle"]["retained_facts"]["operation:Generator-9"]["actual"],
                         {"status": "pending", "start_event": "E14", "terminal_event": None})
        self.assertEqual(after["oracle"]["retained_facts"]["operation:Generator-9"]["actual"],
                         {"status": "failed", "start_event": "E14", "terminal_event": "E15"})
        self.assertEqual(candidate["recovery"]["publisher_exit"], 73)
        self.assertTrue(candidate["recovery"]["late_receipt_persisted_once"])

    def test_old_dependency_needs_actual_payload_after_multiple_reductions(self):
        cells = _exercise("old_dependency", self.fixtures["old_dependency"], 3, self.root)
        candidate = next(cell for cell in cells if cell["strategy"] == "lcc")
        self.assertEqual(candidate["outcome"], "SUCCESS")
        before, after = candidate["steps"][-2:]
        self.assertFalse(before["old_receipt_body_visible"])
        self.assertTrue(after["old_receipt_body_visible"])
        self.assertEqual(
            after["oracle"]["retained_facts"]["event:D-RECEIPT"]["actual"]["receipt"]["relay_ticket"],
            "privateer::rack/68fded8a-733b-43fe-a4c4-28a3bae41e71#[08]",
        )

    def test_refusals_preserve_the_checkpoint_and_emit_no_packet(self):
        cases = _negatives(self.fixtures, self.root)
        self.assertEqual(len(cases), 10)
        for case in cases:
            with self.subTest(case=case["case"], strategy=case["strategy"]):
                self.assertTrue(case["expectation_met"])
                self.assertTrue(case["inputs_unchanged"])
                self.assertIsNone(case.get("budget_admitted_bytes"))
