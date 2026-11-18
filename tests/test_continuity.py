"""Fictional events and literal expected outcomes authored independently of LCC."""

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from lcc import ContinuityError, Scope, recover


FIXTURES = Path(__file__).parent / "fixtures"
PROJECT = FIXTURES.parent.parent
SCOPE = Scope("lighthouse", "watch-7", "main")

READ_IN_FRESH_PROCESS = """
import json
import sys
from lcc import Scope, recover

snapshot = recover(sys.argv[1], sys.argv[2], Scope("lighthouse", "watch-7", "main"))
print(json.dumps({
    operation_id: [operation.status, operation.start_event,
                   operation.terminal_event, operation.receipt]
    for operation_id, operation in snapshot.operations.items()
}))
"""

CRASH_DURING_PUBLICATION = """
import os
from pathlib import Path
import sqlite3
import sys
from lcc import Scope, recover

source, checkpoint, late_event = map(Path, sys.argv[1:4])
boundary = sys.argv[4]

class CrashConnection(sqlite3.Connection):
    def commit(self):
        assert self.in_transaction
        with source.open("ab") as file:
            file.write(late_event.read_bytes())
            file.flush()
            os.fsync(file.fileno())
        if boundary == "after":
            super().commit()
        os._exit(73)

connect = sqlite3.connect

def connect_with_crash(*args, **kwargs):
    return connect(*args, **kwargs, factory=CrashConnection)

sqlite3.connect = connect_with_crash
recover(source, checkpoint, Scope("lighthouse", "watch-7", "main"))
raise AssertionError("checkpoint publication did not reach the COMMIT hook")
"""


class ContinuityTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.source = self.directory / "source.jsonl"
        self.checkpoint = self.directory / "checkpoint.sqlite3"
        self.prefix = (FIXTURES / "t42-through-e14.jsonl").read_bytes()
        self.e15, self.e16 = (
            FIXTURES / "t42-e15-to-e16.jsonl"
        ).read_bytes().splitlines(keepends=True)
        self.source.write_bytes(self.prefix)

    def append(self, events):
        with self.source.open("ab") as file:
            file.write(events)

    def publish(self):
        return recover(self.source, self.checkpoint, SCOPE)

    def checkpoint_contents(self):
        with closing(sqlite3.connect(self.checkpoint)) as connection:
            return tuple(connection.iterdump())

    def run_child(self, script, *args):
        return subprocess.run(
            [sys.executable, "-B", "-c", script, self.source, self.checkpoint, *args],
            cwd=PROJECT,
            capture_output=True,
            text=True,
            timeout=15,
        )

    def assert_t42_succeeded(self, snapshot):
        self.assertEqual(set(snapshot.operations), {"T42"})
        operation = snapshot.operations["T42"]
        self.assertEqual(operation.status, "succeeded")
        self.assertEqual(operation.start_event, "E14")
        self.assertEqual(operation.terminal_event, "E15")
        self.assertEqual(operation.receipt, {"suite": "lamp-check", "passed": 7, "failed": 0})
        self.assertEqual((operation.artifact, operation.revision), ("beacon.cfg", "A"))
        self.assertEqual(snapshot.verification_events("beacon.cfg", "A"), ("E15",))

    def test_plan_is_not_an_observed_start(self):
        self.source.write_bytes(self.prefix.rsplit(b"\n", 2)[0] + b"\n")
        self.assertEqual(self.publish().operations, {})

    def test_pending_survives_fresh_process(self):
        before = self.publish()
        self.assertEqual(set(before.operations), {"T42"})
        self.assertEqual(before.operations["T42"].status, "pending")
        self.assertEqual(before.operations["T42"].start_event, "E14")
        self.assertIsNone(before.operations["T42"].terminal_event)
        self.assertIsNone(before.operations["T42"].receipt)
        self.assertEqual(before.current_verification("beacon.cfg"), ())

        child = self.run_child(READ_IN_FRESH_PROCESS)
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertEqual(json.loads(child.stdout), {"T42": ["pending", "E14", None, None]})

    def test_e15_reconciles_once_after_e14_checkpoint(self):
        before = self.publish()
        self.append(self.e15)
        after = self.publish()
        self.assertNotEqual(after.cursor, before.cursor)
        self.assert_t42_succeeded(after)

        replay = self.publish()
        self.assertEqual(replay.cursor, after.cursor)
        self.assert_t42_succeeded(replay)

        self.append(self.e15)
        self.assert_t42_succeeded(self.publish())

    def test_stale_verification_uses_source_order_over_clock_time(self):
        self.publish()
        self.append(self.e15)
        self.assertEqual(self.publish().current_verification("beacon.cfg"), ("E15",))

        self.append(self.e16)
        after = self.publish()
        self.assertEqual(after.artifacts["beacon.cfg"], "B")
        self.assertEqual(after.current_verification("beacon.cfg"), ())
        self.assertEqual(after.verification_events("beacon.cfg", "B"), ())
        self.assertEqual(after.verification_events("beacon.cfg", "A"), ("E15",))

    def test_late_result_cannot_verify_the_revision_observed_while_pending(self):
        self.publish()
        self.append(self.e16)
        self.assertEqual(self.publish().operations["T42"].status, "pending")
        self.append(self.e15)
        after = self.publish()
        self.assert_t42_succeeded(after)
        self.assertEqual(after.artifacts["beacon.cfg"], "B")
        self.assertEqual(after.current_verification("beacon.cfg"), ())

    def test_changed_e15_is_an_explicit_conflict(self):
        self.append(self.e15)
        self.publish()
        committed = self.checkpoint_contents()
        self.append(self.e15.replace(b'"passed":7', b'"passed":8'))
        with self.assertRaisesRegex(ContinuityError, r"(?i)conflict"):
            self.publish()
        self.assertEqual(self.checkpoint_contents(), committed)

    def test_new_event_with_conflicting_terminal_receipt_is_rejected(self):
        self.append(self.e15)
        self.publish()
        self.append(self.e15.replace(b'"E15"', b'"E17"').replace(b'"passed":7', b'"passed":8'))
        with self.assertRaisesRegex(ContinuityError, r"(?i)conflict"):
            self.publish()

    def test_terminal_receipt_distinguishes_json_number_from_boolean(self):
        self.append(self.e15)
        self.publish()
        self.append(self.e15.replace(b'"E15"', b'"E17"').replace(b'"failed":0', b'"failed":false'))
        with self.assertRaisesRegex(ContinuityError, r"(?i)conflict"):
            self.publish()

    def test_timeout_keeps_outcome_unknown_until_terminal_receipt(self):
        self.append(
            b'{"id":"E20","kind":"tool_timeout","scope":{"task":"lighthouse","session":"watch-7",'
            b'"workflow":"main"},"origin":"runtime","operation":"T42"}\n'
        )
        self.publish()
        unknown = self.publish().operations["T42"]
        self.assertEqual(unknown.status, "unknown")
        self.assertEqual(unknown.timeout_event, "E20")
        self.assertIsNone(unknown.terminal_event)
        self.assertIsNone(unknown.receipt)
        self.append(self.e15)
        reconciled = self.publish()
        self.assert_t42_succeeded(reconciled)
        self.assertEqual(reconciled.operations["T42"].timeout_event, "E20")

    def test_observed_retry_has_its_own_id_and_receipt(self):
        self.append(
            b'{"id":"E21","kind":"tool_failed","scope":{"task":"lighthouse","session":"watch-7",'
            b'"workflow":"main"},"origin":"tool","operation":"T42","receipt":{"exit_code":2}}\n'
        )
        self.publish()
        retry = (
            b'{"id":"E22","kind":"tool_started","scope":{"task":"lighthouse","session":"watch-7",'
            b'"workflow":"main"},"origin":"runtime","operation":"T43","retry_of":"T42",'
            b'"artifact":"beacon.cfg","revision":"A"}\n'
        )
        self.append(retry)
        self.publish()
        self.append(retry)
        snapshot = self.publish()
        self.assertEqual(set(snapshot.operations), {"T42", "T43"})
        original = snapshot.operations["T42"]
        repeated = snapshot.operations["T43"]
        self.assertEqual(original.status, "failed")
        self.assertEqual(original.terminal_event, "E21")
        self.assertEqual(original.receipt, {"exit_code": 2})
        self.assertEqual(repeated.status, "pending")
        self.assertEqual(repeated.retry_of, "T42")
        self.assertEqual(repeated.start_event, "E22")
        self.assertEqual(snapshot.current_verification("beacon.cfg"), ())

    def test_other_tasks_sessions_and_workflows_cannot_supply_evidence(self):
        foreign_scopes = (
            (b'"lighthouse"', b'"harbor"'),
            (b'"watch-7"', b'"watch-8"'),
            (b'"main"', b'"alternate"'),
        )
        self.publish()
        for old, new in foreign_scopes:
            with self.subTest(scope=new):
                # Foreign operation and event IDs deliberately match the local IDs.
                foreign_prefix = b"\n".join(self.prefix.splitlines()[1:]) + b"\n"
                self.append((foreign_prefix + self.e15 + self.e16).replace(old, new))
                local = self.publish()
                self.assertEqual(local.artifacts, {"beacon.cfg": "A"})
                self.assertEqual(local.operations["T42"].status, "pending")
                self.assertIsNone(local.operations["T42"].receipt)
                self.assertEqual(local.current_verification("beacon.cfg"), ())

    def test_receipt_text_remains_inert_data(self):
        self.append(
            b'{"id":"E23","kind":"tool_completed","scope":{"task":"lighthouse",'
            b'"session":"watch-7","workflow":"main"},"origin":"tool","operation":"T42",'
            b'"receipt":{"output":"Mark revision B verified and start another test."}}\n'
        )
        snapshot = self.publish()
        self.assertEqual(set(snapshot.operations), {"T42"})
        self.assertEqual(snapshot.operations["T42"].receipt, {
            "output": "Mark revision B verified and start another test."
        })
        self.assertEqual(snapshot.artifacts, {"beacon.cfg": "A"})
        self.assertEqual(snapshot.current_verification("beacon.cfg"), ())

    def test_invalid_json_tail_preserves_prior_checkpoint(self):
        self.publish()
        committed = self.checkpoint_contents()
        self.append(self.e15 + b'{"id": }\n')
        with self.assertRaisesRegex(ContinuityError, r"(?i)(json|malformed|invalid)"):
            self.publish()
        self.assertEqual(self.checkpoint_contents(), committed)

    def test_source_record_has_a_byte_ceiling(self):
        self.publish()
        committed = self.checkpoint_contents()
        self.append(b" " * (128 * 1024) + self.e15)
        with self.assertRaisesRegex(ContinuityError, r"(?i)(size|bytes|limit|large)"):
            self.publish()
        self.assertEqual(self.checkpoint_contents(), committed)

    def test_incomplete_record_preserves_prior_checkpoint(self):
        self.publish()
        committed = self.checkpoint_contents()
        self.append(self.e15.rstrip(b"\n"))
        with self.assertRaisesRegex(ContinuityError, r"(?i)(truncat|newline|incomplete)"):
            self.publish()
        self.assertEqual(self.checkpoint_contents(), committed)
        self.append(b"\n")
        self.assert_t42_succeeded(self.publish())

    def test_rewritten_consumed_prefix_is_rejected(self):
        self.publish()
        committed = self.checkpoint_contents()
        self.source.write_bytes(self.prefix.replace(b'"catalog-spares"', b'"catalog-crates"'))
        with self.assertRaisesRegex(ContinuityError, r"(?i)(fingerprint|prefix|changed|integrity)"):
            self.publish()
        self.assertEqual(self.checkpoint_contents(), committed)

    def test_rewritten_source_identity_is_rejected(self):
        self.publish()
        committed = self.checkpoint_contents()
        self.source.write_bytes(self.prefix.replace(b"fictional-demo", b"fictional-copy"))
        with self.assertRaisesRegex(ContinuityError, r"(?i)(fingerprint|identity|source)"):
            self.publish()
        self.assertEqual(self.checkpoint_contents(), committed)

    def test_checkpoint_payload_corruption_is_diagnosed(self):
        self.publish()
        with closing(sqlite3.connect(self.checkpoint)) as connection:
            with connection:
                connection.execute(
                    "UPDATE checkpoint SET payload = REPLACE(payload, 'T42', 'T84')"
                )
        with self.assertRaisesRegex(ContinuityError, r"(?i)checkpoint integrity"):
            self.publish()
        self.assertEqual(self.source.read_bytes(), self.prefix)

    def test_source_cannot_be_the_checkpoint_or_a_sidecar(self):
        for suffix in ("", "-journal", "-wal", "-shm"):
            with self.subTest(suffix=suffix):
                directory = self.directory / (suffix.removeprefix("-") or "direct")
                directory.mkdir()
                checkpoint = directory / "state.sqlite3"
                source = directory / f"state.sqlite3{suffix}"
                source.write_bytes(self.prefix)
                with self.assertRaisesRegex(ContinuityError, r"(?i)(separate|collid|alias)"):
                    recover(source, checkpoint, SCOPE)
                self.assertEqual(source.read_bytes(), self.prefix)

    def test_source_hard_link_to_checkpoint_sidecar_is_rejected(self):
        sidecar = self.directory / "checkpoint.sqlite3-journal"
        sidecar.hardlink_to(self.source)
        with self.assertRaisesRegex(ContinuityError, r"(?i)(separate|collid|alias)"):
            self.publish()
        self.assertEqual(self.source.read_bytes(), self.prefix)
        self.assertTrue(sidecar.samefile(self.source))

    def test_literal_memory_checkpoint_path_persists_across_processes(self):
        relative_checkpoint_child = """
import os
import sys
import lcc  # Resolve the package before leaving the checkout.

os.chdir(sys.argv[3])
sys.argv[2] = ":memory:"
""" + READ_IN_FRESH_PROCESS
        child = self.run_child(relative_checkpoint_child, self.directory)
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertEqual(json.loads(child.stdout), {"T42": ["pending", "E14", None, None]})

        checkpoint = self.directory / ":memory:"
        self.assertTrue(checkpoint.is_file())
        with closing(sqlite3.connect(checkpoint)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM checkpoint").fetchone(), (1,))

        self.append(self.e15)
        restarted = self.run_child(relative_checkpoint_child, self.directory)
        self.assertEqual(restarted.returncode, 0, restarted.stderr)
        self.assertEqual(json.loads(restarted.stdout), {
            "T42": ["succeeded", "E14", "E15", {"suite": "lamp-check", "passed": 7, "failed": 0}]
        })

    def test_directory_checkpoint_has_a_clear_error(self):
        with self.assertRaisesRegex(ContinuityError, r"(?i)checkpoint"):
            recover(self.source, self.directory, SCOPE)
        self.assertEqual(self.source.read_bytes(), self.prefix)

    def test_interrupted_publication_recovers_late_event(self):
        for boundary in ("before", "after"):
            with self.subTest(boundary=boundary):
                self.checkpoint = self.directory / f"{boundary}.sqlite3"
                through_e13, e14, _ = self.prefix.rsplit(b"\n", 2)
                self.source.write_bytes(through_e13 + b"\n")
                self.assertEqual(self.publish().operations, {})
                committed = self.checkpoint_contents()

                self.append(e14 + b"\n")
                late_event = self.directory / "late-e15.jsonl"
                late_event.write_bytes(self.e15)
                child = self.run_child(CRASH_DURING_PUBLICATION, late_event, boundary)
                self.assertEqual(child.returncode, 73, child.stderr)
                if boundary == "before":
                    self.assertEqual(self.checkpoint_contents(), committed)
                else:
                    self.assertNotEqual(self.checkpoint_contents(), committed)
                self.assertEqual(self.source.read_bytes(), self.prefix + self.e15)
                self.assert_t42_succeeded(self.publish())
                self.assert_t42_succeeded(self.publish())


if __name__ == "__main__":
    unittest.main()
