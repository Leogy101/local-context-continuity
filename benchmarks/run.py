"""Exercise deterministic continuity at repeated committed boundaries."""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict
from functools import partial
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory

from lcc import ContinuityError, Scope, build_packet, recover

from .baselines import build_baseline
from .scoring import score

FIXTURES = Path(__file__).with_name("fixtures")
SCHEDULES = (1, 3, 5, 10)
CEILING = 8192
WIDE_CEILING = 128 * 1024
BUILDERS = {
    "lcc": build_packet,
    "recent_history": partial(build_baseline, "recent_history"),
    "observation_masking": partial(build_baseline, "observation_masking"),
}
REJECTIONS = {
    "missing_required_evidence": "REJECTED_REQUIRED_EVIDENCE",
    "budget_too_small": "REJECTED_BUDGET",
    "unsupported_source": "INVALID_SOURCE",
}

# A fixture-only child inserts the late source record after the production
# publisher has staged its SQLite row. It commits and terminates immediately.
_LATE_PUBLISHER = """
import json
import os
from pathlib import Path
import sqlite3
import sys
from lcc import Scope, recover

source, checkpoint, late, scope = sys.argv[1:]
source = Path(source)
late = Path(late).read_bytes()
connect = sqlite3.connect

class Interrupted(sqlite3.Connection):
    def commit(self):
        with source.open("ab") as stream:
            stream.write(late)
            stream.flush()
            os.fsync(stream.fileno())
        super().commit()
        os._exit(73)

sqlite3.connect = lambda *args, **kwargs: connect(*args, factory=Interrupted, **kwargs)
recover(source, checkpoint, Scope(**json.loads(scope)))
"""


def _encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _digest(data):
    return sha256(data).hexdigest()


def _cursor(prefix):
    return f"v1.{len(prefix):x}.{_digest(prefix)}"


def _read_fixture(name, fixture):
    with (FIXTURES / f"{name}.jsonl").open("rb") as stream:
        data = stream.read(65537)
    if len(data) > 65536:
        raise ValueError(f"Fixture exceeds the byte limit for {name}")
    header, *lines = data.splitlines(keepends=True)
    ids = [json.loads(line)["id"] for line in lines]
    count = fixture["before_count"]
    if ids != fixture["input_event_ids"] or not 0 < count < len(lines):
        raise ValueError(f"Invalid fixture manifest for {name}")
    return header + b"".join(lines[:count]), b"".join(lines[count:])


def _scope_events(prefix, scope):
    header, *lines = prefix.splitlines(keepends=True)
    scoped = {}
    for line in lines:
        event = json.loads(line)
        if event["scope"] == scope:
            scoped.setdefault(event["id"], event)
    return scoped, {"source_id": json.loads(header)["source_id"], "fingerprint": _digest(header)}


def _context(fixture, phase):
    return {
        "kind": "caller",
        "scope": fixture["scope"],
        "provenance": "Independent synthetic benchmark task snapshot",
        "context_text": fixture["context_text"],
        "required_event_ids": phase.get("required_event_ids", []),
    }


def _ballast(scope, index):
    operation = f"Noise-{index:02d}"
    common = {"scope": scope, "operation": operation}
    return b"".join(_encode(event) + b"\n" for event in (
        common | {"id": operation + "-S", "kind": "tool_started", "origin": "runtime"},
        common | {"id": operation + "-R", "kind": "tool_completed", "origin": "tool",
                  "receipt": {"telemetry": "status: nominal; " * 285}},
    ))


def _files(source, checkpoint):
    return _digest(source.read_bytes()), _digest(checkpoint.read_bytes())


def _fresh_packet(source, checkpoint, scope, context, budget):
    task_context = checkpoint.with_suffix(".json")
    task_context.write_bytes(_encode(context))
    return subprocess.run(
        [sys.executable, "-B", "-m", "lcc", "packet", str(source), str(checkpoint),
         scope.task, scope.session, scope.workflow, "--task-context", str(task_context),
         "--max-bytes", str(budget)],
        capture_output=True, timeout=20,
    )


