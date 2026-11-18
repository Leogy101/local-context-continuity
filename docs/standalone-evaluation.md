# Standalone continuation evaluation

The retained pilot compared Local Context Continuity (LCC) packets with
recent-history packets from three synthetic timelines. The two strategies
used the same consumed source prefix, task scope, caller context, and
role-separated instructions for each case. This was an exploratory
standalone comparison. Production action enforcement and installed
OpenClaw integration remain unqualified.

## Retained results

| Measure | LCC | Recent history |
| --- | ---: | ---: |
| Prompt tokens across the three cases, per deployment | 4,787 | 6,465 |
| Strictly correct responses across the two deployments | 1/6 | 1/6 |

LCC used approximately **26% fewer prompt tokens** in aggregate on this set
and used **243 more** on the pending-operation case. The token counts describe
these two deployments and tested inputs. The separate [M3 benchmark](m3-report.md)
measures serialized bytes. The retained four-request clarification diagnostic
left action accuracy at 1/2 and strict correctness at 0/2 per instruction.

The read-only decision gate replay independently **blocked six stale
proposals and allowed two valid controls** from committed evidence. Its
decisions are separate from answer grading. [Decision replay and evidence](decision-validation-prototype.md)

## Frozen inputs and scoring

Each pair uses a boundary from a 10-compaction synthetic schedule.

| Case | Boundary | Decisive evidence |
| --- | --- | --- |
| Requirement correction | After the transition | `R-CORRECTION` replaces `R-NORTH` with `R-SOUTH`. The backup constraint `R-BACKUP` still applies. |
| Pending operation | Before the transition at step 10 | `E14` starts `T42`. The tenth ballast exchange is present and the receipt `E15` lies outside the consumed prefix. |
| Stale verification | After the transition | `V-EDIT-B` makes B current. `V-START` and `V-PASS` bind the historical pass to A. |

The packet allowance of 8,192 bytes applies to the entire canonical UTF-8
packet, including provenance and omission references. The
[frozen manifest](../evaluation/standalone/frozen/manifest.json) records packet,
message-body, and wrapper byte counts. Message-body bytes cover the common
instruction and rendered packet. Wrapper bytes measure JSON envelope and
escaping overhead. Runtime token counts also depend on the model tokenizer
and chat template.

The frozen package includes six messages-only requests, six packets, three
caller snapshots, and three consumed source traces. Its retained checkout
hash is historical provenance. The public source tree has a separate Git
history. The [oracle](../evaluation/standalone/oracle.json) stays separate
from the requests. Strict scoring checks the action, typed state, and cited
evidence against an exact frozen request hash. Missing answers or altered
request bytes are treated as inconclusive. These tools declare no actions
to execute from a model response.

The public source provides enough material to regenerate the fixtures and
check the frozen scorer offline. The aggregate pilot and diagnostic results
come from retained local runs whose full archives stay local. The three
files under [decision gate fixtures](../tests/fixtures/decision-validation)
are cleared model-generated answers to synthetic inputs, kept as
byte-for-byte regression excerpts.

## Offline reproduction

From the repository root with Python 3.11 or later, run

```sh
lcc_repro=$(mktemp -d)
python3.11 -B -m evaluation.standalone.prepare --check
python3.11 -B -m evaluation.standalone.prepare --output "$lcc_repro/frozen"
diff -r evaluation/standalone/frozen "$lcc_repro/frozen"
python3.11 -B -m unittest discover -s tests -v
```

Grade an existing response locally with

```sh
python3.11 -B -m evaluation.standalone.score CASE REQUEST_JSON RESPONSE_JSON
```

Use an exact request file in `evaluation/standalone/frozen/requests`.
Omit `RESPONSE_JSON` to record a missing answer, or pass
`--runtime-error` to record an observed runtime failure. These commands
perform no inference.
