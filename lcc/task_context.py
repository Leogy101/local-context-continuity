"""Consume scoped caller assertions or KRE's pinned public result formats."""

import json
import re
from dataclasses import asdict
from datetime import datetime

from .source import Scope, _unique_object, canonical_json, fingerprint

MAX_TASK_CONTEXT_BYTES = 1024 * 1024
_CONTEXT_TEXT_BYTES = 262144
_CARD_SECTIONS = ("objective", "constraints", "decisions", "questions", "next_actions")
_REF_FIELDS = ("source_id", "revision_id", "chunk_id", "content_sha256")
_ORIGINS = ("user_assertion", "source_evidence", "model_inference")
_RFC3339 = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt][0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]+)?(?:[Zz]|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])"
)


class MissingTaskContext(ValueError):
    """An essential task fact or evidence reference is unavailable."""

    def __init__(self, message: str, *, invalidated: tuple[dict, ...] = ()):
        super().__init__(message)
        self.invalidated = invalidated


class UnsupportedTaskContext(ValueError):
    """The task input is malformed, contradictory or outside the supported contract."""


class TaskContextBudget(ValueError):
    """The KRE result could not fit its required evidence."""


def _keys(value: object, fields: str) -> bool:
    return isinstance(value, dict) and value.keys() == set(fields.split())


def _text(value: object, limit: int, minimum: int = 1) -> bool:
    return isinstance(value, str) and minimum <= len(value.encode("utf-8")) <= limit


def _int(value: object, minimum: int = 0) -> bool:
    return type(value) is int and value >= minimum