def _attempt(strategy, paths, scope, context, budget, expected=None,
             prefix=None, oracle=None, fresh=False, check_drop=False):
    source, checkpoint = paths
    before = _files(source, checkpoint)
    try:
        build = BUILDERS[strategy]
        wide = build(source, checkpoint, scope, context, WIDE_CEILING)
        result = build(source, checkpoint, scope, context, budget)
        replay = build(source, checkpoint, scope, context, budget)
        valid_wire = result.max_bytes == budget and (
            result.status == "accepted" and result.packet is not None
            and len(result.packet) <= budget and _encode(json.loads(result.packet)) == result.packet
            or result.status in REJECTIONS and result.packet is None
        )
        record = {
            "admission": result.report(),
            "raw_candidate_bytes": wide.bytes_used,
            "raw_candidate_status": wide.status,
            "budget_admitted_bytes": result.bytes_used,
            "packet_sha256": _digest(result.packet) if result.packet is not None else None,
            "repeat_equal": replay == result,
            "valid_wire": valid_wire,
        }
        outcome = REJECTIONS.get(result.status, "IMPLEMENTATION_ERROR")
        if expected is not None:
            record["expected_outcome"] = expected
        if result.packet is not None and prefix is not None:
            events, source_info = _scope_events(prefix, context["scope"])
            packet = json.loads(result.packet)
            visible_context = {key: context[key] for key in
                               ("kind", "provenance", "context_text", "required_event_ids")}
            visible_context["status"] = "caller-asserted"
            review = score(packet, oracle, context["scope"], _cursor(prefix), source_info,
                           events, visible_context, strategy)
            outcome = "SUCCESS" if review["passed"] else "ORACLE_FAILURE"
            if check_drop:
                recovered = any(
                    event["id"] == "D-RECEIPT" and "receipt" in event
                    for frame in packet["frames"] for event in frame["events"]
                )
                record["old_receipt_body_visible"] = recovered
            record["oracle"] = review
        if fresh:
            child = _fresh_packet(source, checkpoint, scope, context, budget)
            record["fresh_process_equal"] = (
                child.returncode == 0 and child.stdout == result.packet
                and json.loads(child.stderr) == result.report()
            )
        record["inputs_unchanged"] = _files(source, checkpoint) == before
        if (not valid_wire or not record["repeat_equal"] or not record["inputs_unchanged"]
                or fresh and not record["fresh_process_equal"]):
            outcome = "IMPLEMENTATION_ERROR"
        record["outcome"] = outcome
        record["dispatchable_bytes"] = result.bytes_used if outcome == "SUCCESS" else None
        if expected is not None:
            record["expectation_met"] = outcome == expected
        return record
    except Exception as exc:
        return {
            "outcome": "IMPLEMENTATION_ERROR",
            "error": _error(exc, source.parent),
            "inputs_unchanged": _files(source, checkpoint) == before,
        }


def _error(exc, directory):
    return f"{type(exc).__name__}: {exc}".replace(str(directory), "<temporary>")[:240]


def _save(source, checkpoint, scope, prefix):
    snapshot = recover(source, checkpoint, scope)
    if snapshot.cursor != _cursor(prefix):
        raise ValueError("published cursor differs from the canonical opening prefix")
    return snapshot


def _interrupt(source, checkpoint, scope, suffix):
    late = checkpoint.with_suffix(".jsonl")
    late.write_bytes(suffix)
    result = subprocess.run(
        [sys.executable, "-B", "-c", _LATE_PUBLISHER, str(source), str(checkpoint),
         str(late), _encode(asdict(scope)).decode()],
        capture_output=True, timeout=20,
    )
    if result.returncode != 73:
        raise RuntimeError("fixture publisher did not exit at the injected post-commit boundary")
    return result.returncode


