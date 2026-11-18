"""Deterministic operation and artifact state for one task scope."""

from copy import deepcopy
from dataclasses import asdict, dataclass, replace
from typing import Literal

from .source import ContinuityError, Event, canonical_json


Status = Literal["pending", "unknown", "succeeded", "failed"]


@dataclass(frozen=True)
class Operation:
    status: Status
    start_event: str
    artifact: str | None = None
    revision: str | None = None
    retry_of: str | None = None
    timeout_event: str | None = None
    terminal_event: str | None = None
    receipt: dict[str, object] | None = None
    verified: bool = False


@dataclass(frozen=True)
class Snapshot:
    cursor: str
    operations: dict[str, Operation]
    artifacts: dict[str, str]
    artifact_events: dict[str, str]

    def verification_events(self, artifact: str, revision: str) -> tuple[str, ...]:
        return tuple(sorted(
            operation.terminal_event
            for operation in self.operations.values()
            if operation.artifact == artifact
            and operation.revision == revision
            and operation.verified
            and operation.terminal_event is not None
        ))

    def current_verification(self, artifact: str) -> tuple[str, ...]:
        revision = self.artifacts.get(artifact)
        return () if revision is None else self.verification_events(artifact, revision)


@dataclass(frozen=True)
class _ArtifactObservation:
    revision: str
    event_id: str


class Projection:
    """Apply normalized events that have already been filtered to one scope."""

    def __init__(self) -> None:
        self._seen: dict[str, str] = {}
        self._operations: dict[str, Operation] = {}
        self._artifacts: dict[str, _ArtifactObservation] = {}

    def apply(self, event: Event) -> None:
        seen_digest = self._seen.get(event.id)
        if seen_digest is not None:
            if seen_digest != event.digest:
                raise ContinuityError(f"conflicting replay for event {event.id!r}")
            return

        match event.kind:
            case "tool_planned" | "message" | "message_superseded":
                pass
            case "artifact_observed":
                self._artifacts[event.data["artifact"]] = _ArtifactObservation(
                    revision=event.data["revision"], event_id=event.id
                )
            case "tool_started":
                self._start(event)
            case "tool_timeout" | "tool_completed" | "tool_failed":
                self._update(event)
            case _:
                raise ContinuityError(f"unsupported event kind {event.kind!r}")

        self._seen[event.id] = event.digest

    def _start(self, event: Event) -> None:
        operation_id = event.data["operation"]
        retry_of = event.data.get("retry_of")
        if operation_id in self._operations:
            raise ContinuityError(
                f"conflicting start for operation {operation_id!r} at event {event.id!r}"
            )
        if retry_of is not None and retry_of not in self._operations:
            raise ContinuityError(
                f"retry {operation_id!r} references missing start for {retry_of!r}"
            )

        self._operations[operation_id] = Operation(
            status="pending",
            start_event=event.id,
            artifact=event.data.get("artifact"),
            revision=event.data.get("revision"),
            retry_of=retry_of,
        )

    def _update(self, event: Event) -> None:
        operation_id = event.data["operation"]
        operation = self._operations.get(operation_id)
        if operation is None:
            raise ContinuityError(
                f"event {event.id!r} references missing start for {operation_id!r}"
            )

        if event.kind == "tool_timeout":
            if operation.timeout_event is None:
                status = "unknown" if operation.status == "pending" else operation.status
                self._operations[operation_id] = replace(
                    operation, status=status, timeout_event=event.id
                )
            return

        status = "succeeded" if event.kind == "tool_completed" else "failed"
        receipt = event.data["receipt"]
        verified = event.data.get("verified", False)
        if verified and (operation.artifact is None or operation.revision is None):
            raise ContinuityError(
                f"verification at event {event.id!r} requires an artifact revision"
            )
        if operation.terminal_event is not None:
            if (
                operation.status != status
                or operation.verified != verified
                or canonical_json(operation.receipt) != canonical_json(receipt)
            ):
                raise ContinuityError(
                    f"conflicting terminal receipt for operation {operation_id!r} "
                    f"at events {operation.terminal_event!r} and {event.id!r}"
                )
            return

        self._operations[operation_id] = replace(
            operation,
            status=status,
            terminal_event=event.id,
            receipt=deepcopy(receipt),
            verified=verified,
        )

    def snapshot(self, cursor: str) -> Snapshot:
        return Snapshot(
            cursor=cursor,
            operations=deepcopy(self._operations),
            artifacts={key: value.revision for key, value in self._artifacts.items()},
            artifact_events={key: value.event_id for key, value in self._artifacts.items()},
        )

    def to_record(self) -> dict:
        return {
            "seen": self._seen,
            "operations": {key: asdict(value) for key, value in self._operations.items()},
            "artifacts": {key: asdict(value) for key, value in self._artifacts.items()},
        }

    @classmethod
    def from_record(cls, values: object) -> "Projection":
        try:
            if not isinstance(values, dict) or set(values) != {"seen", "operations", "artifacts"}:
                raise ValueError("expected projection fields")
            if any(not isinstance(value, dict) for value in values.values()):
                raise ValueError("expected projection maps")
            projection = cls()
            projection._seen = values["seen"]
            projection._operations = {
                key: Operation(**value) for key, value in values["operations"].items()
            }
            projection._artifacts = {
                key: _ArtifactObservation(**value) for key, value in values["artifacts"].items()
            }
            return projection
        except (TypeError, ValueError) as exc:
            raise ContinuityError("invalid checkpoint projection") from exc
