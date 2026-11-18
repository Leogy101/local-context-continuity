"""Recover synthetic observations and assemble bounded offline resume packets."""

import argparse
import json
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory

from . import ContinuityError, PacketResult, Scope, build_packet, recover
from .source import _unique_object
from .task_context import MAX_TASK_CONTEXT_BYTES


def _demo() -> None:
    fixtures = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
    with TemporaryDirectory(prefix="lcc-demo-") as directory:
        source = Path(directory) / "source.jsonl"
        checkpoint = Path(directory) / "checkpoint.sqlite"
        source.write_bytes((fixtures / "t42-through-e14.jsonl").read_bytes())
        terminal, edit = (fixtures / "t42-e15-to-e16.jsonl").read_bytes().splitlines(keepends=True)
        command = [
            sys.executable, "-B", "-m", "lcc", "recover", str(source), str(checkpoint),
            "lighthouse", "watch-7", "main",
        ]

        def fresh_process() -> dict:
            result = subprocess.run(command, capture_output=True, text=True)
            if result.returncode:
                raise ContinuityError(result.stderr.strip())
            return json.loads(result.stdout)

        def print_operation(label: str, state: dict) -> None:
            operation = state["operations"]["T42"]
            print(f"{label} T42 {operation['status']} (start {operation['start_event']})")

        before = fresh_process()
        print_operation("Before restart", before)
        after = fresh_process()
        print_operation("After restart", after)
        print(f"Same projection and cursor after restart: {before == after}")
        with source.open("ab") as stream:
            stream.write(terminal)
        completed = fresh_process()
        print_operation("After late result", completed)
        operation = completed["operations"]["T42"]
        print(f"Receipt {operation['terminal_event']}: {json.dumps(operation['receipt'], sort_keys=True)}")
        print(f"Replay leaves the same projection and cursor: {completed == fresh_process()}")
        print(f"Revision A verification: {', '.join(completed['current_verification']['beacon.cfg'])}")
        with source.open("ab") as stream:
            stream.write(edit)
        revised = fresh_process()
        verified = revised["current_verification"]["beacon.cfg"]
        print(f"Revision {revised['artifacts']['beacon.cfg']} verification: "
              f"{', '.join(verified) or 'none'}")
        previous_pass = revised["operations"]["T42"]
        if previous_pass["verified"]:
            print(f"Historical revision {previous_pass['revision']} verification: "
                  f"{previous_pass['terminal_event']}")


def _packet_demo() -> None:
    fixtures = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
    source = fixtures / "t42-through-e14.jsonl"
    scope = Scope("lighthouse", "watch-7", "main")
    context = {
        "kind": "caller",
        "scope": asdict(scope),
        "provenance": "Fictional shift handoff",
        "context_text": "Check the outcome of T42 before starting another test.",
        "required_event_ids": ["E14"],
    }
    with TemporaryDirectory(prefix="lcc-packet-demo-") as directory:
        checkpoint = Path(directory) / "checkpoint.sqlite"
        recover(source, checkpoint, scope)
        baseline = build_packet(source, checkpoint, scope, context, 64 * 1024)
        if baseline.required_bytes is None:
            raise ContinuityError("packet demo could not load its synthetic inputs")
        exact = build_packet(source, checkpoint, scope, context, baseline.required_bytes)
        rejected = build_packet(source, checkpoint, scope, context, baseline.required_bytes - 1)
        if exact.packet is None or rejected.status != "budget_too_small":
            raise ContinuityError("packet demo did not meet its admission boundaries")
        print("Exact ceiling", json.dumps(exact.report(), sort_keys=True))
        print(exact.packet.decode("utf-8"))
        print("One byte below", json.dumps(rejected.report(), sort_keys=True))


def _invalid_constant(value: str) -> None:
    raise ValueError("expected a finite JSON number")


def _packet(args: argparse.Namespace, scope: Scope) -> int:
    try:
        path = Path(args.task_context)
        if not path.is_file():
            raise OSError("not a task context file")
        with path.open("rb") as stream:
            raw = stream.read(MAX_TASK_CONTEXT_BYTES + 1)
        if len(raw) > MAX_TASK_CONTEXT_BYTES:
            raise ValueError("task context input exceeds the file limit")
        context = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object,
                             parse_constant=_invalid_constant)
    except OSError:
        result = PacketResult(
            "missing_required_evidence", args.max_bytes,
            diagnostic="The task context file is unavailable.",
        )
    except (ValueError, RecursionError):
        result = PacketResult(
            "unsupported_source", args.max_bytes,
            diagnostic="The task context file is invalid or exceeds the input limit.",
        )
    else:
        result = build_packet(args.source, args.checkpoint, scope, context, args.max_bytes)

    print(json.dumps(result.report(), sort_keys=True), file=sys.stderr)
    if result.packet is None:
        return 1
    sys.stdout.buffer.write(result.packet)
    return 0


def _byte_limit(value: str) -> int:
    amount = int(value)
    if amount < 0:
        raise argparse.ArgumentTypeError("the byte ceiling must be nonnegative")
    return amount


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("demo", help="demonstrate restart and a late fictional result")
    commands.add_parser("packet-demo", help="demonstrate exact packet admission and rejection")
    ingest = commands.add_parser("recover", help="ingest a fixture and print the scoped projection")
    packet = commands.add_parser("packet", help="assemble a read-only packet from a saved boundary")
    for command in (ingest, packet):
        for name in ("source", "checkpoint", "task", "session", "workflow"):
            command.add_argument(name)
    packet.add_argument("--task-context", required=True, metavar="JSON")
    packet.add_argument("--max-bytes", required=True, type=_byte_limit)
    args = parser.parse_args()
    try:
        if args.command == "demo":
            _demo()
        elif args.command == "packet-demo":
            _packet_demo()
        else:
            scope = Scope(args.task, args.session, args.workflow)
            if args.command == "packet":
                return _packet(args, scope)
            state = recover(args.source, args.checkpoint, scope)
            output = asdict(state)
            output["current_verification"] = {
                artifact: state.current_verification(artifact) for artifact in state.artifacts
            }
            print(json.dumps(output, sort_keys=True))
    except (ContinuityError, OSError) as exc:
        print(f"lcc: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
