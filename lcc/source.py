"""Read an immutable JSONL prefix and a bounded, append-only tail."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from typing import BinaryIO, Iterator

SCHEMA = 1
MAX_LINE_BYTES = 64 * 1024
MAX_SOURCE_BYTES = 16 * 1024 * 1024


class ContinuityError(ValueError):
    """The source or checkpoint cannot be recovered safely."""


def _identifier(value: object) -> bool:
    return isinstance(value, str) and 0 < len(value) <= 512


@dataclass(frozen=True)
class Scope:
    task: str
    session: str
    workflow: str

    def __post_init__(self) -> None:
        if not all(_identifier(value) for value in (self.task, self.session, self.workflow)):
            raise ContinuityError("scope requires nonempty task, session, and workflow IDs")


@dataclass(frozen=True)
class Event:
    id: str
    kind: str
    scope: Scope
    origin: str
    data: dict
    digest: str


@dataclass(frozen=True)
class Boundary:
    offset: int
    prefix_sha256: str

    @property
    def cursor(self) -> str:
        return f"v1.{self.offset:x}.{self.prefix_sha256}"


def fingerprint(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                      separators=(",", ":"))


def _unique_object(pairs: list[tuple]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _unsupported_number(value: str) -> None:
    raise ValueError("non-integer JSON number; use a string for exact decimals")


def _json_object(raw: bytes) -> tuple[dict, bytes]:
    data = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object,
                      parse_float=_unsupported_number, parse_constant=_unsupported_number)
    if not isinstance(data, dict):
        raise ValueError("expected a JSON object")
    return data, canonical_json(data).encode("utf-8")


_BASE = {"id", "kind", "scope", "origin"}
_OBSERVED = {"runtime", "tool"}
# Origin, required payload fields, optional payload fields.
_KINDS = {
    "message": ({"system", "user", "assistant"}, {"role", "text"}, set()),
    "message_superseded": ({"user"}, {"replaces", "replacement"}, set()),
    "tool_planned": ({"assistant"}, {"operation"}, set()),
    "tool_started": (_OBSERVED, {"operation"}, {"artifact", "revision", "retry_of"}),
    "tool_timeout": ({"runtime"}, {"operation"}, set()),
    "tool_completed": (_OBSERVED, {"operation", "receipt"}, {"verified"}),
    "tool_failed": (_OBSERVED, {"operation", "receipt"}, set()),
    "artifact_observed": (_OBSERVED, {"artifact", "revision"}, set()),
}


def _parse_event(raw: bytes) -> Event:
    data, normalized = _json_object(raw)
    kind = data.get("kind")
    if not isinstance(kind, str) or kind not in _KINDS:
        raise ValueError(f"unsupported event kind {kind!r}")
    origins, required, optional = _KINDS[kind]
    keys = data.keys()
    if not (_BASE | required <= keys <= _BASE | required | optional | {"timestamp"}):
        raise ValueError(f"missing or unsupported fields for {kind}")
    origin = data["origin"]
    if not isinstance(origin, str) or origin not in origins:
        raise ValueError(f"invalid origin for {kind}")
    scope_data = data["scope"]
    if not isinstance(scope_data, dict) or scope_data.keys() != {"task", "session", "workflow"}:
        raise ValueError("invalid event scope")
    for key in keys & {
        "id", "operation", "artifact", "revision", "retry_of", "timestamp",
        "replaces", "replacement",
    }:
        if not _identifier(data[key]):
            raise ValueError(f"invalid {key}")
    if kind == "message":
        if data["role"] != origin:
            raise ValueError("message role must match origin")
        if not isinstance(data["text"], str) or not data["text"].strip():
            raise ValueError("message text must be nonempty")
    if ("artifact" in keys) != ("revision" in keys):
        raise ValueError("artifact and revision must be supplied together")
    if "receipt" in keys and not isinstance(data["receipt"], dict):
        raise ValueError("receipt must be a JSON object")
    if "verified" in keys and type(data["verified"]) is not bool:
        raise ValueError("verified must be a boolean")
    return Event(data["id"], kind, Scope(**scope_data), origin, data, fingerprint(normalized))


class Source:
    """One read of an append-only fixture at its opening byte boundary."""

    def __init__(self, file: BinaryIO, *, end: int | None = None):
        self.file = file
        info = os.fstat(file.fileno())
        if end is not None and not 0 <= end <= info.st_size:
            raise ContinuityError("source no longer contains the consumed checkpoint boundary")
        self.end = info.st_size if end is None else end
        if not stat.S_ISREG(info.st_mode) or self.end > MAX_SOURCE_BYTES:
            raise ContinuityError(f"source must be a regular file of at most {MAX_SOURCE_BYTES} bytes")
        self._digest = hashlib.sha256()
        header = self._read_line()
        try:
            data, _ = _json_object(header)
            if (data.keys() != {"schema", "source_id"} or type(data["schema"]) is not int
                    or data["schema"] != SCHEMA or not _identifier(data["source_id"])):
                raise ValueError("expected schema 1 and a nonempty source_id")
        except (ValueError, RecursionError) as exc:
            raise ContinuityError(f"source header: {exc}") from exc
        self.source_id = data["source_id"]
        self.fingerprint = fingerprint(header)

    def _read_line(self) -> bytes:
        offset = self.file.tell()
        line = self.file.readline(min(MAX_LINE_BYTES + 1, self.end - offset))
        if len(line) > MAX_LINE_BYTES:
            raise ContinuityError(f"source record at byte {offset} exceeds {MAX_LINE_BYTES} bytes")
        if not line.endswith(b"\n"):
            raise ContinuityError(f"truncated source record at byte {offset} (expected newline)")
        self._digest.update(line)
        return line

    def resume(self, previous: Boundary) -> None:
        if not self.file.tell() <= previous.offset <= self.end:
            raise ContinuityError("source no longer contains the consumed checkpoint boundary")
        while self.file.tell() < previous.offset:
            chunk = self.file.read(min(MAX_LINE_BYTES, previous.offset - self.file.tell()))
            if not chunk:
                raise ContinuityError("source truncated in consumed prefix")
            self._digest.update(chunk)
        if self.boundary != previous:
            raise ContinuityError("source consumed prefix fingerprint changed")

    @property
    def boundary(self) -> Boundary:
        return Boundary(self.file.tell(), self._digest.hexdigest())

    def __iter__(self) -> Iterator[Event]:
        while self.file.tell() < self.end:
            offset = self.file.tell()
            line = self._read_line()
            try:
                event = _parse_event(line)
            except (ValueError, RecursionError) as exc:
                raise ContinuityError(f"invalid source event at byte {offset}: {exc}") from exc
            yield event
