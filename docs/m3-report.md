# M3 repeated compaction benchmark

**The M3 acceptance gate passed.** Work started at accepted M2 commit
`d7d0f484d8a25e3fdc30261505ebd49c52c780ee`. The local commit containing this
report binds the runner, eight independent oracles, and retained evidence.
M3 changes are confined to benchmark tooling, fixtures, evidence, and tests.

## Continuity results

Each row contains four schedules at 1, 3, 5, and 10 compactions. A schedule
passes when every numbered boundary and its post-transition assessment
satisfy the oracle. The 96 matched schedules produced 552 step outcomes.

| Scenario | LCC passed | Recent history passed | Observation masking passed |
| --- | ---: | ---: | ---: |
| Requirement correction | 4/4 | 4/4 | 4/4 |
| Pending tool | 4/4 | 4/4 | 4/4 |
| Stale verification | 4/4 | 4/4 | 2/4 |
| Old dependency | 4/4 | 4/4 | 4/4 |
| Scope collision | 4/4 | 4/4 | 2/4 |
| Unknown outcome | 4/4 | 4/4 | 4/4 |
| Clock skew | 4/4 | 4/4 | 4/4 |
| Interrupted publication | 4/4 | 4/4 | 2/4 |
| **Total** | **32/32** | **32/32** | **26/32** |

Observation masking missed required evidence in the 5- and 10-compaction
schedules for stale verification, scope collision, and interrupted
publication. The failures include missing historical verification, a missing
local artifact observation, and a missing terminal operation. The complete
[machine-readable record](../benchmarks/results/m3.json) retains all passing
and failing steps, exact observed and expected facts, omission and mask
references, and packet hashes.

## Serialized byte comparison

All three strategies used an 8,192-byte primary ceiling on complete canonical
UTF-8 JSON packets. The table compares the final assessment only for pairs
where both complete schedules passed. It treats six masking continuity
failures as incomparable.

| LCC compared with | LCC smaller | Equal | LCC larger | Incomparable |
| --- | ---: | ---: | ---: | ---: |
| Recent history | 6 | 5 | 21 | 0 |
| Observation masking | 18 | 5 | 3 | 6 |

Recent history matched LCC's retention on this corpus and used fewer bytes
in 21 matched schedules. The simple baseline is a viable comparison for
later work. The byte results measure the recorded policy at the fixture
ceiling and include each strategy's envelope, roles, context, and references.

Each step separates `raw_candidate_bytes` under the 128 KiB diagnostic
ceiling, `required_bytes` for the strategy's essentials and omission
references, and `budget_admitted_bytes` at the primary ceiling.
`dispatchable_bytes` additionally requires oracle success in the benchmark.
The common stage is `serialized_resume_packet`, with unit `utf8_bytes`.
The [strategy definitions](../benchmarks/strategies.md) document the shared
M1 reader and grouping, and the differences in retained state.

## Recovery and negative evidence

Each numbered boundary appends one irrelevant completed interaction, ingests
the canonical prefix into SQLite, and replaces the bounded view. The last
numbered boundary immediately precedes the decisive event. A separate
read-only assessment follows its ingestion. The canonical source survives
every reduction. All three strategies receive the same source, scope,
active request, explicit task-context input, and ceiling.

For the old dependency after ten reductions, LCC's 7,950-byte pre-transition
packet retained a reference to `D-RECEIPT` and omitted its body. The explicit
later dependency recovered the original opaque ticket from that receipt in
the 7,173-byte final packet. All four schedules demonstrated absence and
subsequent recovery of the receipt body.

In each interruption schedule, a child process appended and synced E15 after
the SQLite row covering E14 had been staged. It committed and exited
immediately. A fresh process read the saved prefix with the original
operation pending. Subsequent ingestion persisted E15's exact failure
receipt once for that same operation. For the 10-compaction LCC cell,
the before and after packets measured 8,037 and 7,942 bytes. The pending
T42 scenario independently recovered the normal late success. The timeout
scenario preserved the operation's unknown outcome and the need to
reconcile it before a repeat.

All ten negative trials met their expectations. Each strategy rejected an
explicitly required ID in the unconsumed tail, a 256-byte ceiling below its
minimum, and a changed committed prefix. The shared publisher separately
reported a conflicting terminal replay. The rejections preserved the
checkpoint, and the admission failures returned no packet.

## Reproduction and engineering review

Run from the project root. The regression suite passed **44 tests** on
Python 3.11.15, including 40 M1 and M2 regressions.

```sh
python3.11 -B -m unittest discover -s tests -v
python3.11 -B -m lcc demo
python3.11 -B -m lcc packet-demo
python3.11 -B -m benchmarks.run --output /private/tmp/lcc-m3-replay.json
cmp benchmarks/results/m3.json /private/tmp/lcc-m3-replay.json
```

Independent fresh runs produced byte-identical 1,777,328-byte evidence files
with SHA-256 `1eb6022c022402c1610f726f5c8d36338e1f25951030d1a836d43f9d7de978ae`.

Independent review checked fixture-oracle independence, comparator fairness,
exact serialization, recoverable evidence, negative status accounting, and the
actual SQLite commit boundary. Review found that the initial scorer could
accept altered JSON value types, added roles on tool events, incorrect source
ordinals, and unsupported artifact claims. The revised scorer uses strict
JSON equality and validates roles, order, grouping, and artifact claims.
Execution errors now take precedence in a cell's aggregate so an earlier
oracle miss cannot hide a later error. The reviewer reproduced the fixes,
the full suite, and the retained matrix, then signed off.

## Limits and M4 recommendation

These results establish deterministic continuity for eight synthetic fixtures,
one primary ceiling, and the documented comparators. The benchmark models
repeated packet assembly with the canonical archive available. Its task
context is caller-asserted. Public KRE contract coverage comes from the M2
regression suite. Provider rendering, model behavior, and live host semantics
remain outside the evidence.

After approval, start M4 with a version-bound installed OpenClaw capability
profile and one isolated synthetic session. Qualify transcript cursor identity,
replacement and reset behavior, context-engine ownership, accepted-turn
fencing, cancellation, and checkpoint recovery. Use that evidence to determine
the smallest adapter that preserves the M1 and M2 boundaries.
