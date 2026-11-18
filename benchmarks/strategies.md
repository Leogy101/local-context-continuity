# Benchmark strategies

Each strategy receives the same committed canonical source prefix, task scope,
task-context input, and byte ceiling. All three use LCC's frame and context
readers, including the source fingerprint, explicit supersession, replay identity,
operation grouping, and role and origin checks. Every packet uses the
`lcc-packet/1` envelope and compact canonical JSON. Admission measures the length
of that complete UTF-8 byte string, at the `serialized_resume_packet` stage.

| Strategy | Required frames and other state | Optional frame selection |
| --- | --- | --- |
| `lcc` | Applicable user and system messages, correction records, current artifact observations, pending and unknown operations, explicit required event IDs, and their retry dependencies. Includes the M2 artifact and revision-scoped verification projection. | Considers recent coherent frames first. A terminal operation omitted for budget retains a compact operation receipt and source references. |
| `recent_history` | System messages, latest applicable user request, pending and unknown operations, explicit required event IDs, and their retry dependencies. | Considers recent coherent frames first, keeping each full or leaving only its recoverable omission references. |
| `observation_masking` | The recent-history requirements plus all applicable user messages and correction records. | Uses the same selection order. For optional complete operation frames older than the newest complete operation frame, masks terminal receipt bodies of at least 256 canonical UTF-8 bytes. |

All variants select at frame granularity and emit frames in source order. Recent
priority is based on a frame's source anchor. A retry predecessor inherits the
priority of its newest dependent and precedes that dependent at equal priority.
If an optional frame fails to fit, selection continues with the next candidate.
Source order and event IDs remain available for interleaved tool observations.
Superseded messages carry omission references.

The baselines use M2's operation grouping and leave the artifact and
verification fields empty. An oracle inspecting a baseline can use artifact
observations and start and terminal associations actually selected in frames.
Both baselines select artifact observations by frame recency. The masking
strategy retains each selected masked event's ID, origin, outcome and structured
verification marker with its operation frame. Its `receipt_masked` and `mask_reason` fields
explain that the receipt body is recoverable only from the canonical source.
An exact required ID forces its full coherent frame. Recovery of a required
value depends on the value appearing in an eligible frame.

`required_bytes` includes the strategy's required frames and field contents
plus its omission references. Optional frame selection uses the remaining
allowance. A packet can pass byte admission while omitting a fact the independent
oracle requires. The benchmark records that as a continuity failure.