def _exercise(name, fixture, count, directory):
    source = directory / "source.jsonl"
    checkpoint = directory / "checkpoint.sqlite"
    paths = source, checkpoint
    steps = {strategy: [] for strategy in BUILDERS}
    recovery = {}
    try:
        prefix, suffix = _read_fixture(name, fixture)
        scope = Scope(**fixture["scope"])
        source.write_bytes(prefix)
        interruption = name == "interrupted_publication"
        for index in range(1, count + 1):
            noise = _ballast(fixture["scope"], index)
            prefix += noise
            with source.open("ab") as stream:
                stream.write(noise)
            if interruption and index == count:
                recovery["publisher_exit"] = _interrupt(source, checkpoint, scope, suffix)
                recovery["late_outside_opening_extent"] = source.read_bytes() == prefix + suffix
            else:
                _save(source, checkpoint, scope, prefix)
            phase = fixture["before"]
            for strategy, entries in steps.items():
                entries.append({"boundary": index} | _attempt(
                    strategy, paths, scope, _context(fixture, phase), CEILING,
                    prefix=prefix, oracle=phase,
                    fresh=strategy == "lcc" and index == count
                    and (interruption or name == "pending_tool" and count == 5),
                    check_drop=name == "old_dependency",
                ))

        if not interruption:
            with source.open("ab") as stream:
                stream.write(suffix)
        prefix += suffix
        snapshot = _save(source, checkpoint, scope, prefix)
        committed = _files(source, checkpoint)
        recovery["ingestion_replay_equal"] = _save(source, checkpoint, scope, prefix) == snapshot
        recovery["ingestion_replay_unchanged"] = _files(source, checkpoint) == committed
        if interruption or name == "pending_tool":
            late = json.loads(suffix)
            operation = snapshot.operations[late["operation"]]
            recovery["late_receipt_persisted_once"] = (
                operation.start_event == "E14" and operation.terminal_event == "E15"
                and _encode(operation.receipt) == _encode(late["receipt"])
                and sum(json.loads(line).get("id") == "E15" for line in prefix.splitlines()[1:]) == 1
            )
        if not all(recovery.values()):
            raise ValueError("late publication or ingestion replay did not match the fixture")
        phase = fixture["after"]
        for strategy, entries in steps.items():
            entries.append({"boundary": "after_transition"} | _attempt(
                strategy, paths, scope, _context(fixture, phase), CEILING,
                prefix=prefix, oracle=phase, check_drop=name == "old_dependency",
            ))
    except Exception as exc:
        for entries in steps.values():
            entries.append({"boundary": "setup_or_recovery", "outcome": "IMPLEMENTATION_ERROR",
                            "error": _error(exc, directory)})

    results = []
    for strategy, entries in steps.items():
        failures = [step["outcome"] for step in entries if step["outcome"] != "SUCCESS"]
        if "IMPLEMENTATION_ERROR" in failures:
            outcome = "IMPLEMENTATION_ERROR"
        else:
            outcome = failures[0] if failures else "SUCCESS"
        results.append({
            "scenario": name, "compactions": count, "strategy": strategy, "budget": CEILING,
            "outcome": outcome, "steps": entries, "recovery": recovery,
        })
    return results


def _run_negative_cases(fixtures, directory, negatives):
    fixture = fixtures["old_dependency"]
    prefix, suffix = _read_fixture("old_dependency", fixture)
    scope = Scope(**fixture["scope"])
    source = directory / "source.jsonl"
    checkpoint = directory / "checkpoint.sqlite"
    paths = source, checkpoint
    context = _context(fixture, fixture["after"])
    source.write_bytes(prefix)
    _save(source, checkpoint, scope, prefix)
    with source.open("ab") as stream:
        stream.write(suffix)
    tail_id = json.loads(suffix)["id"]

    cases = [
        ("required_in_unconsumed_tail", CEILING, context | {"required_event_ids": [tail_id]},
         "REJECTED_REQUIRED_EVIDENCE"),
        ("required_over_ceiling", 256, context, "REJECTED_BUDGET"),
        ("changed_source_prefix", CEILING, context, "INVALID_SOURCE"),
    ]
    for label, budget, task_context, expectation in cases:
        if label == "required_over_ceiling":
            _save(source, checkpoint, scope, prefix + suffix)
        if label == "changed_source_prefix":
            source.write_bytes((prefix + suffix).replace(b"relay-inventory", b"relay-corrupted", 1))
        for strategy in BUILDERS:
            negatives.append({"case": label, "strategy": strategy, "budget": budget} | _attempt(
                strategy, paths, scope, task_context, budget, expected=expectation,
            ))

    # The three selectors share one publisher. Keep the ingestion conflict
    # once, separately from packet admission and a valid UNKNOWN outcome.
    prefix, suffix = _read_fixture("pending_tool", fixtures["pending_tool"])
    conflict_scope = Scope(**fixtures["pending_tool"]["scope"])
    conflict_source = directory / "conflict.jsonl"
    conflict_db = directory / "conflict.sqlite"
    conflict_source.write_bytes(prefix + suffix)
    _save(conflict_source, conflict_db, conflict_scope, prefix + suffix)
    changed = json.loads(suffix)
    changed["receipt"] = {"contradictory": True}
    with conflict_source.open("ab") as stream:
        stream.write(_encode(changed) + b"\n")
    before = _files(conflict_source, conflict_db)
    try:
        recover(conflict_source, conflict_db, conflict_scope)
    except ContinuityError as exc:
        outcome = "CONFLICT" if "conflict" in str(exc) else "INVALID_SOURCE"
        diagnostic = str(exc)
    else:
        outcome, diagnostic = "IMPLEMENTATION_ERROR", "Conflicting replay was published."
    unchanged = before == _files(conflict_source, conflict_db)
    negatives.append({
        "case": "conflicting_terminal_replay", "strategy": "shared_publisher",
        "outcome": outcome, "inputs_unchanged": unchanged, "diagnostic": diagnostic,
        "expectation_met": outcome == "CONFLICT" and unchanged,
    })


