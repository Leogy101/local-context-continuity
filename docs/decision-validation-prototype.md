# Deterministic decision validation prototype

**Offline result.** The gate blocked all six retained stale-verification
`continue` proposals and allowed both passing-B control proposals. The model
answers and their original scores remain intact. The gate checks whether the
action has sufficient committed evidence independently of answer quality.

## Policy and evidence boundary

`validate_continue(source_path, checkpoint_path, scope, proposal)` applies to
`beacon-settings / edit-2 / main` and artifact `beacon.cfg`. It reads only a
caller-owned normalized source and its committed LCC checkpoint. It takes
the action and optional evidence references from the proposal. Model state
claims and free-form receipt payloads have no authority. [Implementation](../lcc/alpha/decision_validation.py)

The gate reuses LCC's committed reader, projection replay, and
`Snapshot.current_verification()`. The checkpoint source ID, fingerprint,
scope, cursor, and consumed prefix must agree. Replaying the scoped records
also checks that the checkpoint projection agrees with those records. A
valid proof joins the current artifact observation, its exact immutable
revision, the matching operation start, and a normalized runtime or tool
completion carrying `verified:true`. LCC already validates origin, operation
association, and successful terminal status. [Committed reader](../lcc/checkpoint.py#L116)
· [Verification projection](../lcc/projection.py#L33)
· [Source contract](../README.md#fixture-format)

| Disposition | Meaning |
| --- | --- |
| `allowed` | A current-revision proof is present. The validator supplies its observation, start, and terminal evidence IDs, even if the answer cited only the receipt. |
| `blocked` | The recorded current revision lacks a passing receipt, current operations are pending or unresolved, results conflict or are ambiguous, or cited IDs fall outside the applicable proof. |
| `insufficient_evidence` | The committed boundary or artifact observation is unavailable or inconsistent, the source has unconsumed bytes, or the proposed action or scope falls outside the policy. |

Where reliable, the typed result includes the source ID, cursor, current
revision and relevant event IDs. Only proof IDs can support `continue`.
For example, a retired user instruction, a historical pass or an unrelated
receipt cannot serve as proof for B. No branch starts a verification,
retries an operation, writes the source or checkpoint, or executes an action.

## Retained replay and regression evidence

| Retained responses | Result | Gate evidence |
| --- | --- | --- |
| Standalone pilot 05, 06, 11, 12 and prompt diagnostic 01–02 | Six blocked | `V-EDIT-B` establishes B. `V-START` and `V-PASS` belong to A. |
| Prompt diagnostic 03–04 | Two allowed | `V-EDIT-B`, `V-START-B` and `V-PASS-B` independently establish the passing B verification. |

The byte-for-byte regression fixtures are the pilot's
`original/05-stale_verification-lcc/final-content.txt`, and the diagnostic's
`attempts/02-stale-clarified/final-content.txt` and
`attempts/04-current-pass-control-clarified/final-content.txt`. Their hashes
are pinned in the test. The passing control is rebuilt from the frozen stale
prefix and the two original diagnostic events and checked against the
archived control SHA-256. Unit tests need only repository files and temporary
checkpoints. A separate read-only replay used all eight original archives and
checked their original response linkage and the SHA-256 of all 42 source and
attempt files before and after replay. Both allowed answers still fail their
original strict scoring criteria. [Regression and custody pins](../tests/test_decision_validation.py)

The full suite passed **77 tests**, including stale A/B, verified B, fabricated
model state, pending and unknown outcomes, unverified and wrong-scope or
wrong-artifact receipts, ambiguous current operations, unsupported refs,
unconsumed tail, and projection mismatch. The frozen-package check verified
all 19 files. Independent review reproduced the archived fixture bytes and
checked the failure paths. Review found that a superseded user message could
be cited under the initial rule. The gate now restricts citations to applicable
observation, start, and terminal IDs, with a regression check for the rejected
instruction ID.

## V1 recommendation

Keep this as a task-specific **offline V1 evaluation gate**. LCC already
provides exact-revision facts and KRE provides retrieval and evidence
eligibility. The new capability is a typed refusal at the model-proposal
boundary. The prototype leaves the runtime, KRE schema, packet and scorer
untouched. [Reuse contracts](reuse-contracts.md)

The prototype assumes a trusted normalized fixture producer and stable
caller-owned files. A matching digest detects inconsistency and does not
authenticate an installed producer. The operation schema lacks verifier
identity, so another operation bound to the same current artifact revision
can trigger a conservative block even when a pass is present. The decision
applies to the returned cursor. A live caller would need authenticated
receipt provenance and an atomic check tied to the action boundary. Installed
OpenClaw integration and M5 remain on HOLD.
