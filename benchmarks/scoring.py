"""Score visible packet facts against a fixture-side oracle and source prefix."""

from collections import Counter
from json import dumps

_OPERATION_KINDS = {"tool_started", "tool_completed", "tool_failed", "tool_timeout"}
_ANNOTATIONS = {"source_order", "receipt_trust", "echo_of", "receipt_masked", "mask_reason"}


def _equal(left, right):
    return dumps(left, sort_keys=True) == dumps(right, sort_keys=True)


def _excerpt(actual, expected):
    if isinstance(expected, dict) and isinstance(actual, dict):
        return {key: _excerpt(actual[key], value) for key, value in expected.items() if key in actual}
    return actual


def _status(start, terminal, timeout):
    if terminal:
        return "succeeded" if terminal["kind"] == "tool_completed" else "failed"
    if timeout:
        return "unknown"
    return "pending" if start else None


def _operation(start, terminal, timeout):
    facts = {
        "status": _status(start, terminal, timeout),
        "start_event": start["id"] if start else None,
        "terminal_event": terminal["id"] if terminal else None,
        "timeout_event": timeout["id"] if timeout else None,
    }
    if start:
        facts.update({key: start[key] for key in ("artifact", "revision", "retry_of") if key in start})
    return facts


def _pass(operation, start, terminal, artifacts):
    return {
        "operation": operation,
        "artifact": start["artifact"],
        "revision": start["revision"],
        "start_event": start["id"],
        "terminal_event": terminal["id"],
        "current": artifacts.get(start["artifact"]) == start["revision"],
    }