def _negatives(fixtures, directory):
    results = []
    try:
        _run_negative_cases(fixtures, directory, results)
    except Exception as exc:
        results.append({"case": "negative_setup", "outcome": "IMPLEMENTATION_ERROR",
                        "error": _error(exc, directory)})
    return results


def _comparisons(cells):
    indexed = {(cell["scenario"], cell["compactions"], cell["strategy"]): cell for cell in cells}
    comparisons = {strategy: {} for strategy in BUILDERS if strategy != "lcc"}
    for candidate in cells:
        if candidate["strategy"] != "lcc":
            continue
        for strategy, counts in comparisons.items():
            other = indexed[candidate["scenario"], candidate["compactions"], strategy]
            outcome = "incomparable"
            if candidate["outcome"] == other["outcome"] == "SUCCESS":
                first = candidate["steps"][-1]["dispatchable_bytes"]
                second = other["steps"][-1]["dispatchable_bytes"]
                outcome = "lcc_smaller" if first < second else "lcc_larger" if first > second else "equal"
            counts[outcome] = counts.get(outcome, 0) + 1
    return comparisons


def run():
    fixtures = json.loads((FIXTURES / "oracle.json").read_bytes())
    cells = []
    for name, fixture in fixtures.items():
        for count in SCHEDULES:
            with TemporaryDirectory(prefix="lcc-m3-") as directory:
                cells.extend(_exercise(name, fixture, count, Path(directory)))
    with TemporaryDirectory(prefix="lcc-m3-negative-") as directory:
        negatives = _negatives(fixtures, Path(directory))
    counts = {
        strategy: dict(Counter(cell["outcome"] for cell in cells if cell["strategy"] == strategy))
        for strategy in BUILDERS
    }
    old_route = [
        not cell["steps"][-2].get("old_receipt_body_visible", True)
        and cell["steps"][-1].get("old_receipt_body_visible", False)
        for cell in cells
        if cell["scenario"] == "old_dependency" and cell["strategy"] == "lcc"
        and cell["compactions"] > 1 and cell["outcome"] == "SUCCESS"
    ]
    complete = len(cells) == 96 and all(
        [step["boundary"] for step in cell["steps"]] ==
        [*range(1, cell["compactions"] + 1), "after_transition"] for cell in cells
    )
    gate_met = (complete and counts["lcc"] == {"SUCCESS": 32} and old_route == [True] * 3
                and len(negatives) == 10
                and all(case.get("expectation_met", False) for case in negatives)
                and all(cell["outcome"] != "IMPLEMENTATION_ERROR" for cell in cells))
    return {
        "format": "lcc-benchmark/1", "compactions": list(SCHEDULES), "budget": CEILING,
        "raw_candidate_ceiling": WIDE_CEILING, "stage": "serialized_resume_packet",
        "unit": "utf8_bytes", "counts": counts, "final_bytes_comparison": _comparisons(cells),
        "gate_met": gate_met,
        "cells": cells, "negative_cases": negatives,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="write full JSON evidence to this file")
    options = parser.parse_args()
    results = run()
    data = _encode(results) + b"\n"
    if options.output is None:
        sys.stdout.buffer.write(data)
    else:
        options.output.write_bytes(data)
        print(json.dumps({"counts": results["counts"], "gate_met": results["gate_met"]}))
    return 0 if results["gate_met"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
