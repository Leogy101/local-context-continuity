# M0 and M1 engineering report

M0 and M1 meet the offline acceptance checks. The deliverable is a standalone
local Python project with a synthetic source reader, deterministic operation
projection, and an atomic SQLite checkpoint. The local Git repository has
no remote.

## Contract decisions

| Source | Reviewed revision | Decision |
| --- | --- | --- |
| KRE | `bbf77a9e50d71292a8bada36e2af5142afb49c09` | Reuse the public task and required-evidence contracts in M2. |
| Agent Flight Recorder | `6db1c02a6655276e0e835d792d57bbc72fe43388` | Reuse recording 0.1 for optional diagnostics. Payload and coverage gaps remain explicit. |
| OpenClaw | `7338e1c1e4c1bd2a664e1964c3cb360d556083b8` | Defer host integration to M4. Installed cursor, fence, cancellation, and compaction behavior remain unknown. |

The [reuse matrix](reuse-contracts.md) records the public links, manifest
checks, and the decision to implement the narrow offline core. The core keeps
operation evidence and the consumed boundary together. It recovers from
stored state plus appended source events.

## Executed checks

Commands run from the project root on Darwin arm64, Python 3.11.15, and
SQLite 3.53.4:

```sh
python3.11 -B -m unittest discover -s tests -v
python3.11 -B -m lcc demo
```

Final suite result was **23 passed, 0 failed**. The staged fictional fixture
and independently written assertions cover all nine M1 acceptance cases.
The count reflects separate checks for the failure modes within those cases,
including abrupt exit immediately before and after commit, corruption,
malformed records, and cross-scope evidence. The master plan's
eight-scenario repeat-compaction benchmark is M3.

The demo printed:

```text
Before restart T42 pending (start E14)
After restart T42 pending (start E14)
Same projection and cursor after restart: True
After late result T42 succeeded (start E14)
Receipt E15: {"failed": 0, "passed": 7, "suite": "lamp-check"}
Replay leaves the same projection and cursor: True
Revision A verification: E15
Revision B verification: none
Historical revision A verification: E15
```

## Independent review

A reviewer who authored neither the M1 code nor the fixture assertions
inspected transaction boundaries, source identity, state transitions, and
the literal test oracles. An early test caught an unclear malformed JSON
diagnostic. Before the path fixes, regression probes also showed that a
fixture named like a SQLite sidecar could be removed, and that SQLite
interpreted a bare `:memory:` name as nondurable storage. The revised code
gives a clear malformed-input diagnostic, rejects fixture aliases including
sidecar paths, treats `:memory:` as an ordinary filename, and rejects
directory destinations.

The reviewer reran `python3 -B -m unittest discover -s tests -v` and
`env PYTHONDONTWRITEBYTECODE=1 python3 -B -m lcc demo` on Python 3.14.6.
All 23 tests and the demo passed. Separate probes covered concurrent
publishers and verified that the checkpoint retains fingerprints and state
without preserving original plan text. The reviewer found no unresolved
material issues in M1.

## Limits and proposed M2

M1 covers synthetic append-only fixtures and projection recovery. The
checkpoint verifies its consumed prefix by reading those bytes on recovery.
Checksums provide change detection. Source provenance comes from the
fixture producer. The exact receipt schema represents fractional quantities
as strings. The evidence is from offline process execution on this machine.

Proposed M2, pending approval, is deterministic selection of coherent
conversation frames, including explicit pending operations. Consume
validated KRE context or a caller-supplied task snapshot with explicit
provenance, retain invalidation and evidence references, and enforce
admission at a named serialized stage with an exact byte ceiling.
Report missing essential evidence and undersized budgets as
non-dispatchable. Packet construction should leave ingestion and
turn acceptance as separate operations.
