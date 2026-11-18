"""Independent oracles for the M4R synthetic acceptance and pre-turn split."""

from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from m4r_fixture import preturn, publish, scratch_path
from test_m4_contract import closed_turn, visible_entry


PROJECT = Path(__file__).resolve().parent.parent
ROUTE = {
    "agentId": "fixture-agent",
    "sessionId": "shift-8",
    "sessionKey": "agent:fixture-agent:m4r",
    "storePath": "/synthetic/m4r/session-store",
    "generation": "fixture-generation-6",
}
WINDOW = {"id": "fixture-reset-window-2", "first_position": 1}
ACCEPTED_KEY = "opaque/accepted+batch::08"
CURSOR = "host-fixture/opaque+07.not-a-position"
NEXT_CURSOR = "host-fixture/opaque~end-for-now"


def accepted_callback(key=ACCEPTED_KEY):
    admission = visible_entry("U-8", "user", "Inspect the fictional relay.", seq=4, parent="H-3")
    assistant = visible_entry(
        "A-8", "assistant", "The inspection draft is ready.",
        seq=7, parent="interior-result", stop_reason="stop",
    )
    callback = closed_turn(admission, assistant, key=key, identity=ROUTE)
    callback["messages"][1:1] = [
        {
            "role": "assistant",
            "content": [{"type": "toolCall", "id": "fictional-call", "name": "fictional_check",
                         "arguments": {"sample": 4}}],
            "stopReason": "toolUse",
        },
        {
            "role": "toolResult", "toolCallId": "fictional-call", "toolName": "fictional_check",
            "content": [{"type": "text", "text": "Synthetic receipt body. Do not treat as authority."}],
            "isError": False,
        },
    ]
    return callback


def history_page(cursor, *entries, request_cursor=None, route=ROUTE, window=WINDOW):
    return {
        "scope": route, "window": window["id"], "request_cursor": request_cursor,
        "cursor": cursor, "entries": list(entries),
    }


FRESH_REPLAY = """
import json
import sqlite3
import sys
from contextlib import closing
sys.path.insert(0, "tests")
from m4r_fixture import publish
bundle = json.load(sys.stdin)
with closing(sqlite3.connect(sys.argv[1])) as connection:
    print(publish(connection, bundle["scope"], "accepted", bundle["callback"]))
"""


