# Offline resume packets

`build_packet(source_path, checkpoint_path, scope, task_context, max_bytes)`
reads an M1 snapshot and the matching scoped source records through its saved
cursor. Call `recover` separately to publish a new boundary. The builder
checks the checkpoint's integrity and source fingerprint and reads the saved
prefix from a read-only snapshot of the default rollback-journal SQLite
checkpoint. Appended events become visible after the next successful
ingestion. The packet labels source coverage `committed-prefix`.

## Task context

Supply one JSON object. A host snapshot carries explicit provenance and any
source-event dependencies:

```json
{
  "kind": "caller",
  "scope": {"task": "lighthouse", "session": "watch-7", "workflow": "main"},
  "provenance": "Fictional shift handoff",
  "context_text": "Check the outcome of T42 before starting another test.",
  "required_event_ids": ["E14"]
}
```

The packet labels this context `caller-asserted`. Its text is a supplied
snapshot. The transcript's original role and origin still determine how a
source message is presented. Required event IDs bind to the requested scope
and saved boundary.

A KRE input uses `kind: "kre"`, the same explicit `scope`, and a `result`
containing the public `kre-required/1` or `kre-task/1` output reviewed in
the [reuse matrix](reuse-contracts.md). KRE supplies retrieval, eligibility and
task-resume outcomes. The builder validates the declared outcome and exact
references. For `kre-required/1`, it carries `context_text` once with
metadata-only evidence items. Optional omissions can accompany a partial
response when every required reference remains eligible. For `kre-task/1`,
source-evidence entries point to shared excerpts, with literal text retained
for user assertions and model inferences. A partial resume that invalidates
task entries is rejected because that contract provides no required-versus-
optional distinction for those entries. Its rejection report preserves the
invalidated section, entry index, and reason.

## Packet format

The `lcc-packet/1` object includes scope, source identity and header fingerprint,
cursor, task context, frames, current artifact revisions, compact
revision-scoped verification, omission references, and accounting.

- Frames are ordered by their first source record. Each included event keeps
  its ID and origin, with `source_order` to recover chronological order when
  observations from multiple operations interleave. A tool frame retains its
  start and related observations together. Terminal echoes keep a source ID
  and an `echo_of` link. Receipt content is labeled `untrusted`.
- Required material includes system and applicable user messages, explicit
  corrections, pending and unknown operations, current artifact observations,
  and caller-listed evidence. A selected retry includes the predecessor
  frame. Each verification pass retains its artifact, exact revision,
  operation, start and terminal references, and whether its revision is
  current. Omitted completed frames include a typed `operation_receipt`
  with its status and zero-based start and terminal indices into that
  omission's `event_ids`. Retry and artifact links are preserved when present.
- An explicit `message_superseded` event links two prior user messages in
  canonical order. The superseded message becomes a reference with reason
  `superseded`. Other applicable user messages stay required. Optional
  frames are prioritized by recency and omitted with reason `budget` if
  their complete contents and dependencies cannot fit. An optional retry
  gives its prerequisite that same recency priority, with the prerequisite
  considered first.
- Omission entries carry scoped event IDs under the same source identity and
  cursor. Resolving them requires the original source. The builder performs
  no tool execution and preserves the stored checkpoint.

## Byte admission

`PacketResult.packet` is canonical compact JSON encoded in UTF-8. Its size
includes scope, roles, context, all references, and JSON punctuation.
`PacketResult.report()` gives the byte ceiling, exact `bytes_used` for a
successful packet, and `required_bytes` for the required fields and omission
references when calculable. The unit is `utf8_bytes` and the measured stage
is `serialized_resume_packet`. The report is separate from the packet.
Optional history uses only the remainder after required fields fit.

| Status | Meaning |
| --- | --- |
| `accepted` | The serialized packet fits the ceiling. |
| `missing_required_evidence` | Required context, event references, or eligible evidence is unavailable. |
| `unsupported_source` | The source boundary, scope, or public input shape could not be verified. |
| `budget_too_small` | The required content cannot fit or KRE reported a required-context budget failure. |

Every rejection returns a bounded diagnostic and no packet bytes.
Source reads are bounded by the committed M1 fixture limits. The task-context
input is capped at 1 MiB. All byte evidence concerns this offline format.
Provider rendering, token accounting, and response allowance belong to later
qualification.
