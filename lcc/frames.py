"""Materialize coherent scoped frames at an already committed source boundary."""

from __future__ import annotations

from dataclasses import dataclass
from os import PathLike

from .checkpoint import _read_committed
from .projection import Snapshot
from .source import ContinuityError, Event, Scope


@dataclass(frozen=True)
class Frame:
    anchor: str
    event_ids: tuple[str, ...]
    required: bool
    payload: dict
    omission_reason: str = "budget"
    dependencies: tuple[str, ...] = ()


_OPERATION_EVENTS = {"tool_started", "tool_completed", "tool_failed", "tool_timeout"}


def _unique_events(events: list[Event]) -> list[Event]:
    unique: dict[str, Event] = {}
    for event in events:
        original = unique.setdefault(event.id, event)
        if original.digest != event.digest:
            raise ContinuityError(f"conflicting replay for event {event.id!r}")
    return list(unique.values())


def _superseded(events: list[Event]) -> set[str]:
    users: dict[str, int] = {}
    replacements: dict[str, str] = {}
    for order, event in enumerate(events):
        if event.kind == "message" and event.origin == "user":
            users[event.id] = order
        if event.kind == "message_superseded":
            old = event.data["replaces"]
            new = event.data["replacement"]
            if old not in users or new not in users or users[old] >= users[new]:
                raise ContinuityError(
                    f"correction {event.id!r} must link earlier scoped user messages in order"
                )
            if replacements.setdefault(old, new) != new:
                raise ContinuityError(f"conflicting corrections for message {old!r}")
    return set(replacements)


def _record(event: Event, order: int) -> dict:
    record = event.data | {"source_order": order}
    if "receipt" in record:
        record["receipt_trust"] = "untrusted"
    return record


def _operation_frame(operation_id: str, records: list[dict], snapshot: Snapshot) -> Frame:
    operation = snapshot.operations[operation_id]
    evidence = {
        "tool_started": operation.start_event,
        "tool_timeout": operation.timeout_event,
        "tool_completed": operation.terminal_event,
        "tool_failed": operation.terminal_event,
    }
    for record in records:
        first = evidence[record["kind"]]
        if record["id"] != first:
            record.pop("receipt", None)
            record.pop("verified", None)
            record.pop("receipt_trust", None)
            record["echo_of"] = first

    dependencies = ()
    if operation.retry_of is not None:
        dependencies = (snapshot.operations[operation.retry_of].start_event,)
    return Frame(
        anchor=operation.start_event,
        event_ids=tuple(record["id"] for record in records),
        required=operation.status in {"pending", "unknown"},
        payload={
            "kind": "operation",
            "operation": operation_id,
            "status": operation.status,
            "events": records,
        },
        dependencies=dependencies,
    )


def load_frames(
    source_path: str | PathLike[str],
    checkpoint_path: str | PathLike[str],
    scope: Scope,
) -> tuple[Snapshot, list[Frame], dict[str, str]]:
    """Read roles, evidence, and projection through the same committed cursor."""
    snapshot, available, source_info = _read_committed(source_path, checkpoint_path, scope)
    events = _unique_events(available)
    superseded = _superseded(events)
    current_artifact_events = set(snapshot.artifact_events.values())
    operation_records: dict[str, list[dict]] = {}
    for order, event in enumerate(events, 1):
        if event.kind in _OPERATION_EVENTS:
            operation_records.setdefault(event.data["operation"], []).append(_record(event, order))

    frames = []
    for order, event in enumerate(events, 1):
        if event.kind == "tool_started":
            operation_id = event.data["operation"]
            frames.append(_operation_frame(operation_id, operation_records[operation_id], snapshot))
        elif event.kind not in _OPERATION_EVENTS:
            required = (
                event.kind == "message" and event.origin in {"system", "user"}
                or event.kind == "message_superseded"
                or event.id in current_artifact_events
            )
            omitted = event.id in superseded
            frames.append(Frame(
                anchor=event.id,
                event_ids=(event.id,),
                required=required and not omitted,
                payload={"kind": event.kind, "events": [] if omitted else [_record(event, order)]},
                omission_reason="superseded" if omitted else "budget",
            ))
    return snapshot, frames, source_info
