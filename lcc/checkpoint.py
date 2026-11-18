"""Publish one scoped projection and its consumed source boundary together."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from dataclasses import asdict
from pathlib import Path

from .projection import Projection, Snapshot
from .source import Boundary, ContinuityError, Event, Scope, Source, canonical_json, fingerprint

_CHECKPOINT_VERSION = 1


def _decode(row: tuple, scope: Scope) -> tuple[dict, Boundary]:
    payload, checksum = row
    if not isinstance(payload, str) or fingerprint(payload.encode("utf-8")) != checksum:
        raise ContinuityError("checkpoint integrity check failed")
    try:
        record = json.loads(payload)
        if type(record["schema"]) is not int or record["schema"] != _CHECKPOINT_VERSION:
            raise ValueError("unsupported schema")
        boundary = Boundary(**record["boundary"])
        if type(boundary.offset) is not int:
            raise ValueError("invalid consumed offset")
        saved_scope = Scope(**record["scope"])
    except (KeyError, TypeError, ValueError, RecursionError) as exc:
        raise ContinuityError(f"invalid checkpoint metadata: {exc}") from exc
    if saved_scope != scope:
        raise ContinuityError("checkpoint scope differs from requested scope")
    return record, boundary


def _check_source(record: dict, source: Source) -> None:
    if record.get("source") != [source.source_id, source.fingerprint]:
        raise ContinuityError("source identity or header fingerprint changed")


def _restore(row: tuple, source: Source, scope: Scope) -> Projection:
    record, boundary = _decode(row, scope)
    _check_source(record, source)
    source.resume(boundary)
    return Projection.from_record(record.get("projection"))


def _check_paths(source: Path, checkpoint: Path) -> None:
    if not source.is_file():
        raise ContinuityError("source must be an existing regular file")
    if not checkpoint.name or (checkpoint.exists() and not checkpoint.is_file()):
        raise ContinuityError("checkpoint path must name a regular database file")
    # SQLite can remove or overwrite its journal files while opening the DB.
    # Check inode aliases and both direct and symlink-resolved filenames.
    for database_path in {checkpoint, Path(os.path.realpath(checkpoint))}:
        for suffix in ("", "-journal", "-wal", "-shm"):
            candidate = database_path.with_name(database_path.name + suffix)
            if candidate.exists() and source.samefile(candidate):
                raise ContinuityError("source collides with the checkpoint or a SQLite sidecar")


def recover(
    source_path: str | os.PathLike[str],
    checkpoint_path: str | os.PathLike[str],
    scope: Scope,
) -> Snapshot:
    """Ingest the fixed opening extent and atomically publish it for this scope."""
    source_path = Path(source_path)
    # An absolute filename also avoids SQLite treating a name like :memory:
    # as a request for nondurable storage.
    checkpoint_path = Path(checkpoint_path).absolute()
    try:
        _check_paths(source_path, checkpoint_path)
        with closing(sqlite3.connect(checkpoint_path, isolation_level=None)) as database:
            database.execute("PRAGMA synchronous = FULL")
            # Lock before reading the prior boundary so concurrent publishers
            # cannot commit an older snapshot over a newer one.
            database.execute("BEGIN IMMEDIATE")
            database.execute("""
                CREATE TABLE IF NOT EXISTS checkpoint (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    payload TEXT NOT NULL,
                    sha256 TEXT NOT NULL
                )
            """)
            row = database.execute("SELECT payload, sha256 FROM checkpoint WHERE id = 1").fetchone()
            with source_path.open("rb") as stream:
                source = Source(stream)
                projection = Projection() if row is None else _restore(row, source, scope)
                prior = source.boundary
                for event in source:
                    if event.scope == scope:
                        projection.apply(event)
                boundary = source.boundary

            snapshot = projection.snapshot(boundary.cursor)
            if row is None or boundary != prior:
                payload = canonical_json({
                    "schema": _CHECKPOINT_VERSION,
                    "scope": asdict(scope),
                    "source": [source.source_id, source.fingerprint],
                    "boundary": asdict(boundary),
                    "projection": projection.to_record(),
                })
                database.execute("""
                    INSERT INTO checkpoint (id, payload, sha256) VALUES (1, ?, ?)
                    ON CONFLICT (id) DO UPDATE SET payload=excluded.payload, sha256=excluded.sha256
                """, (payload, fingerprint(payload.encode("utf-8"))))
            database.commit()
            return snapshot
    except (OSError, sqlite3.Error) as exc:
        raise ContinuityError(f"source or checkpoint I/O: {exc}") from exc


def _read_committed(
    source_path: str | os.PathLike[str],
    checkpoint_path: str | os.PathLike[str],
    scope: Scope,
) -> tuple[Snapshot, list[Event], dict[str, str]]:
    """Read the committed row and verify the source prefix without publishing."""
    source_path = Path(source_path)
    checkpoint_path = Path(checkpoint_path).absolute()
    try:
        _check_paths(source_path, checkpoint_path)
        if not checkpoint_path.is_file():
            raise ContinuityError("no committed checkpoint")
        with checkpoint_path.open("rb") as stream:
            header = stream.read(20)
        # Even a read-only WAL connection can create sidecars. M1's default
        # rollback-journal mode supports a locking read without those writes.
        if header[:16] != b"SQLite format 3\0" or header[18:20] != b"\x01\x01":
            raise ContinuityError("expected a rollback-journal SQLite checkpoint")
        uri = checkpoint_path.as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, isolation_level=None)) as database:
            row = database.execute("SELECT payload, sha256 FROM checkpoint WHERE id = 1").fetchone()
        if row is None:
            raise ContinuityError("no committed checkpoint")

        record, boundary = _decode(row, scope)
        with source_path.open("rb") as stream:
            source = Source(stream, end=boundary.offset)
            _check_source(record, source)
            events = [event for event in source if event.scope == scope]
            if source.boundary != boundary:
                raise ContinuityError("source consumed prefix fingerprint changed")
        projection = Projection.from_record(record.get("projection"))
        source_info = {"source_id": source.source_id, "fingerprint": source.fingerprint}
        return projection.snapshot(boundary.cursor), events, source_info
    except (OSError, sqlite3.Error) as exc:
        raise ContinuityError(f"source or checkpoint I/O: {exc}") from exc
