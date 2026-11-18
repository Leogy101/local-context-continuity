"""Select whole frames and admit the canonical UTF-8 packet."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from os import PathLike
from typing import Literal

from .frames import Frame, load_frames
from .projection import Snapshot
from .source import ContinuityError, Scope, canonical_json
from .task_context import (
    MissingTaskContext,
    TaskContextBudget,
    UnsupportedTaskContext,
    parse_task_context,
)

UNIT = "utf8_bytes"
STAGE = "serialized_resume_packet"
Status = Literal["accepted", "missing_required_evidence", "unsupported_source", "budget_too_small"]


@dataclass(frozen=True)
class PacketResult:
    status: Status
    max_bytes: int
    packet: bytes | None = None
    required_bytes: int | None = None
    diagnostic: str | None = None
    invalidated: tuple[dict, ...] = ()

    @property
    def bytes_used(self) -> int | None:
        return None if self.packet is None else len(self.packet)

    def report(self) -> dict:
        report = {
            "status": self.status,
            "unit": UNIT,
            "stage": STAGE,
            "max_bytes": self.max_bytes,
            "required_bytes": self.required_bytes,
            "bytes_used": self.bytes_used,
            "diagnostic": self.diagnostic,
        }
        if self.invalidated:
            report["invalidated"] = list(self.invalidated)
        return report


def _encoded(value: object) -> bytes:
    return canonical_json(value).encode("utf-8")


def _with_dependencies(starting: set[str], by_anchor: dict[str, Frame]) -> set[str]:
    needed = set()
    pending = list(starting)
    while pending:
        anchor = pending.pop()
        if anchor not in needed:
            needed.add(anchor)
            pending.extend(by_anchor[anchor].dependencies)
    return needed


def _verification(snapshot: Snapshot) -> list[dict]:
    return [
        {
            "operation": operation_id,
            "artifact": operation.artifact,
            "revision": operation.revision,
            "start_event": operation.start_event,
            "terminal_event": operation.terminal_event,
            "current": snapshot.artifacts.get(operation.artifact) == operation.revision,
        }
        for operation_id, operation in sorted(snapshot.operations.items())
        if operation.verified
    ]


def _array_overhead(count: int) -> int:
    """The fixed envelope accounts for brackets; count only separators here."""
    return max(0, count - 1)


def _omission(frame: Frame, snapshot: Snapshot) -> dict:
    result = {"event_ids": list(frame.event_ids), "reason": frame.omission_reason}
    if frame.payload["kind"] == "operation":
        operation_id = frame.payload["operation"]
        operation = snapshot.operations[operation_id]
        if operation.terminal_event is not None:
            receipt = {
                "id": operation_id,
                "status": operation.status,
                "start_event_index": 0,
                "terminal_event_index": frame.event_ids.index(operation.terminal_event),
            }
            if operation.retry_of is not None:
                receipt["retry_of"] = operation.retry_of
            if operation.artifact is not None:
                receipt.update(artifact=operation.artifact, revision=operation.revision)
            result["operation_receipt"] = receipt
    return result


def _optional_order(frames: list[Frame]) -> list[Frame]:
    # Give a prerequisite the recency of its newest dependent frame. Source
    # order breaks ties and puts prerequisites before their retries.
    priority = {frame.anchor: index for index, frame in enumerate(frames)}
    for frame in reversed(frames):
        for anchor in frame.dependencies:
            priority[anchor] = max(priority[anchor], priority[frame.anchor])
    return sorted(frames, key=lambda frame: -priority[frame.anchor])


def _assemble(
    snapshot: Snapshot, frames: list[Frame], source: dict,
    scope: Scope, context: dict, max_bytes: int,
) -> PacketResult:
    by_anchor = {frame.anchor: frame for frame in frames}
    by_event = {
        event_id: frame.anchor
        for frame in frames if frame.omission_reason != "superseded"
        for event_id in frame.event_ids
    }
    required = {frame.anchor for frame in frames if frame.required}
    for event_id in context["required_event_ids"]:
        if event_id not in by_event:
            return PacketResult(
                "missing_required_evidence", max_bytes,
                diagnostic="A required source event is unavailable in the scoped checkpoint.",
            )
        required.add(by_event[event_id])
    selected = _with_dependencies(required, by_anchor)

    envelope = {
        "schema": "lcc-packet/1",
        "accounting": {"unit": UNIT, "stage": STAGE},
        "scope": asdict(scope),
        "cursor": snapshot.cursor,
        "source": source | {"coverage": "committed-prefix"},
        "task_context": context,
        "artifacts": snapshot.artifacts,
        "verification": _verification(snapshot),
        "frames": [],
        "omitted": [],
    }
    omissions = {frame.anchor: _omission(frame, snapshot) for frame in frames}
    omission_cost = {key: len(_encoded(value)) for key, value in omissions.items()}
    frame_cost = {
        frame.anchor: len(_encoded(frame.payload))
        for frame in frames if frame.omission_reason != "superseded"
    }
    size = len(_encoded(envelope))
    size += sum(frame_cost[anchor] if anchor in selected else omission_cost[anchor]
                for anchor in by_anchor)
    size += _array_overhead(len(selected)) + _array_overhead(len(frames) - len(selected))
    required_bytes = size
    if required_bytes > max_bytes:
        return PacketResult(
            "budget_too_small", max_bytes, required_bytes=required_bytes,
            diagnostic="Required packet fields and source references exceed the byte ceiling.",
        )

    for frame in _optional_order(frames):
        anchor = frame.anchor
        if anchor in selected or anchor not in frame_cost:
            continue
        if any(dependency not in selected for dependency in frame.dependencies):
            continue
        count = len(selected)
        omitted_count = len(frames) - count
        change = frame_cost[anchor] - omission_cost[anchor]
        change += _array_overhead(count + 1) - _array_overhead(count)
        change += _array_overhead(omitted_count - 1) - _array_overhead(omitted_count)
        if size + change <= max_bytes:
            selected.add(anchor)
            size += change

    envelope["frames"] = [frame.payload for frame in frames if frame.anchor in selected]
    envelope["omitted"] = [omissions[frame.anchor] for frame in frames if frame.anchor not in selected]
    packet = _encoded(envelope)
    if len(packet) != size or len(packet) > max_bytes:
        raise ContinuityError("packet accounting mismatch")
    return PacketResult("accepted", max_bytes, packet, required_bytes)


def build_packet(
    source_path: str | PathLike[str], checkpoint_path: str | PathLike[str],
    scope: Scope, task_context: object, max_bytes: int,
) -> PacketResult:
    """Build against the saved source boundary without publishing a new one."""
    if type(max_bytes) is not int or max_bytes < 0:
        raise ValueError("max_bytes must be a nonnegative integer")
    try:
        context = parse_task_context(task_context, scope)
        snapshot, frames, source = load_frames(source_path, checkpoint_path, scope)
        return _assemble(snapshot, frames, source, scope, context, max_bytes)
    except MissingTaskContext as exc:
        return PacketResult(
            "missing_required_evidence", max_bytes, diagnostic=str(exc), invalidated=exc.invalidated,
        )
    except UnsupportedTaskContext as exc:
        return PacketResult("unsupported_source", max_bytes, diagnostic=str(exc))
    except TaskContextBudget as exc:
        return PacketResult("budget_too_small", max_bytes, diagnostic=str(exc))
    except (ContinuityError, OSError, RecursionError):
        return PacketResult(
            "unsupported_source", max_bytes,
            diagnostic="The source or checkpoint could not be verified at the saved boundary.",
        )
