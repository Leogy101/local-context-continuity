# M2 engineering report

M2 implements offline bounded resume packets and meets the milestone acceptance
checks. The starting commit was `bfd29efe527576ace4a67b118919fa033c9bb189`.
The local commit containing this report records the completed implementation.

Packet assembly reads the unchanged M1 checkpoint and its verified source
prefix. It preserves applicable role-labeled requirements, whole operation
frames, pending and unknown outcomes, retry links, and exact revision
associations. Older optional history keeps source references. Omitted completed
operations also retain a compact typed receipt. The builder consumes the
pinned KRE required-context and task-resume results or an explicitly
caller-asserted snapshot.

## Verification

Run from the project root on Python 3.11 or later:

```sh
python3.11 -B -m unittest discover -s tests -v
python3.11 -B -m lcc demo
python3.11 -B -m lcc packet-demo
```

All **40 tests passed** on Python 3.11.15. The suite includes 23 M1 regressions
and 17 M2 tests with independently authored fictional oracles. It checks
pending T42 before ingestion of E15, historical verification of A after
revision B, explicit corrections with still-applicable requirements,
required old identifiers, KRE evidence and provenance, literal UTF-8
length, fresh-process determinism, scope isolation, and byte-for-byte
checkpoint preservation on success and failure.

The packet demo emits the complete accepted example between its two admission
reports:

```text
Exact ceiling {"bytes_used": 1670, "diagnostic": null, "max_bytes": 1670, "required_bytes": 1670, "stage": "serialized_resume_packet", "status": "accepted", "unit": "utf8_bytes"}
One byte below {"bytes_used": null, "diagnostic": "Required packet fields and source references exceed the byte ceiling.", "max_bytes": 1669, "required_bytes": 1670, "stage": "serialized_resume_packet", "status": "budget_too_small", "unit": "utf8_bytes"}
```

The accepted example includes E14 as the sole event in T42's pending frame,
includes the current observation E01, and references the 12 omitted events.
The rejected example returns no packet. The M1 demo still reconciles E15
to E14 after restart and retains A's pass without verifying B.

## Independent review

Reviewers inspected changes they had not authored. Their checks included a
literal wire-length oracle, a fresh-process check, a malformed unconsumed
tail, read-only SQLite sidecar checks, and pinned KRE fixtures. Four
changes followed:

- Omitted operations retain a typed status, receipt references, and
  applicable retry and revision links. The source reports committed-prefix
  coverage explicitly.
- Retry packing orders prerequisites once. In the reviewer's focused
  4,000-retry probe, assembly at the required-only ceiling went from roughly
  1.73 seconds to 0.14 seconds with exact accounting.
- KRE provenance dates must be valid and include a timezone. Shared
  task-card excerpts stay in one evidence item, while each related card
  entry retains its ordered references.
- Non-dispatchable KRE task-resume results preserve invalidated section,
  index, and reason in the admission report.

The revised implementation passed independent engineering review.

## Limits and proposed M3

M2 measures the full serialized offline JSON packet. Provider rendering,
model context capacity, and response allowances require later
qualification. Source evidence remains recoverable from the committed
synthetic JSONL prefix. Task context is a supplied snapshot, so KRE's
retained eligibility describes that snapshot. A partial task resume with
invalidated entries fails closed because its public contract does not
label any invalidated entry optional.

Proposed M3 is the plan's eight-scenario repeat-compaction benchmark.
Run the independent scenarios across the 1, 3, 5, and 10 compaction
schedules, compare with recent-history and observation-masking baselines
under comparable byte ceilings, and retain failing examples and exact
accounting. M3 awaits owner approval.