def score(packet, oracle, scope, cursor, source_info, source_events, context, strategy):
    """Read states from the output. Use canonical data only to validate claims."""
    checks = {}
    actual_facts = {}

    def fact(name, expected, value):
        actual = _excerpt(value, expected)
        actual_facts[name] = {"expected": expected, "actual": actual}
        checks[name] = _equal(actual, expected)

    fact("scope", scope, packet["scope"])
    fact("cursor", cursor, packet["cursor"])
    fact("schema", "lcc-packet/1", packet["schema"])
    fact("packet_accounting", {"stage": "serialized_resume_packet", "unit": "utf8_bytes"},
         packet["accounting"])
    fact("source", source_info | {"coverage": "committed-prefix"}, packet["source"])
    fact("task_context", context, packet["task_context"])

    represented = Counter()
    visible = {}
    artifacts = {}
    operations = {}
    passes = {}
    observations = []
    masked = []
    issues = []
    order = {event_id: position for position, event_id in enumerate(source_events, 1)}
    operation_events = {}
    for event_id, event in source_events.items():
        if event["kind"] in _OPERATION_KINDS:
            operation_events.setdefault(event["operation"], []).append(event_id)

    anchors = []
    for frame in packet["frames"]:
        records = frame["events"]
        positions = [order.get(event["id"], 0) for event in records]
        if not positions or positions != sorted(positions):
            issues.append("out-of-order or empty frame")
        if positions:
            anchors.append(positions[0])
        for event in records:
            event_id = event["id"]
            represented[event_id] += 1
            visible[event_id] = event
            original = source_events.get(event_id)
            if original is None or original["scope"] != scope:
                issues.append(f"wrong-scope or invented event {event_id}")
                continue
            expected = original.copy()
            if event.get("receipt_masked") is True:
                masked.append(event_id)
                if (strategy != "observation_masking" or "receipt" not in original or "receipt" in event
                        or event.get("mask_reason") != "older-observation" or "echo_of" in event):
                    issues.append(f"invalid observation mask {event_id}")
                expected.pop("receipt", None)
            elif "echo_of" in event:
                first = source_events.get(event["echo_of"], {})
                if (not any(item["id"] == event["echo_of"] for item in records)
                        or first.get("operation") != original.get("operation")
                        or first.get("kind") != original["kind"]
                        or not _equal(first.get("receipt"), original.get("receipt"))
                        or bool(first.get("verified")) != bool(original.get("verified"))
                        or "receipt" in event or "verified" in event):
                    issues.append(f"invalid terminal or timeout echo {event_id}")
                expected.pop("receipt", None)
                expected.pop("verified", None)
            stripped = {key: value for key, value in event.items() if key not in _ANNOTATIONS}
            if not _equal(stripped, expected) or not _equal(event.get("source_order"), order[event_id]):
                issues.append(f"changed source event {event_id}")
            if "receipt" in event and event.get("receipt_trust") != "untrusted":
                issues.append(f"promoted tool receipt {event_id}")
            if event["kind"] == "artifact_observed":
                observations.append(event)
    if anchors != sorted(anchors):
        issues.append("out-of-order frame anchors")

    for event in sorted(observations, key=lambda event: event["source_order"]):
        artifacts[event["artifact"]] = event["revision"]
    for artifact, revision in packet["artifacts"].items():
        if artifacts.get(artifact) != revision:
            issues.append(f"contradictory artifact projection {artifact}")
        artifacts[artifact] = revision

    for frame in packet["frames"]:
        if frame["kind"] != "operation":
            if any(event["kind"] in _OPERATION_KINDS for event in frame["events"]):
                issues.append("ungrouped operation evidence")
            continue
        op_id = frame["operation"]
        records = [event for event in frame["events"] if "echo_of" not in event]
        start = next((event for event in records if event["kind"] == "tool_started"), None)
        terminal = next((event for event in records if event["kind"] in {"tool_completed", "tool_failed"}), None)
        timeout = next((event for event in records if event["kind"] == "tool_timeout"), None)
        facts = _operation(start, terminal, timeout)
        if (not records or start is not records[0] or not start
                or any(event.get("operation") != op_id for event in records)
                or frame["status"] != facts["status"] or op_id in operations
                or [event["id"] for event in frame["events"]] != operation_events.get(op_id)):
            issues.append(f"incoherent operation frame {op_id}")
        operations[op_id] = facts
        if start and terminal and terminal["kind"] == "tool_completed" and terminal.get("verified"):
            passes[op_id] = _pass(op_id, start, terminal, artifacts)

    for omission in packet["omitted"]:
        ids = omission["event_ids"]
        represented.update(ids)
        first = source_events.get(ids[0], {}) if ids else {}
        if first.get("kind") in _OPERATION_KINDS and ids != operation_events.get(first["operation"]):
            issues.append(f"partial operation omission {first['operation']}")
        receipt = omission.get("operation_receipt")
        if receipt:
            op_id = receipt["id"]
            start = source_events.get(ids[receipt["start_event_index"]], {})
            terminal = source_events.get(ids[receipt["terminal_event_index"]], {})
            facts = {"status": receipt["status"], "start_event": start.get("id"),
                     "terminal_event": terminal.get("id")}
            facts.update({key: receipt[key] for key in ("artifact", "revision", "retry_of")
                          if key in receipt})
            if (op_id in operations or start.get("kind") != "tool_started"
                    or terminal.get("kind") not in {"tool_completed", "tool_failed"}
                    or start.get("operation") != op_id or terminal.get("operation") != op_id
                    or not _equal(facts, _excerpt(_operation(start, terminal, None), facts))):
                issues.append(f"incoherent compact receipt {op_id}")
            operations[op_id] = facts

    claims = set()
    for entry in packet["verification"]:
        op_id = entry["operation"]
        start = source_events.get(entry["start_event"], {})
        terminal = source_events.get(entry["terminal_event"], {})
        if (not terminal.get("verified") or terminal.get("kind") != "tool_completed"
                or start.get("kind") != "tool_started" or start.get("operation") != op_id
                or terminal.get("operation") != op_id or "artifact" not in start
                or not _equal(_pass(op_id, start, terminal, artifacts), entry)
                or op_id in passes and not _equal(passes[op_id], entry)
                or op_id in claims):
            issues.append(f"unsubstantiated verification {op_id}")
        passes[op_id] = entry
        claims.add(op_id)

    # Omitted IDs establish coverage, never the contents of a required event.
    fact("coverage", sorted(source_events), sorted(represented))
    fact("duplicate_event_references", [], sorted(key for key, count in represented.items() if count > 1))
    fact("consistency", [], issues)
    for key, expected in oracle.get("events", {}).items():
        fact(f"event:{key}", expected, visible.get(key))
    for key in oracle.get("forbidden_events", []):
        fact(f"forbidden_event:{key}", False, key in visible)
    for key in oracle.get("references", []):
        fact(f"reference:{key}", True, key in represented)
    for key, expected in oracle.get("operations", {}).items():
        fact(f"operation:{key}", expected, operations.get(key))
    for key, expected in oracle.get("artifacts", {}).items():
        fact(f"artifact:{key}", expected, artifacts.get(key))
    if "verification" in oracle:
        key = lambda entry: entry["operation"]
        fact("verification", sorted(oracle["verification"], key=key), sorted(passes.values(), key=key))
    for event_id in context["required_event_ids"]:
        event = visible.get(event_id, {})
        fact(f"required_payload:{event_id}", True,
             bool(event) and "receipt_masked" not in event and "echo_of" not in event)

    failures = [name for name, passed in checks.items() if not passed]
    # Keep failure detail and exact scenario facts. Full receipt padding is
    # absent from the evidence record and remains reproducible from source.
    actual_facts.pop("coverage")
    return {
        "passed": not failures,
        "prerequisite": oracle["prerequisite"],
        "forbidden_claims": oracle["forbidden_claims"],
        "failed_checks": failures,
        "retained_facts": actual_facts,
        "omitted": packet["omitted"],
        "masked_receipts": masked,
    }
