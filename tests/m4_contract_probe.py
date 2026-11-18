"""Offline fixture probe for a synthetic OpenClaw SDK shape.

The caller supplies observations and a separate simulated host callback.
This module has no host access, installer, cursor writer, or dispatcher.
"""

from copy import deepcopy
import json

VERSION = "fixture-version-1"
BUILD_ID = "fixture-build-a"
STORE = "/synthetic/m4/session-store"
TARGET_FIELDS = {"agentId", "sessionId", "sessionKey"}
OBSERVATION_FIELDS = TARGET_FIELDS | {"storePath", "generation"}
ENTRY_FIELDS = {"entryId", "parentId", "seq", "role", "message"}
PAGE_FIELDS = {"kind", "cursor", "entries", "hasMore", "serializedBytes"}
CALLBACK_FIELDS = {"sessionId", "sessionKey", "advancementKey", "admission", "terminal", "messages"}
ANCHOR_FIELDS = OBSERVATION_FIELDS | {
    "entryId", "rawSeq", "effectiveParentId", "activeMessagePosition",
}
RESET_REASONS = {
    "anchor_missing", "anchor_moved", "generation_mismatch", "invalid_cursor", "scope_mismatch",
}


class Refused(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _text(value):
    return isinstance(value, str) and 0 < len(value) <= 512 and bool(value.strip())


def _index(value):
    return type(value) is int and value >= 0


def _wire(value, code):
    try:
        data = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if len(data.encode("utf-8")) > 65536:
            raise Refused(code)
    except (TypeError, ValueError, RecursionError) as exc:
        raise Refused(code) from exc
    return data


def _entry_valid(entry):
    if not isinstance(entry, dict) or not ENTRY_FIELDS <= entry.keys() <= (
        ENTRY_FIELDS | {"createdAt", "idempotencyKey"}
    ):
        return False
    message = entry["message"]
    return (
        _text(entry["entryId"])
        and (entry["parentId"] is None or _text(entry["parentId"]))
        and _index(entry["seq"])
        and entry["role"] in ("user", "assistant", "toolResult")
        and isinstance(message, dict) and message.get("role") == entry["role"]
        and all(_text(entry[key]) for key in entry.keys() & {"createdAt", "idempotencyKey"})
    )


def _endpoint_text(entry):
    content = entry["message"].get("content")
    if entry["role"] == "user" and isinstance(content, str) and content.strip():
        return content
    if isinstance(content, list) and content and all(
        isinstance(block, dict) and block.keys() == {"type", "text"} and block["type"] == "text"
        and isinstance(block["text"], str)
        for block in content
    ):
        text = "".join(block["text"] for block in content)
        if text.strip():
            return text
    raise Refused("unverified_turn")


class Probe:
    """Simulate a bounded stream of visible pages and explicit turn receipts."""

    def __init__(self, version, build_id, target):
        if (version, build_id) != (VERSION, BUILD_ID):
            raise Refused("unsupported_build")
        if not isinstance(target, dict) or target.keys() != OBSERVATION_FIELDS or not all(
            _text(value) for value in target.values()
        ) or target["storePath"] != STORE:
            raise Refused("scope_mismatch")
        # These route facts are supplied by the fixture harness. The installed
        # visible page does not itself give the reader an acceptance binding.
        self._target = target.copy()
        self.cursor = None
        self.entries = {}
        self.committed = {}
        self.reset_required = False
        self._readable = False
        self._last = None
        self._accepted_ids = set()

    def observe(self, target, requested_cursor, result):
        if target != self._target:
            raise Refused("scope_mismatch")
        if self.reset_required:
            return "blocked_reset"
        if not isinstance(result, dict):
            raise Refused("invalid_page")
        serialized = _wire(result, "invalid_page")
        if self._last == (requested_cursor, serialized):
            return "duplicate"
        if requested_cursor != self.cursor:
            raise Refused("stale_cursor")
        kind = result.get("kind")
        if kind == "reset":
            if (result.keys() != {"kind", "reason", "cursor"}
                    or result["reason"] not in tuple(RESET_REASONS)
                    or not _text(result.get("cursor"))):
                raise Refused("invalid_page")
            self.reset_required = True
            return "blocked_reset"
        if kind == "missing" and result.keys() == {"kind"}:
            self._readable = False
            return "missing"
        if (kind == "unavailable" and result.keys() == {"kind", "reason"}
                and result["reason"] == "projection_rebuilding"):
            self._readable = False
            return "unavailable"
        entries = result.get("entries")
        next_cursor = result.get("cursor")
        if (kind != "page" or not PAGE_FIELDS <= result.keys() <= PAGE_FIELDS | {"requiredBytes"}
                or not _text(next_cursor) or not isinstance(entries, list)
                or len(entries) > 64 or type(result.get("hasMore")) is not bool
                or not _index(result.get("serializedBytes"))):
            raise Refused("invalid_page")
        if not entries and self.cursor is not None and next_cursor != self.cursor:
            raise Refused("invalid_page")
        if "requiredBytes" in result:
            if (entries or result["serializedBytes"] or not result["hasMore"]
                    or not _index(result["requiredBytes"]) or result["requiredBytes"] == 0):
                raise Refused("invalid_page")
            self._readable = False
            return "oversized"
        if bool(entries) != bool(result["serializedBytes"]) or not entries and result["hasMore"]:
            raise Refused("invalid_page")
        pending = {}
        for entry in entries:
            if not _entry_valid(entry):
                raise Refused("invalid_page")
            entry_id = entry["entryId"]
            if entry_id in self.entries and (
                _wire(self.entries[entry_id], "invalid_page") != _wire(entry, "invalid_page")
            ):
                self.reset_required = True
            if entry_id in pending or entry_id in self.entries:
                raise Refused("entry_conflict")
            if entry["seq"] != len(self.entries) + len(pending) + 1:
                raise Refused("invalid_page")
            pending[entry_id] = entry
        if next_cursor == self.cursor and pending:
            raise Refused("invalid_page")
        self.entries.update(deepcopy(pending))
        self.cursor = next_cursor
        self._last = requested_cursor, serialized
        self._readable = True
        return "observed"

    def _anchor(self, anchor, fields):
        if not isinstance(anchor, dict) or any(
            anchor.get(key) != value for key, value in self._target.items()
        ):
            raise Refused("scope_mismatch")
        if (not fields <= anchor.keys() <= fields | {"idempotencyKey"}
                or not _text(anchor.get("entryId"))
                or not _index(anchor.get("rawSeq")) or not _index(anchor.get("activeMessagePosition"))):
            raise Refused("unverified_turn")
        entry = self.entries.get(anchor["entryId"])
        if entry is not None and anchor["activeMessagePosition"] + 1 != entry["seq"]:
            raise Refused("unverified_turn")
        return entry

    def commit(self, callback):
        if self.reset_required:
            raise Refused("blocked_reset")
        if not isinstance(callback, dict) or any(
            callback.get(key) != self._target[key] for key in ("sessionId", "sessionKey")
        ):
            raise Refused("scope_mismatch")
        if not CALLBACK_FIELDS <= callback.keys() <= CALLBACK_FIELDS | {"sessionTarget"}:
            raise Refused("unverified_turn")
        if "sessionTarget" in callback and callback["sessionTarget"] != {
            field: self._target[field] for field in TARGET_FIELDS | {"storePath"}
        }:
            raise Refused("scope_mismatch")
        key = callback.get("advancementKey")
        if not _text(key):
            raise Refused("unverified_turn")
        serialized = _wire(callback, "unverified_turn")
        if key in self.committed:
            if self.committed[key] != serialized:
                raise Refused("advancement_conflict")
            return "duplicate", []
        if not self._readable:
            raise Refused("unverified_turn")
        admission, terminal = callback.get("admission"), callback.get("terminal")
        start = self._anchor(admission, ANCHOR_FIELDS | {"role", "logicalTurnId"})
        end = self._anchor(terminal, ANCHOR_FIELDS)
        if (not start or not end or admission.get("role") != "user"
                or not _text(admission.get("logicalTurnId")) or start["role"] != "user"
                or end["role"] != "assistant"
                or end["parentId"] != start["entryId"]
                or terminal.get("effectiveParentId") != start["entryId"]
                or admission.get("effectiveParentId") != start["parentId"]
                or terminal["activeMessagePosition"] != admission["activeMessagePosition"] + 1
                or admission["rawSeq"] >= terminal["rawSeq"]
                or _wire(callback.get("messages"), "unverified_turn") !=
                _wire([start["message"], end["message"]], "unverified_turn")):
            raise Refused("unverified_turn")
        if end["message"].get("stopReason") != "stop":
            raise Refused("unsettled_turn")
        entry_ids = {start["entryId"], end["entryId"]}
        if self._accepted_ids.intersection(entry_ids):
            raise Refused("advancement_conflict")
        scope = {
            "task": self._target["sessionKey"], "session": self._target["sessionId"],
            "workflow": self._target["agentId"],
        }
        events = [
            {"id": entry["entryId"], "kind": "message", "scope": scope, "origin": entry["role"],
             "role": entry["role"], "text": _endpoint_text(entry)}
            for entry in (start, end)
        ]
        self.committed[key] = serialized
        self._accepted_ids.update(entry_ids)
        return "committed", events
