# Offline V1 alpha

`local-context-continuity` **1.0.0a1** includes source recovery, bounded context
assembly, and a read-only decision gate for synthetic tasks. The pure Python
wheel has no runtime dependencies beyond Python 3.11 or later with
standard-library SQLite.

## Offline install

Use the wheel and checksum file from the same release. From the artifact
directory, verify the hashes and install into a disposable environment.

```sh
shasum -a 256 -c SHA256SUMS
lcc_wheel="$PWD/local_context_continuity-1.0.0a1-py3-none-any.whl"
lcc_install=$(mktemp -d)
python3.11 -m venv "$lcc_install/venv"
"$lcc_install/venv/bin/python" -m pip install --disable-pip-version-check \
  --no-index --no-deps "$lcc_wheel"
"$lcc_install/venv/bin/python" -m pip check
(cd "$lcc_install" && "$lcc_install/venv/bin/lcc-alpha-demo")
```

`python -m lcc.alpha` runs the same JSON walkthrough. It recovers the same
pending operation, cursor, and bounded packet after restart. The wide view uses
7,042 bytes and the bounded view uses 1,601 of its 4,096-byte budget. Revision A
passes verification, then B becomes current. A proposal to continue is
`blocked` when it relies on A's receipt, becomes `insufficient_evidence` while
B's receipt is uncommitted, and becomes `allowed` after B's valid receipt is
committed. The walkthrough performs no real verification or cutover.

The installed package also provides `python -m lcc recover`,
`python -m lcc packet`, and `from lcc.alpha import validate_continue`. The
individual walkthroughs `python -m lcc demo` and `python -m lcc packet-demo`
use packaged synthetic fixtures. [Fixture format and CLI examples](../README.md#use-a-synthetic-source)

## Architecture and security

- **Source and checkpoint.** Recovery reads a normalized append-only JSONL
  source scoped to a task, session, and workflow. SQLite commits the source
  cursor and operation projection together. Starts, outcomes, and verification
  evidence retain their original artifact revision. [Fixture format](../README.md#fixture-format)
- **Context assembly.** The packet builder reads the checkpoint, selects required
  and optional whole frames within a UTF-8 byte limit, and records omission
  references and provenance. The source history stays available. The offline
  builder consumes explicit context. [Packet contract](resume-packets.md) · [Related contracts](reuse-contracts.md)
- **Decision validation.** The gate checks the synthetic `beacon.cfg` policy
  against committed evidence for the exact current revision, including the
  observation, verification start, and verified completion. The result can be
  `allowed`, `blocked`, or `insufficient_evidence`. Another same-revision
  operation without a verified pass can block continuation as ambiguous
  because the records lack verifier identity. [Decision prototype](decision-validation-prototype.md)

Callers own the source, fixture provenance, and disclosure boundary. Use a
trusted producer and stable caller-owned paths. Checksums detect
inconsistency. Packet views can carry historical text and tool payloads, so
review the actual packet before sharing it. The gate issues a decision at an
observed cursor. Production enforcement requires authenticated receipt
provenance, a trusted currentness check at action time, and qualified OpenClaw
lifecycle integration. Production action enforcement and installed OpenClaw
integration remain unqualified. Broader M5 qualification remains on hold.

## Build and verify a release

Run the source checks from the project root. They exercise the full offline
suite, the 19-file frozen evaluation package, and the retained synthetic M3 output.

```sh
python3.11 -B -m unittest discover -s tests -v
python3.11 -B -m evaluation.standalone.prepare --check
lcc_replay=$(mktemp -d)
python3.11 -B -m benchmarks.run --output "$lcc_replay/m3.json"
cmp benchmarks/results/m3.json "$lcc_replay/m3.json"
```

Build offline from a disposable checkout with `setuptools>=77.0.3` and `wheel`
already installed. The reference build uses Python 3.11.15, pip 26.1,
setuptools 82.0.1, wheel 0.47.0, and the epoch below.

```sh
lcc_wheel_dir=$(mktemp -d)
SOURCE_DATE_EPOCH=1795030740 python3.11 -m pip wheel --disable-pip-version-check \
  --no-index --no-deps --no-build-isolation --wheel-dir "$lcc_wheel_dir" .
```

Run the installed walkthrough from outside the checkout and save its output
as `alpha-demo.json` in the artifact directory. Produce fresh digests after the
build and walkthrough.

```sh
(cd "$lcc_wheel_dir" && shasum -a 256 \
  local_context_continuity-1.0.0a1-py3-none-any.whl alpha-demo.json > SHA256SUMS)
```

### Release checklist

- Verify the exact source inventory, fresh public history, and MIT license.
- Run the source checks and verify the wheel's license, contents, offline
  installation, and synthetic walkthrough in a clean environment.
- Confirm the release assets match the reviewed source and fresh checksums.
- Obtain owner approval before publishing the source or artifacts.

The source checkout carries the frozen synthetic inputs and the replay and
scoring programs. The release wheel carries the runtime, the MIT license,
and the two legacy synthetic walkthrough fixtures. The full model-run
archives stay local.
[Evaluation methodology and results](standalone-evaluation.md)
