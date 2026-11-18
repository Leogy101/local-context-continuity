"""Run the offline alpha's synthetic end-to-end demonstration."""

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory

from lcc import ContinuityError, build_packet
from lcc.source import canonical_json

from .decision_validation import ARTIFACT, SCOPE, validate_continue


CEILING = 4096


def _event(event_id, kind, origin, **data):
    return {"id": event_id, "kind": kind, "origin": origin, "scope": asdict(SCOPE)} | data


def _append(source, *records):
    with source.open("ab") as stream:
        for record in records:
            stream.write(canonical_json(record).encode("utf-8") + b"\n")


def _recover_fresh(source, checkpoint):
    result = subprocess.run([
        sys.executable, "-B", "-m", "lcc", "recover", str(source), str(checkpoint),
        SCOPE.task, SCOPE.session, SCOPE.workflow,
    ], capture_output=True, text=True)
    if result.returncode:
        raise ContinuityError(result.stderr.strip() or "recovery child failed")
    return json.loads(result.stdout)


def _demonstrate(directory):
    source = directory / "beacon.jsonl"
    checkpoint = directory / "beacon.sqlite"
    _append(source, {"schema": 1, "source_id": "offline-alpha-beacon"},
            _event("V-ASK", "message", "user", role="user",
                   text="Verify the current beacon settings before cutting over."),
            _event("V-EDIT-A", "artifact_observed", "runtime", artifact=ARTIFACT, revision="A"),
            _event("V-START", "tool_started", "runtime", operation="Verify-A", artifact=ARTIFACT, revision="A"),
            _event("LOG-START", "tool_started", "runtime", operation="Log-telemetry"),
            _event("LOG-END", "tool_completed", "tool", operation="Log-telemetry",
                   receipt={"telemetry": "nominal " * 640}))
    context = {"kind": "caller", "scope": asdict(SCOPE), "provenance": "Synthetic alpha demonstration",
               "context_text": "Maintain the beacon verification prerequisite.", "required_event_ids": []}
    before = _recover_fresh(source, checkpoint)
    wide = build_packet(source, checkpoint, SCOPE, context, 16_384)
    compact = build_packet(source, checkpoint, SCOPE, context, CEILING)
    if wide.packet is None or compact.packet is None or len(compact.packet) >= len(wide.packet):
        raise ContinuityError("expected an admitted compact packet smaller than the wide packet")
    packet = json.loads(compact.packet)
    omissions = [item["event_ids"] for item in packet["omitted"] if item["reason"] == "budget"]
    pending = next((frame for frame in packet["frames"] if frame.get("operation") == "Verify-A"), None)
    if ["LOG-START", "LOG-END"] not in omissions or pending is None or pending["status"] != "pending":
        raise ContinuityError("compaction did not retain the pending start and omit telemetry")
    restarted = _recover_fresh(source, checkpoint)
    rebuilt = build_packet(source, checkpoint, SCOPE, context, CEILING)
    if before != restarted or rebuilt.packet != compact.packet:
        raise ContinuityError("fresh recovery changed the projection or compact packet")

    _append(source,
            _event("V-PASS", "tool_completed", "tool", operation="Verify-A", receipt={}, verified=True),
            _event("V-EDIT-B", "artifact_observed", "runtime", artifact=ARTIFACT, revision="B"))
    stale = _recover_fresh(source, checkpoint)
    blocked = validate_continue(source, checkpoint, SCOPE, {
        "next_action": "continue", "evidence_ids": ["V-PASS"],
        "state": {"current_revision": "B", "last_passing_revision": "A"},
    })
    previous_pass = stale["operations"]["Verify-A"]
    if (blocked.status != "blocked" or blocked.current_revision != "B"
            or previous_pass["revision"] != "A" or not previous_pass["verified"]):
        raise ContinuityError("expected a historical pass for A and a blocked proposal for B")

    _append(source,
            _event("V-START-B", "tool_started", "runtime", operation="Verify-B", artifact=ARTIFACT, revision="B"),
            _event("V-PASS-B", "tool_completed", "tool", operation="Verify-B", receipt={}, verified=True))
    proposal = {"next_action": "continue", "evidence_ids": ["V-PASS-B"]}
    uncommitted = validate_continue(source, checkpoint, SCOPE, proposal)
    _recover_fresh(source, checkpoint)
    allowed = validate_continue(source, checkpoint, SCOPE, proposal)
    if uncommitted.status != "insufficient_evidence" or allowed.status != "allowed":
        raise ContinuityError("expected a refusal before checkpointing and approval with committed B proof")

    return {
        "compaction": {"wide_bytes": len(wide.packet), "bounded_bytes": len(compact.packet),
                       "ceiling_bytes": CEILING, "omitted_event_ids": omissions},
        "restart": {"cursor": restarted["cursor"], "pending_operation": "Verify-A",
                    "status": restarted["operations"]["Verify-A"]["status"],
                    "start_event": restarted["operations"]["Verify-A"]["start_event"],
                    "same_projection_and_packet": True},
        "stale_verification": {"current_revision": blocked.current_revision,
                               "last_verified_revision": previous_pass["revision"]},
        "decisions": {"stale_pass": asdict(blocked), "uncommitted_pass": asdict(uncommitted),
                      "committed_current_pass": asdict(allowed)},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    try:
        with TemporaryDirectory(prefix="lcc-alpha-") as directory:
            result = _demonstrate(Path(directory))
    except (ContinuityError, OSError) as error:
        print(f"lcc-alpha-demo: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