def _hash(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _timestamp(value: object) -> bool:
    # The pinned producer uses UTC with millisecond precision. Accept RFC3339
    # offsets as well, within the calendar range supported by that producer.
    if not isinstance(value, str) or len(value) > 128 or _RFC3339.fullmatch(value) is None:
        return False
    try:
        datetime.fromisoformat(value.upper())
    except ValueError:
        return False
    return True


def _copy_bounded_json(value: object) -> dict:
    # Scan for an upper bound on serialization work before copying. A count of
    # elements and string characters is a lower bound on encoded size.
    remaining = MAX_TASK_CONTEXT_BYTES

    def check(node: object, depth: int) -> None:
        nonlocal remaining
        remaining -= 1
        if depth > 16 or remaining < 0:
            raise UnsupportedTaskContext("task context exceeds size or nesting bounds")
        if type(node) is str:
            remaining -= len(node)
        elif type(node) is dict:
            for key, item in node.items():
                if type(key) is not str:
                    raise UnsupportedTaskContext("task context object keys must be strings")
                check(key, depth + 1)
                check(item, depth + 1)
        elif type(node) is list:
            for item in node:
                check(item, depth + 1)
        elif type(node) is int:
            if node.bit_length() > 256:
                raise UnsupportedTaskContext("task context integer exceeds size bound")
        elif type(node) is not bool and node is not None:
            raise UnsupportedTaskContext("task context must contain JSON objects, arrays and scalar values")
        if remaining < 0:
            raise UnsupportedTaskContext("task context exceeds size or nesting bounds")

    check(value, 0)
    try:
        payload = canonical_json(value)
        size = len(payload.encode("utf-8"))
    except (ValueError, RecursionError) as exc:
        raise UnsupportedTaskContext("task context contains invalid JSON data") from exc
    if size > MAX_TASK_CONTEXT_BYTES:
        raise UnsupportedTaskContext("task context exceeds byte limit")
    return json.loads(payload)


def _ref_key(ref: dict) -> tuple:
    return tuple(ref[field] for field in _REF_FIELDS)


def _refs(value: object, limit: int = 32) -> list[dict]:
    if not isinstance(value, list) or len(value) > limit or any(
        not _keys(ref, "source_id revision_id chunk_id content_sha256")
        or not all(_text(ref[field], 512) and len(ref[field]) <= 128
                   and all(ord(char) >= 32 for char in ref[field])
                   for field in _REF_FIELDS[:3])
        or not _hash(ref["content_sha256"])
        for ref in value
    ):
        raise UnsupportedTaskContext("invalid KRE evidence reference")
    if len({_ref_key(ref)[:3] for ref in value}) != len(value):
        raise UnsupportedTaskContext("duplicate or contradictory KRE evidence references")
    return value


def _evidence(value: object) -> dict[tuple, dict]:
    if not isinstance(value, list) or len(value) > 32:
        raise UnsupportedTaskContext("invalid KRE evidence list")
    items = {}
    citations = set()
    refs = []
    for item in value:
        if not _keys(item, "citation_id source_id revision_id chunk_id text content_sha256 "
                          "locator recorded_at authority origin lifecycle freshness egress"):
            raise UnsupportedTaskContext("invalid KRE evidence item")
        locator, egress = item["locator"], item["egress"]
        if (
            not _text(item["citation_id"], 512)
            or len(item["citation_id"]) > 128
            or not _text(item["text"], _CONTEXT_TEXT_BYTES, 0)
            or fingerprint(item["text"].encode("utf-8")) != item["content_sha256"]
            or not _timestamp(item["recorded_at"])
            or item["authority"] not in ("authoritative", "unverified")
            or item["origin"] not in _ORIGINS
            or item["lifecycle"] not in ("active", "superseded")
            or item["freshness"] not in ("fresh", "stale", "unknown")
            or not _keys(egress, "restriction")
            or egress["restriction"] not in ("local_only", "approved_remote")
            or not _keys(locator, "label line_start line_end")
            or not _text(locator["label"], 8192, 0)
            or len(locator["label"]) > 2048
            or not _int(locator["line_start"], 1)
            or not _int(locator["line_end"], locator["line_start"])
        ):
            raise UnsupportedTaskContext("invalid KRE evidence item")
        if item["citation_id"] in citations:
            raise UnsupportedTaskContext("duplicate KRE citation ID")
        citations.add(item["citation_id"])
        refs.append({field: item[field] for field in _REF_FIELDS})
    for ref, item in zip(_refs(refs), value):
        items[_ref_key(ref)] = item
    return items


def _metadata(item: dict) -> dict:
    return {key: value for key, value in item.items() if key != "text"}


def _eligible(item: dict) -> bool:
    return item["lifecycle"] == "active" and item["freshness"] != "stale"


def _warnings(value: object) -> bool:
    return isinstance(value, list) and len(value) <= 32 and all(
        isinstance(message, str) and len(message) <= 128 for message in value
    )


def _context_0_1(context: object) -> dict[tuple, dict]:
    if (
        not _keys(context, "contract_version request_id status context_revision generated_at "
                          "scope_fingerprint items context_text token_usage coverage omissions warnings error")
        or context["contract_version"] != "0.1"
        or context["status"] not in ("ok", "partial", "error")
        or context["coverage"] not in ("sufficient", "partial", "unknown")
        or not _timestamp(context["generated_at"])
        or not all(_text(context[field], 512) and len(context[field]) <= 128 for field in (
            "request_id", "context_revision", "scope_fingerprint"
        ))
        or not _warnings(context["warnings"])
        or not isinstance(context["omissions"], dict)
        or len(context["omissions"]) > 8
        or any(not _int(count) for count in context["omissions"].values())
        or not _text(context["context_text"], _CONTEXT_TEXT_BYTES, 0)
    ):
        raise UnsupportedTaskContext("invalid KRE context 0.1 envelope")

    text, usage, error = context["context_text"], context["token_usage"], context["error"]
    if (
        not _keys(usage, "estimated_tokens counter_kind counter_id counter_revision context_bytes includes_citations")
        or not _int(usage["estimated_tokens"])
        or usage["counter_kind"] not in ("exact", "conservative_bytes")
        or not all(usage[field] is None or isinstance(usage[field], str)
                   and len(usage[field]) <= 128 for field in ("counter_id", "counter_revision"))
        or not _int(usage["context_bytes"])
        or usage["context_bytes"] != len(text.encode("utf-8"))
        or usage["includes_citations"] is not True
    ):
        raise UnsupportedTaskContext("invalid KRE context accounting")

    evidence = _evidence(context["items"])
    if context["status"] == "error":
        if (
            evidence or text or not _keys(error, "code message retryable")
            or not _text(error["code"], 128)
            or not _text(error["message"], 4096, 0)
            or len(error["message"]) > 1024
            or type(error["retryable"]) is not bool
        ):
            raise UnsupportedTaskContext("invalid KRE context error")
    elif error is not None:
        raise UnsupportedTaskContext("conflicting KRE context status and error")

    # The pinned public fixture encodes the same items as JSON in context_text.
    # Verify correspondence and carry only one copy of the excerpt text forward.
    try:
        mirrored = json.loads(text, object_pairs_hook=_unique_object) if text else []
        matches = canonical_json(mirrored) == canonical_json(context["items"])
    except (ValueError, RecursionError):
        matches = False
    if not matches:
        raise UnsupportedTaskContext("KRE context text and evidence items do not agree")
    return evidence


def _parse_required(result: dict) -> dict:
    if not _keys(result, "wrapper_version status context required error"):
        raise UnsupportedTaskContext("invalid KRE required-evidence envelope")
    context, required = result["context"], result["required"]
    evidence = _context_0_1(context)
    if (
        result["status"] != context["status"] or result["error"] != context["error"]
        or not _keys(required, "requested delivered missing")
    ):
        raise UnsupportedTaskContext("conflicting KRE required-evidence envelope")
    requested = [_ref_key(ref) for ref in _refs(required["requested"])]
    delivered = [_ref_key(ref) for ref in _refs(required["delivered"])]
    if delivered != [ref for ref in requested if ref in evidence]:
        raise UnsupportedTaskContext("KRE delivered references disagree with evidence items")

    missing = required["missing"]
    if not isinstance(missing, list) or len(missing) > 32 or any(
        not _keys(item, "ref reason") or item["reason"] not in ("unavailable", "budget", "request_error")
        for item in missing
    ):
        raise UnsupportedTaskContext("invalid KRE missing evidence report")
    missing_refs = [_ref_key(ref) for ref in _refs([item["ref"] for item in missing])]
    if any(ref not in requested or ref in delivered for ref in missing_refs):
        raise UnsupportedTaskContext("conflicting KRE missing evidence report")
    if context["status"] == "error":
        if context["error"]["code"] == "BUDGET_TOO_SMALL":
            raise TaskContextBudget("KRE required evidence could not fit its context budget")
        if context["error"]["code"] in ("INVALID_REQUEST", "INVALID_REQUIRED_REFS"):
            raise UnsupportedTaskContext("KRE rejected the evidence request as malformed")
        raise MissingTaskContext("KRE required evidence is unavailable")
    if missing_refs or delivered != requested or not evidence:
        raise MissingTaskContext("KRE required evidence is unavailable")
    if any(not _eligible(evidence[ref]) for ref in requested):
        raise MissingTaskContext("KRE required evidence is stale or superseded")
    return {
        "contract": "kre-required/1",
        **{field: context[field] for field in ("request_id", "status", "context_revision",
            "generated_at", "scope_fingerprint", "context_text", "coverage", "omissions", "warnings")},
        "evidence": [_metadata(item) for item in evidence.values()],
        "required": required,
    }


def _parse_resume(result: dict, scope: Scope) -> dict:
    if (
        not _keys(result, "task_version task_id status card evidence invalidated recorded_at "
                         "card_sha256 context_revision scope_fingerprint warnings")
        or result["task_id"] != scope.task
        or result["status"] not in ("ok", "partial")
        or not _keys(result["card"], "objective constraints decisions questions next_actions")
        or not _hash(result["card_sha256"])
        or not _timestamp(result["recorded_at"])
        or not all(_text(result[field], 128) for field in ("context_revision", "scope_fingerprint"))
        or not _warnings(result["warnings"])
    ):
        raise UnsupportedTaskContext("invalid KRE task resume envelope")
    card, invalidated = result["card"], result["invalidated"]
    if not isinstance(invalidated, list) or len(invalidated) > 65 or any(
        not _keys(item, "section index reason") or item["section"] not in _CARD_SECTIONS
        or not _int(item["index"]) or item["index"] >= (1 if item["section"] == "objective" else 16)
        or item["reason"] != "evidence_unavailable"
        for item in invalidated
    ) or bool(invalidated) != (result["status"] == "partial"):
        raise UnsupportedTaskContext("invalid KRE task invalidation report")
    if invalidated:
        raise MissingTaskContext(
            "KRE task resume invalidated task context.",
            invalidated=tuple(invalidated),
        )
    if card["objective"] is None:
        raise MissingTaskContext("KRE task resume has no required objective.")
    card_bytes = canonical_json(card).encode("utf-8")
    if len(card_bytes) > 32768 or fingerprint(card_bytes) != result["card_sha256"]:
        raise UnsupportedTaskContext("KRE task card digest or size is invalid")

    evidence = _evidence(result["evidence"])
    present = set()
    for section in _CARD_SECTIONS:
        entries = [card[section]] if section == "objective" else card[section]
        if not isinstance(entries, list) or len(entries) > 16:
            raise UnsupportedTaskContext("invalid KRE task card section")
        for entry in entries:
            if (
                not _keys(entry, "text origin refs") or not _text(entry["text"], 8192)
                or "\x00" in entry["text"] or entry["origin"] not in _ORIGINS
            ):
                raise UnsupportedTaskContext("invalid KRE task card entry")
            refs = [_ref_key(ref) for ref in _refs(entry["refs"], 8)]
            if entry["origin"] != "user_assertion" and not refs:
                raise MissingTaskContext("KRE task entry lacks its required evidence reference")
            if any(ref not in evidence or not _eligible(evidence[ref]) for ref in refs):
                raise MissingTaskContext("KRE task dependency is unavailable or ineligible")
            present.update(refs)
            if entry["origin"] == "source_evidence":
                if (
                    any(evidence[ref]["origin"] != "source_evidence" for ref in refs)
                    or entry["text"] != "\n\n".join(evidence[ref]["text"] for ref in refs)
                ):
                    raise UnsupportedTaskContext("KRE task excerpt disagrees with its evidence")
                # The card keeps its structure and exact ordered references.
                # Evidence carries the one copy shared by any card sections.
                del entry["text"]
    if present != evidence.keys():
        raise UnsupportedTaskContext("KRE task dependencies disagree with evidence items")
    return {
        "contract": "kre-task/1",
        **{field: result[field] for field in ("task_id", "status", "card", "invalidated",
            "recorded_at", "context_revision", "scope_fingerprint", "warnings")},
        "input_card_sha256": result["card_sha256"],
        "evidence": list(evidence.values()),
    }


def parse_task_context(value: object, scope: Scope) -> dict:
    """Validate an explicit local snapshot without consulting or updating KRE."""
    if value is None:
        raise MissingTaskContext("required task context was not supplied")
    if not isinstance(value, dict):
        raise UnsupportedTaskContext("task context must be a scoped object")
    data = _copy_bounded_json(value)
    kind = data.get("kind")
    if data.get("scope") != asdict(scope):
        raise UnsupportedTaskContext("task context scope differs from the requested scope")
    ids = data.get("required_event_ids", [])
    if (
        not isinstance(ids, list) or len(ids) > 128
        or not all(isinstance(event_id, str) and 1 <= len(event_id) <= 512 for event_id in ids)
        or len(set(ids)) != len(ids)
    ):
        raise UnsupportedTaskContext("invalid required event ID list in task context")
    if kind == "caller":
        if not _keys(data, "kind scope provenance context_text required_event_ids"):
            raise UnsupportedTaskContext("invalid caller snapshot envelope")
        if not _text(data["provenance"], 512) or not data["provenance"].strip():
            raise UnsupportedTaskContext("caller snapshot requires an explicit provenance")
        if not isinstance(data["context_text"], str) or not data["context_text"].strip():
            raise MissingTaskContext("caller snapshot has no required task context")
        return {
            "kind": "caller", "status": "caller-asserted", "provenance": data["provenance"],
            "context_text": data["context_text"], "required_event_ids": ids,
        }
    if kind != "kre" or data.keys() not in (
        {"kind", "scope", "result"}, {"kind", "scope", "result", "required_event_ids"}
    ) or not isinstance(data["result"], dict):
        raise UnsupportedTaskContext("unsupported task context envelope")
    result = data["result"]
    if result.get("wrapper_version") == "kre-required/1":
        normalized = _parse_required(result)
    elif result.get("task_version") == "kre-task/1":
        normalized = _parse_resume(result, scope)
    else:
        raise UnsupportedTaskContext("unsupported KRE result contract")
    return {
        "kind": "kre", "snapshot_validation": "structure-and-references-checked",
        "required_event_ids": ids, **normalized,
    }
