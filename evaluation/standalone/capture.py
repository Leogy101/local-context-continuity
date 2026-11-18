"""Reproduce the three selected M3 snapshots for the standalone pilot."""

from dataclasses import dataclass
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from benchmarks import run as benchmark
from lcc import Scope


COMPACTIONS = 10
POLICIES = ("lcc", "recent_history")
CASES = (
    ("requirement_correction", "after_transition"),
    ("pending_tool", COMPACTIONS),
    ("stale_verification", "after_transition"),
)
M3_RESULTS = benchmark.FIXTURES.parent / "results" / "m3.json"
M3_SHA256 = "1eb6022c022402c1610f726f5c8d36338e1f25951030d1a836d43f9d7de978ae"


@dataclass(frozen=True, slots=True)
class CaseCapture:
    name: str
    boundary: int | str
    prefix: bytes
    scope: dict
    cursor: str
    context: dict
    packets: dict[str, bytes]
    compactions: int = COMPACTIONS


def _capture_case(name, boundary, fixture, retained, directory):
    source = directory / "source.jsonl"
    checkpoint = directory / "checkpoint.sqlite"
    scope = Scope(**fixture["scope"])
    prefix, suffix = benchmark._read_fixture(name, fixture)
    source.write_bytes(prefix)
    for index in range(1, COMPACTIONS + 1):
        ballast = benchmark._ballast(fixture["scope"], index)
        with source.open("ab") as stream:
            stream.write(ballast)
        prefix += ballast
        snapshot = benchmark._save(source, checkpoint, scope, prefix)

    if boundary == "after_transition":
        with source.open("ab") as stream:
            stream.write(suffix)
        prefix += suffix
        snapshot = benchmark._save(source, checkpoint, scope, prefix)
        phase = fixture["after"]
    else:
        phase = fixture["before"]
    context = benchmark._context(fixture, phase)

    before = benchmark._files(source, checkpoint)
    expected_files = {source, checkpoint}
    if before[0] != benchmark._digest(prefix) or set(directory.iterdir()) != expected_files:
        raise ValueError(f"{name}: replayed source or checkpoint files differ")

    packets = {}
    for policy in POLICIES:
        cell = retained[name, COMPACTIONS, policy]
        step = next(item for item in cell["steps"] if item["boundary"] == boundary)
        expected_cursor = step["oracle"]["retained_facts"]["cursor"]["expected"]
        if step["outcome"] != "SUCCESS" or snapshot.cursor != expected_cursor:
            raise ValueError(f"{name}/{policy}: replayed M3 boundary differs")

        result = benchmark.BUILDERS[policy](source, checkpoint, scope, context, benchmark.CEILING)
        if (benchmark._files(source, checkpoint) != before
                or set(directory.iterdir()) != expected_files):
            raise ValueError(f"{name}/{policy}: assembly changed replay files")
        packet = result.packet
        if result.status != "accepted" or packet is None:
            raise ValueError(f"{name}/{policy}: assembly rejected: {result.report()}")
        if (result.report() != step["admission"]
                or len(packet) != step["budget_admitted_bytes"]
                or benchmark._digest(packet) != step["packet_sha256"]
                or json.loads(packet)["cursor"] != expected_cursor):
            raise ValueError(f"{name}/{policy}: packet differs from the retained M3 result")
        packets[policy] = packet

    return CaseCapture(name, boundary, prefix, fixture["scope"], snapshot.cursor, context, packets)


def capture() -> tuple[CaseCapture, ...]:
    """Return the exact prefixes and wire packets, using disposable checkpoints."""
    evidence = M3_RESULTS.read_bytes()
    if benchmark._digest(evidence) != M3_SHA256:
        raise ValueError("retained M3 evidence digest differs")
    report = json.loads(evidence)
    if report["budget"] != benchmark.CEILING:
        raise ValueError("benchmark ceiling differs from retained M3 evidence")
    retained = {
        (cell["scenario"], cell["compactions"], cell["strategy"]): cell
        for cell in report["cells"]
    }
    fixtures = json.loads((benchmark.FIXTURES / "oracle.json").read_bytes())
    captured = []
    for name, boundary in CASES:
        with TemporaryDirectory(prefix="lcc-standalone-") as path:
            captured.append(_capture_case(name, boundary, fixtures[name], retained, Path(path)))
    return tuple(captured)
