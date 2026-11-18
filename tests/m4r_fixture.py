"""Callback-first and reset-window simulations. No host imports or access."""

from pathlib import Path

from m4_contract_probe import (
    ANCHOR_FIELDS, CALLBACK_FIELDS, OBSERVATION_FIELDS, _entry_valid, _index, _text, _wire,
)


def _bound(anchor, scope):
    return isinstance(anchor, dict) and all(anchor.get(key) == val for key, val in scope.items())


def _scope_valid(scope):
    return isinstance(scope, dict) and scope.keys() == OBSERVATION_FIELDS and all(
        _text(value) for value in scope.values()
    )


def _anchor_valid(anchor, scope, fields):
    return (
        _bound(anchor, scope) and fields <= anchor.keys() <= fields | {"idempotencyKey"}
        and _text(anchor.get("entryId"))
        and _index(anchor.get("rawSeq")) and _index(anchor.get("activeMessagePosition"))
        and (anchor["effectiveParentId"] is None or _text(anchor["effectiveParentId"]))
    )


def publish(connection, scope, outcome, callback=None):
    """Model an atomic local receipt. Fixture finalization sets the outcome."""
    if outcome in ("rejected", "aborted", "cancelled") and callback is None:
        return "not_accepted"
    if (outcome != "accepted" or not _scope_valid(scope) or not isinstance(callback, dict)
            or not CALLBACK_FIELDS <= callback.keys() <= CALLBACK_FIELDS | {"sessionTarget"}):
        raise ValueError("missing or contradictory acceptance")
    if any(callback.get(key) != scope[key] for key in ("sessionId", "sessionKey")):
        raise ValueError("callback scope mismatch")
    if "sessionTarget" in callback and callback["sessionTarget"] != {
        key: scope[key] for key in ("agentId", "sessionId", "sessionKey", "storePath")
    }:
        raise ValueError("callback target mismatch")

    key = callback.get("advancementKey")
    start, end = callback.get("admission"), callback.get("terminal")
    messages = callback.get("messages")
    if (
        not _text(key) or not _anchor_valid(start, scope, ANCHOR_FIELDS | {"role", "logicalTurnId"})
        or not _anchor_valid(end, scope, ANCHOR_FIELDS)
        or start["entryId"] == end["entryId"] or start["rawSeq"] >= end["rawSeq"]
        or start["activeMessagePosition"] >= end["activeMessagePosition"]
        or start.get("role") != "user" or not _text(start.get("logicalTurnId"))
        or not isinstance(messages, list) or not 2 <= len(messages) <= 64
        or len(messages) != end["activeMessagePosition"] - start["activeMessagePosition"] + 1
        or any(not isinstance(message, dict) or message.get("role") not in
               ("user", "assistant", "toolResult") for message in messages)
        or messages[0]["role"] != "user" or messages[-1]["role"] != "assistant"
        or messages[-1].get("stopReason") != "stop"
    ):
        raise ValueError("unverified accepted range")

    # The accepted batch is authoritative in this scenario. It contributes
    # endpoint IDs and message order, with no invented host entry IDs or cursor.
    payload = _wire(callback, "invalid_callback")
    scoped = _wire(scope, "invalid_scope")
    if connection.in_transaction:
        raise ValueError("fixture publisher requires its own transaction")
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("""
            CREATE TABLE IF NOT EXISTS accepted_batches (
                scope TEXT NOT NULL, advancement_key TEXT NOT NULL, payload TEXT NOT NULL,
                UNIQUE (scope, advancement_key)
            )
        """)
        saved = connection.execute(
            "SELECT payload FROM accepted_batches WHERE scope = ? AND advancement_key = ?",
            (scoped, key),
        ).fetchone()
        if saved is not None:
            if saved[0] != payload:
                raise ValueError("conflicting accepted delivery")
            return "duplicate"
        connection.execute(
            "INSERT INTO accepted_batches VALUES (?, ?, ?)", (scoped, key, payload)
        )
    return "committed"


def preturn(scope, window, admission, requested, result):
    """Use an explicit fixture-only window, never infer host reset authority."""
    if (not isinstance(window, dict) or not _text(window.get("id"))
            or not _index(window.get("first_position"))):
        raise ValueError("unknown reset window")
    if (not _scope_valid(scope)
            or not _anchor_valid(admission, scope, ANCHOR_FIELDS | {"role", "logicalTurnId"})
            or admission.get("role") != "user" or not _text(admission.get("logicalTurnId"))
            or window["first_position"] > admission["activeMessagePosition"]):
        raise ValueError("invalid admission")
    binding = {"scope": scope.copy(), "window": window["id"]}
    cursor_binding = binding | {"first_position": window["first_position"]}
    token = None
    if requested is not None:
        if (not isinstance(requested, dict) or requested.keys() != cursor_binding.keys() | {"token"}
                or any(requested[key] != val for key, val in cursor_binding.items())
                or not _index(requested["first_position"])
                or not _text(requested["token"])):
            raise ValueError("cursor outside current window")
        token = requested["token"]
    if (not isinstance(result, dict) or any(result.get(key) != val for key, val in binding.items())
            or "request_cursor" not in result or result["request_cursor"] != token
            or not _text(result.get("cursor")) or not isinstance(result.get("entries"), list)
            or len(result["entries"]) > 64):
        raise ValueError("invalid pre-turn page")
    if token is not None and not result["entries"] and result["cursor"] != token:
        raise ValueError("empty page changed the cursor")
    selected = []
    previous = 0
    seen = set()
    for entry in result["entries"]:
        if (not _entry_valid(entry) or entry["seq"] <= previous
                or entry["entryId"] in seen
                or entry["seq"] - 1 >= admission["activeMessagePosition"]):
            raise ValueError("page crosses admission or has inconsistent entries")
        previous = entry["seq"]
        seen.add(entry["entryId"])
        if entry["seq"] - 1 >= window["first_position"]:
            selected.append(entry)
    return selected, cursor_binding | {"token": result["cursor"]}


def scratch_path(root, relative):
    """Check a synthetic path without creating it or following symlinks."""
    canonical = root.resolve(strict=True)
    if root != canonical or not canonical.is_dir():
        raise ValueError("expected a canonical scratch directory")
    parts = Path(relative)
    if parts.is_absolute() or not parts.parts or ".." in parts.parts:
        raise ValueError("expected a relative scratch path")
    path = root
    for part in parts.parts:
        path /= part
        if path.is_symlink():
            raise ValueError("symlink in scratch path")
    if not path.resolve().is_relative_to(root):
        raise ValueError("outside scratch root")
    if path.exists() and (not path.is_file() or path.stat().st_nlink != 1):
        raise ValueError("scratch path is not a single-link file")
    return path
