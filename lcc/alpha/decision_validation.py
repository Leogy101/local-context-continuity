"""Read-only prerequisite gate for the synthetic beacon-settings workflow."""

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from lcc import ContinuityError, Scope
from lcc.checkpoint import _read_committed
from lcc.projection import Projection


SCOPE = Scope(task="beacon-settings", session="edit-2", workflow="main")
ARTIFACT = "beacon.cfg"


@dataclass(frozen=True)
class ContinueDecision:
    status: Literal["allowed", "blocked", "insufficient_evidence"]
    reason: str
    source_id: str | None = None
    cursor: str | None = None
    current_revision: str | None = None
    evidence_ids: tuple[str, ...] = ()
    unsupported_ids: tuple[str, ...] = ()


def validate_continue(
    source_path: str | Path, checkpoint_path: str | Path, scope: Scope, proposal: object,
) -> ContinueDecision:
    """Assess a proposed continue against caller-owned source and checkpoint files."""
    if scope != SCOPE:
        return ContinueDecision("insufficient_evidence", "scope_outside_policy")
    if not isinstance(proposal, dict) or proposal.get("next_action") != "continue":
        return ContinueDecision("insufficient_evidence", "action_outside_policy")
    refs = proposal.get("evidence_ids", [])
    if not isinstance(refs, list) or any(not isinstance(ref, str) or not ref.strip() for ref in refs):
        return ContinueDecision("blocked", "invalid_references")

    try:
        snapshot, events, source = _read_committed(source_path, checkpoint_path, scope)
        # Reuse replay checks and confirm the projection agrees with the scoped source.
        projected = Projection()
        for event in events:
            projected.apply(event)
        if projected.snapshot(snapshot.cursor) != snapshot:
            return ContinueDecision("insufficient_evidence", "checkpoint_projection_mismatch")
        committed_size = int(snapshot.cursor.split(".")[1], 16)
        if Path(source_path).stat().st_size != committed_size:
            return ContinueDecision("insufficient_evidence", "source_not_at_checkpoint")
    except (ContinuityError, OSError):
        return ContinueDecision("insufficient_evidence", "invalid_committed_evidence")

    revision = snapshot.artifacts.get(ARTIFACT)
    boundary = {"source_id": source["source_id"], "cursor": snapshot.cursor,
                "current_revision": revision}
    if revision is None:
        return ContinueDecision("insufficient_evidence", "missing_artifact_observation", **boundary)
    observation = snapshot.artifact_events[ARTIFACT]
    attempts = [op for op in snapshot.operations.values() if op.artifact == ARTIFACT]
    current = [op for op in attempts if op.revision == revision]
    passes = set(snapshot.current_verification(ARTIFACT))
    if not passes or any(op.terminal_event not in passes for op in current):
        evidence = (observation,) + tuple(
            event_id for op in current or attempts
            for event_id in (op.start_event, op.timeout_event, op.terminal_event) if event_id is not None
        )
        if passes:
            reason = "ambiguous_current_operations"
        elif any(op.status in {"pending", "unknown"} for op in current):
            reason = "current_verification_incomplete"
        else:
            reason = "current_verification_missing_pass"
        return ContinueDecision("blocked", reason, evidence_ids=evidence, **boundary)

    proof = (observation,) + tuple(
        event_id for op in current for event_id in (op.start_event, op.terminal_event) if event_id is not None
    )
    unsupported = tuple(sorted(set(refs) - set(proof)))
    if unsupported:
        return ContinueDecision("blocked", "unsupported_references", evidence_ids=proof,
                                unsupported_ids=unsupported, **boundary)
    return ContinueDecision("allowed", "current_verification_passed", evidence_ids=proof, **boundary)
