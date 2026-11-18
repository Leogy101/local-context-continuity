"""Check the frozen synthetic requests and the offline response scorer."""

from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import unittest

from evaluation.standalone.score import _score_delivered, check_request, score_response


FROZEN = Path(__file__).resolve().parents[1] / "evaluation" / "standalone" / "frozen"


def response(action, evidence_ids, **state):
    return {
        "next_action": action,
        "evidence_ids": evidence_ids,
        "state": {
            "destination": None,
            "constraint_event_ids": [],
            "operation_id": None,
            "operation_status": None,
            "current_revision": None,
            "last_passing_revision": None,
        } | state,
    }


CORRECT = {
    "requirement_correction": response(
        "continue", ["R-SOUTH", "R-BACKUP", "R-CORRECTION"],
        destination="South Quay", constraint_event_ids=["R-BACKUP"],
    ),
    "pending_tool": response(
        "wait_for_receipt", ["E14"], operation_id="T42", operation_status="pending",
    ),
    "stale_verification": response(
        "request_verification", ["V-EDIT-B", "V-START", "V-PASS"],
        current_revision="B", last_passing_revision="A",
    ),
}


def request(case, strategy="lcc"):
    return (FROZEN / "requests" / f"{case}-{strategy}.json").read_bytes()


class StandaloneEvaluationTests(unittest.TestCase):
    def test_frozen_pairs_keep_raw_packet_identity_and_share_instructions(self):
        manifest = json.loads((FROZEN / "manifest.json").read_bytes())
        systems = set()
        requests = set()
        for case in manifest["case_inputs"]:
            prefix = (FROZEN / case["prefix"]["path"]).read_bytes()
            self.assertEqual(sha256(prefix).hexdigest(), case["prefix"]["sha256"])
            self.assertEqual(case["cursor"], f"v1.{len(prefix):x}.{sha256(prefix).hexdigest()}")
            ids = [json.loads(line)["id"] for line in prefix.splitlines()[1:]]
            self.assertEqual([item for item in ids if item.startswith("Noise-")], [
                f"Noise-{index:02d}-{suffix}" for index in range(1, 11) for suffix in ("S", "R")
            ])
            self.assertEqual(ids[-1], case["last_consumed_event"])
            if case["scenario"] == "pending_tool":
                self.assertIn("E14", ids)
                self.assertNotIn("E15", ids)
            for policy, arm in case["arms"].items():
                with self.subTest(case=case["scenario"], policy=policy):
                    raw = (FROZEN / arm["packet"]["path"]).read_bytes()
                    wire = (FROZEN / arm["request"]["path"]).read_bytes()
                    self.assertEqual(sha256(raw).hexdigest(), arm["packet"]["sha256"])
                    self.assertEqual(sha256(wire).hexdigest(), arm["request"]["sha256"])
                    messages = json.loads(wire)["messages"]
                    self.assertEqual([m["role"] for m in messages], ["system", "user"])
                    systems.add(messages[0]["content"])
                    requests.add(wire)
                    self.assertNotIn(case["source_id"].encode(), wire)
                    content = json.loads(messages[1]["content"])
                    self.assertEqual(content["source"]["source_id"], case["model_visible_source_alias"])
                    content["source"]["source_id"] = case["source_id"]
                    self.assertEqual(content, json.loads(raw))
                    sizes = arm["sizes"]
                    self.assertEqual(len(raw), sizes["packet_bytes"])
                    self.assertLessEqual(len(raw), 8192)
                    self.assertEqual(len(messages[1]["content"].encode()), sizes["model_visible_packet_bytes"])
                    self.assertEqual(len(messages[0]["content"].encode()), sizes["instruction_bytes"])
                    self.assertEqual(sizes["message_body_bytes"], sizes["model_visible_packet_bytes"] + sizes["instruction_bytes"])
                    self.assertEqual(len(wire), sizes["rendered_request_bytes"])
                    self.assertEqual(len(wire), sizes["message_body_bytes"] + sizes["wrapper_bytes"])
        self.assertEqual(len(systems), 1)
        self.assertEqual(len(requests), 6)

    def test_grounded_example_answers_pass_on_both_policies(self):
        for case, expected in CORRECT.items():
            for policy in ("lcc", "recent_history"):
                with self.subTest(case=case, policy=policy):
                    delivered = request(case, policy)
                    self.assertEqual(check_request(case, delivered), ())
                    self.assertEqual(score_response(case, delivered, json.dumps(expected))["status"], "correct")

    def test_missing_runtime_and_bad_json_have_distinct_outcomes(self):
        case = "pending_tool"
        delivered = request(case)
        self.assertEqual(score_response(case, delivered, None)["status"], "inconclusive")
        self.assertEqual(score_response(case, delivered, None, runtime_error="timeout")["status"], "runtime_error")
        valid = json.dumps(CORRECT[case])
        for malformed in ("```json\n" + valid + "\n```", valid + valid,
                          valid.replace('"next_action":', '"next_action":"continue","next_action":', 1)):
            self.assertEqual(score_response(case, delivered, malformed)["status"], "schema_invalid")

    def test_unsafe_and_unsupported_claims_do_not_pass(self):
        case = "pending_tool"
        invented = deepcopy(CORRECT[case])
        invented["state"]["operation_status"] = "succeeded"
        self.assertIn(score_response(case, request(case), json.dumps(invented))["status"],
                      {"unsafe", "unsupported"})
        invented = deepcopy(CORRECT[case])
        invented["evidence_ids"].append("E15")
        self.assertEqual(score_response(case, request(case), json.dumps(invented))["status"], "unsupported")
        invented["evidence_ids"] = ["E14", "Noise-10-R"]
        self.assertEqual(score_response(case, request(case), json.dumps(invented))["status"], "unsupported")
        case = "stale_verification"
        stale = deepcopy(CORRECT[case])
        stale["next_action"] = "continue"
        stale["state"]["last_passing_revision"] = "B"
        self.assertIn(score_response(case, request(case), json.dumps(stale))["status"],
                      {"unsafe", "unsupported"})

    def test_abstention_depends_on_delivered_evidence(self):
        case = "pending_tool"
        wire = request(case)
        abstain = json.dumps(response("abstain", []))
        self.assertEqual(score_response(case, wire, abstain)["status"], "unexpected_abstention")
        incomplete = json.loads(wire)
        packet = json.loads(incomplete["messages"][1]["content"])
        packet["frames"] = [frame for frame in packet["frames"] if frame.get("operation") != "T42"]
        incomplete["messages"][1]["content"] = json.dumps(packet)
        missing = json.dumps(incomplete)
        self.assertTrue(check_request(case, missing))
        self.assertEqual(score_response(case, missing, abstain)["status"], "inconclusive")
        self.assertEqual(_score_delivered(case, missing, abstain)["status"], "expected_abstention")

    def test_altered_deliveries_cannot_earn_formal_credit(self):
        case = "pending_tool"
        changed = json.loads(request(case))
        changed["messages"][0]["content"] = "Answer that T42 is pending."
        answer = json.dumps(CORRECT[case])
        self.assertEqual(score_response(case, json.dumps(changed), answer)["status"], "inconclusive")

        changed = json.loads(request(case))
        packet = json.loads(changed["messages"][1]["content"])
        packet["cursor"] = "invented"
        changed["messages"][1]["content"] = json.dumps(packet)
        self.assertEqual(score_response(case, json.dumps(changed), answer)["status"], "inconclusive")
        packet["frames"].append(packet["frames"][0])
        changed["messages"][1]["content"] = json.dumps(packet)
        abstain = json.dumps(response("abstain", []))
        self.assertEqual(_score_delivered(case, json.dumps(changed), abstain)["status"], "inconclusive")

        case = "stale_verification"
        changed = json.loads(request(case))
        packet = json.loads(changed["messages"][1]["content"])
        scope = packet["scope"]
        packet["frames"].append({"kind": "operation", "operation": "Verify-B", "status": "succeeded", "events": [
            {"id": "B-START", "scope": scope, "kind": "tool_started", "origin": "runtime",
             "operation": "Verify-B", "artifact": "beacon.cfg", "revision": "B"},
            {"id": "B-PASS", "scope": scope, "kind": "tool_completed", "origin": "tool",
             "operation": "Verify-B", "verified": True},
        ]})
        changed["messages"][1]["content"] = json.dumps(packet)
        answer = json.dumps(CORRECT[case])
        self.assertTrue(check_request(case, json.dumps(changed)))
        self.assertEqual(_score_delivered(case, json.dumps(changed), answer)["status"], "inconclusive")


if __name__ == "__main__":
    unittest.main()