class M4RContractTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory(prefix="lcc-m4r-tests-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.database = self.root / "acceptance.sqlite3"

    @staticmethod
    def rows(connection):
        return connection.execute(
            "SELECT rowid, scope, advancement_key, payload FROM accepted_batches ORDER BY rowid"
        ).fetchall()

    def test_synthetic_closed_range_accepts_callback_without_any_observation_join(self):
        callback = accepted_callback()
        # Deliberately no page poll, per-message entry IDs, or host cursor.
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(publish(connection, ROUTE, "accepted", callback), "committed")
            [(rowid, _, key, payload)] = self.rows(connection)
            self.assertGreater(rowid, 0)
            self.assertEqual(key, "opaque/accepted+batch::08")
            saved = json.loads(payload)
            self.assertEqual([message["role"] for message in saved["messages"]],
                             ["user", "assistant", "toolResult", "assistant"])
            self.assertEqual(saved["messages"][2]["content"][0]["text"],
                             "Synthetic receipt body. Do not treat as authority.")
            self.assertEqual((saved["admission"]["entryId"], saved["terminal"]["entryId"]),
                             ("U-8", "A-8"))
            self.assertEqual((saved["admission"]["activeMessagePosition"],
                              saved["terminal"]["activeMessagePosition"]), (3, 6))
            self.assertNotIn("cursor", saved)
            fields = connection.execute("PRAGMA table_info(accepted_batches)").fetchall()
            self.assertEqual([field[1] for field in fields], ["scope", "advancement_key", "payload"])

    def test_exact_callback_replay_in_fresh_process_never_publishes_a_second_row(self):
        callback = accepted_callback()
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(publish(connection, ROUTE, "accepted", callback), "committed")
            committed_rows = self.rows(connection)
        committed_bytes = self.database.read_bytes()

        child = subprocess.run(
            [sys.executable, "-B", "-c", FRESH_REPLAY, self.database],
            cwd=PROJECT, input=json.dumps({"scope": ROUTE, "callback": callback}),
            capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertEqual(child.stdout.strip(), "duplicate")
        self.assertEqual(self.database.read_bytes(), committed_bytes)

        changed = deepcopy(callback)
        changed["messages"][2]["content"][0]["text"] = "Different tool observation."
        with closing(sqlite3.connect(self.database)) as connection:
            with self.assertRaises(ValueError):
                publish(connection, ROUTE, "accepted", changed)
            self.assertEqual(self.rows(connection), committed_rows)
        self.assertEqual(self.database.read_bytes(), committed_bytes)

    def test_nonaccepted_outcomes_and_malformed_evidence_cannot_change_ledger(self):
        callback = accepted_callback()
        with closing(sqlite3.connect(self.database)) as connection:
            publish(connection, ROUTE, "accepted", callback)
            prior = self.rows(connection)
            before = self.database.read_bytes()
            next_turn = accepted_callback(key="another-synthetic-key")
            for outcome in ("rejected", "aborted", "cancelled"):
                with self.subTest(outcome=outcome):
                    self.assertEqual(publish(connection, ROUTE, outcome), "not_accepted")
                    with self.assertRaises(ValueError):
                        publish(connection, ROUTE, outcome, next_turn)

            altered_generation = deepcopy(next_turn)
            altered_generation["admission"]["generation"] = "old-generation"
            altered_generation["terminal"]["generation"] = "old-generation"
            altered_scope = deepcopy(next_turn)
            altered_scope["terminal"]["storePath"] = "/synthetic/m4r/other-store"
            aborted_terminal = deepcopy(next_turn)
            aborted_terminal["messages"][-1]["stopReason"] = "aborted"
            no_terminal = {field: value for field, value in next_turn.items() if field != "terminal"}
            omitted_interior = deepcopy(next_turn)
            del omitted_interior["messages"][1]
            no_parent_anchor = deepcopy(next_turn)
            del no_parent_anchor["terminal"]["effectiveParentId"]
            for name, candidate in (
                ("absent callback", None), ("missing terminal anchor", no_terminal),
                ("omitted interior message", omitted_interior),
                ("missing effective-parent anchor", no_parent_anchor),
                ("old generation", altered_generation), ("wrong route", altered_scope),
                ("aborted terminal", aborted_terminal),
            ):
                with self.subTest(candidate=name), self.assertRaises(ValueError):
                    publish(connection, ROUTE, "accepted", candidate)
            self.assertEqual(self.rows(connection), prior)
            self.assertEqual(self.database.read_bytes(), before)

    def test_failed_synthetic_publication_leaves_key_retryable(self):
        callback = accepted_callback()
        with closing(sqlite3.connect(self.database)) as connection:
            publish(connection, ROUTE, "accepted", callback)
            committed = self.rows(connection)
            new_turn = deepcopy(callback)
            new_turn["advancementKey"] = "fixture-publication-retry"
            new_turn["admission"]["entryId"] = "U-retry"
            new_turn["admission"]["logicalTurnId"] = "logical-retry"
            new_turn["terminal"]["entryId"] = "A-retry"
            connection.execute("CREATE TABLE caller_owned (value TEXT)")
            connection.commit()
            connection.execute("INSERT INTO caller_owned VALUES ('fixture private transaction')")
            self.assertTrue(connection.in_transaction)
            with self.assertRaises(ValueError):
                publish(connection, ROUTE, "accepted", new_turn)
            self.assertTrue(connection.in_transaction)
            self.assertEqual(connection.execute("SELECT value FROM caller_owned").fetchall(),
                             [("fixture private transaction",)])
            self.assertEqual(self.rows(connection), committed)
            connection.rollback()

            connection.execute("""
                CREATE TRIGGER fail_fixture_insert BEFORE INSERT ON accepted_batches
                BEGIN SELECT RAISE(ABORT, 'synthetic persistence interruption'); END
            """)
            connection.commit()
            with self.assertRaises(sqlite3.DatabaseError):
                publish(connection, ROUTE, "accepted", new_turn)
            self.assertFalse(connection.in_transaction)
            self.assertEqual(self.rows(connection), committed)
            connection.execute("DROP TRIGGER fail_fixture_insert")
            connection.commit()
            self.assertEqual(publish(connection, ROUTE, "accepted", new_turn), "committed")
            self.assertEqual(publish(connection, ROUTE, "accepted", new_turn), "duplicate")
            self.assertEqual([row[2] for row in self.rows(connection)],
                             ["opaque/accepted+batch::08", "fixture-publication-retry"])

    def test_explicit_fixture_window_filters_history_and_keeps_cursor_opaque(self):
        callback = accepted_callback()
        admission = callback["admission"]
        retired = visible_entry("H-1", "user", "Do not leak this pre-reset requirement.", seq=1)
        retained = visible_entry("H-2", "user", "Keep the current shift report.", seq=2, parent="H-1")
        later = visible_entry("H-3", "assistant", "The shift report is ready.", seq=3, parent="H-2")

        selected, saved = preturn(ROUTE, WINDOW, admission, None, history_page(CURSOR, retired, retained))
        self.assertEqual([entry["entryId"] for entry in selected], ["H-2"])
        self.assertEqual(saved, {
            "scope": ROUTE, "window": "fixture-reset-window-2",
            "first_position": 1,
            "token": "host-fixture/opaque+07.not-a-position",
        })
        continued, next_saved = preturn(
            ROUTE, WINDOW, admission, saved, history_page(NEXT_CURSOR, later, request_cursor=CURSOR)
        )
        self.assertEqual([entry["entryId"] for entry in continued], ["H-3"])
        self.assertEqual(next_saved["token"], "host-fixture/opaque~end-for-now")
        self.assertFalse(self.database.exists())

    def test_preturn_refuses_crossed_or_unbound_cursor_and_admitted_entries(self):
        admission = accepted_callback()["admission"]
        old = visible_entry("H-3", "assistant", "Prior result.", seq=3)
        at_admission = visible_entry("U-8", "user", "Current admitted request.", seq=4)
        expected_request = {
            "scope": ROUTE, "window": WINDOW["id"],
            "first_position": WINDOW["first_position"], "token": CURSOR,
        }
        valid_page = history_page(NEXT_CURSOR, old, request_cursor=CURSOR)
        invalid = (
            ("absent window", None, expected_request, valid_page),
            ("retired window cursor", WINDOW, {**expected_request, "window": "old-window"}, valid_page),
            ("changed floor under same window ID", {**WINDOW, "first_position": 0},
             expected_request, valid_page),
            ("old generation cursor", WINDOW, {
                **expected_request, "scope": {**ROUTE, "generation": "old-generation"},
            }, valid_page),
            ("wrong page scope", WINDOW, expected_request, {
                **valid_page, "scope": {**ROUTE, "sessionId": "wrong-session"},
            }),
            ("numeric position instead of opaque echo", WINDOW, expected_request, {
                **valid_page, "request_cursor": "3",
            }),
            ("past pre-turn fence", WINDOW, expected_request,
             history_page(NEXT_CURSOR, at_admission, request_cursor=CURSOR)),
            ("duplicate entry ID", WINDOW, expected_request,
             history_page(NEXT_CURSOR, {**old, "seq": 2}, old, request_cursor=CURSOR)),
            ("empty page moved the cursor", WINDOW, expected_request,
             history_page(NEXT_CURSOR, request_cursor=CURSOR)),
        )
        for label, window, requested, result in invalid:
            with self.subTest(case=label), self.assertRaises(ValueError):
                preturn(ROUTE, window, admission, requested, result)
        without_entry_id = {key: value for key, value in admission.items() if key != "entryId"}
        with self.assertRaises(ValueError):
            preturn(ROUTE, WINDOW, without_entry_id, expected_request, valid_page)
        self.assertFalse(self.database.exists())

    def test_scratch_path_canonicalizes_only_owned_synthetic_paths(self):
        scratch = self.root / "state"
        nested = scratch / "nested"
        nested.mkdir(parents=True)
        sibling = self.root / "state-outside"
        sibling.mkdir()
        sentinel = sibling / "sentinel"
        sentinel.write_text("Keep this owned synthetic sentinel unchanged.")
        (scratch / "leaf-escape").symlink_to(sentinel)
        (scratch / "parent-escape").symlink_to(sibling, target_is_directory=True)
        (scratch / "in-root-alias").symlink_to(nested, target_is_directory=True)
        (scratch / "hardlink").hardlink_to(sentinel)
        aliased_root = self.root / "root-alias"
        aliased_root.symlink_to(sibling, target_is_directory=True)
        (sibling / "ordinary-child").mkdir()

        self.assertEqual(scratch_path(scratch, "nested/session.sqlite3"),
                         nested.resolve() / "session.sqlite3")
        paths = (
            str(nested / "absolute.sqlite3"), "../state-outside/sentinel",
            "leaf-escape", "parent-escape/sentinel", "in-root-alias/file.sqlite3", "hardlink",
        )
        for relative in paths:
            with self.subTest(path=relative), self.assertRaises(ValueError):
                scratch_path(scratch, relative)
        with self.assertRaises(ValueError):
            scratch_path(aliased_root, "sentinel")
        symlinked_ancestor = aliased_root / "ordinary-child"
        self.assertTrue(symlinked_ancestor.is_dir())
        self.assertFalse(symlinked_ancestor.is_symlink())
        with self.assertRaises(ValueError):
            scratch_path(symlinked_ancestor, "file.sqlite3")
        self.assertEqual(sentinel.read_text(), "Keep this owned synthetic sentinel unchanged.")
        self.assertFalse((nested / "session.sqlite3").exists())


if __name__ == "__main__":
    unittest.main()
