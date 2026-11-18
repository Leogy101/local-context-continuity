"""Deterministic offline comparators for the M3 benchmark."""

from __future__ import annotations

from dataclasses import asdict
from os import PathLike

from lcc.frames import Frame, load_frames
from lcc.packet import (
    STAGE,
    UNIT,
    PacketResult,
    _array_overhead,
    _encoded,
    _optional_order,
    _with_dependencies,
)
from lcc.source import ContinuityError, Scope
from lcc.task_context import (
    MissingTaskContext,
    TaskContextBudget,
    UnsupportedTaskContext,
    parse_task_context,
)

STRATEGIES = ("recent_history", "observation_masking")
MASK_MINIMUM_BYTES = 256


def _required_frames(frames: list[Frame], context: dict, preserve_controls: bool) -> set[str]:
    required = set()
    latest_user = None
    by_event = {}
    for frame in frames:
        if frame.omission_reason == "superseded":
            continue
        for event_id in frame.event_ids:
            by_event[event_id] = frame.anchor
        event = frame.payload["events"][0]
        if event.get("role") == "user":
            latest_user = frame.anchor
        if (
            event.get("role") == "system"
            or frame.payload.get("status") in ("pending", "unknown")
            or preserve_controls and (
                event.get("role") == "user" or event["kind"] == "message_superseded"
            )
        ):
            required.add(frame.anchor)
    if latest_user is not None:
        required.add(latest_user)
    for event_id in context["required_event_ids"]:
        if event_id not in by_event:
            raise MissingTaskContext("A required source event is unavailable in the scoped checkpoint.")
        required.add(by_event[event_id])
    return _with_dependencies(required, {frame.anchor: frame for frame in frames})


def _masked_payload(frame: Frame) -> dict:
    events = []
    for event in frame.payload["events"]:
        if "receipt" in event and len(_encoded(event["receipt"])) >= MASK_MINIMUM_BYTES:
            event = event.copy()
            del event["receipt"]
            event.update(receipt_masked=True, mask_reason="older-observation")
        events.append(event)
    return frame.payload | {"events": events}


def _assemble(
    strategy: str,
    frames: list[Frame],
    scope: Scope,
    cursor: str,
    source: dict,
    context: dict,
    max_bytes: int,
) -> PacketResult:
    masking = strategy == "observation_masking"
    selected = _required_frames(frames, context, masking)
    envelope = {
        "schema": "lcc-packet/1",
        "accounting": {"unit": UNIT, "stage": STAGE},
        "scope": asdict(scope),
        "cursor": cursor,
        "source": source | {"coverage": "committed-prefix"},
        "task_context": context,
        "artifacts": {},
        "verification": [],
        "frames": [],
        "omitted": [],
    }
    last_completed = next(
        (frame.anchor for frame in reversed(frames)
         if frame.payload.get("status") in ("succeeded", "failed")),
        None,
    )
    omissions = {}
    payloads = {}
    for frame in frames:
        anchor = frame.anchor
        omissions[anchor] = {"event_ids": list(frame.event_ids), "reason": frame.omission_reason}
        if frame.omission_reason == "superseded":
            continue
        payloads[anchor] = (
            _masked_payload(frame)
            if masking and anchor not in selected and anchor != last_completed
            and frame.payload.get("status") in ("succeeded", "failed")
            else frame.payload
        )

    omission_bytes = {anchor: len(_encoded(value)) for anchor, value in omissions.items()}
    payload_bytes = {anchor: len(_encoded(value)) for anchor, value in payloads.items()}
    size = len(_encoded(envelope)) + sum(
        payload_bytes[anchor] if anchor in selected else omitted
        for anchor, omitted in omission_bytes.items()
    )
    size += _array_overhead(len(selected)) + _array_overhead(len(frames) - len(selected))
    required_bytes = size
    if size > max_bytes:
        return PacketResult(
            "budget_too_small", max_bytes, required_bytes=size,
            diagnostic="Required baseline fields and source references exceed the byte ceiling.",
        )

    for frame in _optional_order(frames):
        anchor = frame.anchor
        if (
            anchor in selected or anchor not in payloads
            or any(parent not in selected for parent in frame.dependencies)
        ):
            continue
        count = len(selected)
        omitted_count = len(frames) - count
        change = payload_bytes[anchor] - omission_bytes[anchor]
        change += _array_overhead(count + 1) - _array_overhead(count)
        change += _array_overhead(omitted_count - 1) - _array_overhead(omitted_count)
        if size + change <= max_bytes:
            selected.add(anchor)
            size += change

    envelope["frames"] = [payloads[frame.anchor] for frame in frames if frame.anchor in selected]
    envelope["omitted"] = [omissions[frame.anchor] for frame in frames if frame.anchor not in selected]
    packet = _encoded(envelope)
    if len(packet) != size or size > max_bytes:
        raise AssertionError("baseline packet accounting mismatch")
    return PacketResult("accepted", max_bytes, packet, required_bytes)


def build_baseline(
    strategy: str,
    source_path: str | PathLike[str],
    checkpoint_path: str | PathLike[str],
    scope: Scope,
    task_context: object,
    max_bytes: int,
) -> PacketResult:
    """Evaluate a benchmark comparator at the committed source prefix."""
    if strategy not in STRATEGIES:
        raise ValueError("unsupported benchmark strategy")
    if type(max_bytes) is not int or max_bytes < 0:
        raise ValueError("max_bytes must be a nonnegative integer")
    try:
        context = parse_task_context(task_context, scope)
        snapshot, frames, source = load_frames(source_path, checkpoint_path, scope)
        return _assemble(strategy, frames, scope, snapshot.cursor, source, context, max_bytes)
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
