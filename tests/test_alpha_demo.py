"""Exercise the consolidated walkthrough through its module entry point."""

import json
from pathlib import Path
import subprocess
import sys
import unittest


class AlphaDemoTests(unittest.TestCase):
    def test_compaction_restart_and_exact_revision_gate(self):
        result = subprocess.run(
            [sys.executable, "-B", "-m", "lcc.alpha"],
            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        compact = report["compaction"]
        self.assertLess(compact["bounded_bytes"], compact["wide_bytes"])
        self.assertLessEqual(compact["bounded_bytes"], compact["ceiling_bytes"])
        self.assertEqual(compact["omitted_event_ids"], [["LOG-START", "LOG-END"]])
        restart = report["restart"]
        self.assertTrue(restart["same_projection_and_packet"])
        self.assertEqual((restart["status"], restart["start_event"]), ("pending", "V-START"))
        self.assertEqual(report["stale_verification"], {"current_revision": "B", "last_verified_revision": "A"})
        decisions = report["decisions"]
        self.assertEqual(decisions["stale_pass"]["status"], "blocked")
        self.assertEqual(decisions["uncommitted_pass"]["status"], "insufficient_evidence")
        allowed = decisions["committed_current_pass"]
        self.assertEqual(allowed["status"], "allowed")
        self.assertEqual(allowed["evidence_ids"], ["V-EDIT-B", "V-START-B", "V-PASS-B"])
