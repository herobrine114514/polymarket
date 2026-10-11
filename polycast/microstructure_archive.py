"""Lossless gzip archival for explicitly sealed public logs; no retention deletion."""
from __future__ import annotations
import gzip
import hashlib
import json
import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

NAME = re.compile(r"raw-([a-zA-Z0-9-]+)-(\d{5})\.jsonl\Z")
CHUNK = 1024 * 1024


class ArchiveFailure(RuntimeError):
    def __init__(self, filename: str, reason: str):
        super().__init__(reason)
        self.filename = filename


def digest_stream(stream) -> tuple[str, int]:
    digest, length = hashlib.sha256(), 0
    while block := stream.read(CHUNK):
        digest.update(block)
        length += len(block)
    return digest.hexdigest(), length


def contained_file(directory: Path, name: str) -> Path:
    root = directory.resolve()
    target = root / name
    if target.is_symlink() or target.resolve().parent != root:
        raise ValueError("Archive target leaves its named data directory")
    return target


def publish_once(path: Path, value: dict) -> bool:
    descriptor, temporary_name = tempfile.mkstemp(prefix=".pending-archive-index-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(value, ensure_ascii=False, allow_nan=False))
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            return False
        return True
    finally:
        temporary.unlink(missing_ok=True)


def read_seal(directory: Path, path: Path) -> tuple[Path, dict]:
    if path.is_symlink() or path.resolve().parent != directory.resolve():
        raise ValueError("Invalid seal location")
    seal = json.loads(path.read_text(encoding="utf-8"))
    name = seal.get("filename")
    match = NAME.fullmatch(name) if isinstance(name, str) else None
    if (not match or path.name != name + ".seal.json" or type(seal.get("schema_version")) is not int or
            seal["schema_version"] != 1 or seal.get("session_id") != match[1] or
            type(seal.get("bytes")) is not int or seal["bytes"] < 1 or
            not isinstance(seal.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", seal["sha256"])):
        raise ValueError("Invalid closed segment seal")
    return contained_file(directory, name), seal


def archive_segment(directory: Path, seal_path: Path, *, active_name: str | None = None) -> dict | None:
    """Publish a verified gzip then remove only its byte-equivalent original."""
    source, seal = read_seal(directory, seal_path)
    if source.name == active_name:
        return None
    output = contained_file(directory, source.name + ".gz")
    index = contained_file(directory, source.name + ".archive.json")
    if not source.exists():
        if index.is_file() and output.is_file():
            return None
        raise ValueError("Closed segment has no verifiable original/archive")
    if not source.is_file():
        raise ValueError("Closed segment is not a regular file")
    before = source.stat()
    if before.st_size != seal["bytes"]:
        raise ValueError("Original length differs from closed seal")
    temporary: Path | None = None
    try:
        if not output.exists():
            if shutil.disk_usage(directory).free < before.st_size + 64 * 1024 ** 2:
                raise OSError("Insufficient archival staging capacity")
            descriptor, name = tempfile.mkstemp(prefix=".pending-gzip-", dir=directory)
            temporary = Path(name)
            original_digest, original_length = hashlib.sha256(), 0
            with source.open("rb") as incoming, os.fdopen(descriptor, "wb") as target:
                with gzip.GzipFile(filename="", mode="wb", fileobj=target, compresslevel=6, mtime=0) as compressed:
                    while block := incoming.read(CHUNK):
                        original_digest.update(block)
                        original_length += len(block)
                        compressed.write(block)
                target.flush()
                os.fsync(target.fileno())
            if original_digest.hexdigest() != seal["sha256"] or original_length != seal["bytes"]:
                raise ValueError("Original content differs from closed seal")
            candidate = temporary
        else:
            # A crash may have published gzip without its index. Check both before recovering.
            with source.open("rb") as incoming:
                sha, size = digest_stream(incoming)
            if sha != seal["sha256"] or size != seal["bytes"]:
                raise ValueError("Changed original cannot be retired")
            candidate = output
        with gzip.open(candidate, "rb") as incoming:
            restored_digest, restored_size = digest_stream(incoming)
        if restored_digest != seal["sha256"] or restored_size != seal["bytes"]:
            raise ValueError("Gzip round-trip differs from original")
        if temporary is not None:
            try:
                os.link(temporary, output)
            except FileExistsError:
                with gzip.open(output, "rb") as incoming:
                    sha, size = digest_stream(incoming)
                if sha != seal["sha256"] or size != seal["bytes"]:
                    raise ValueError("Conflicting archive already exists")
        with output.open("rb") as incoming:
            compressed_digest, compressed_size = digest_stream(incoming)
        result = {"schema_version": 1, "method": "gzip-roundtrip-sha256-v1", "filename": source.name,
                  "archive": output.name, "raw_sha256": seal["sha256"], "raw_bytes": seal["bytes"],
                  "archive_sha256": compressed_digest, "archive_bytes": compressed_size,
                  "first_record_seq": seal.get("first_record_seq"), "last_record_seq": seal.get("last_record_seq"),
                  "archived_at": datetime.now(timezone.utc).isoformat(), "bytes_saved": seal["bytes"] - compressed_size}
        if not publish_once(index, result):
            existing = json.loads(index.read_text(encoding="utf-8"))
            if any(existing.get(key) != result[key] for key in ("raw_sha256", "raw_bytes", "archive_sha256", "archive_bytes")):
                raise ValueError("Conflicting archive index")
        after = source.stat()
        if (after.st_size, after.st_mtime_ns, after.st_ino) != (before.st_size, before.st_mtime_ns, before.st_ino):
            raise ValueError("Original changed during archival")
        # Re-resolve the literal original immediately before retiring it, within the same directory.
        contained_file(directory, source.name).unlink()
        return result
    finally:
        if temporary is not None:
            contained_file(directory, temporary.name).unlink(missing_ok=True)


def archive_one(directory: Path, *, active_name: str | None = None, skip: set[str] | None = None) -> dict | None:
    if not directory.is_dir():
        return None
    for path in sorted(directory.glob("raw-*.jsonl.seal.json")):
        source_name = path.name.removesuffix(".seal.json")
        if source_name == active_name or source_name in (skip or set()) or not (directory / source_name).exists():
            continue
        try:
            return archive_segment(directory, path, active_name=active_name)
        except (OSError, ValueError, EOFError, KeyError, TypeError) as exc:
            raise ArchiveFailure(source_name, f"{type(exc).__name__}: {str(exc)[:140]}") from exc
    return None


def seal_closed_session(directory: Path, session_id: str) -> list[str]:
    """Explicit migration only after the caller verifies this session's writer exited."""
    if not re.fullmatch(r"[a-zA-Z0-9-]+", session_id):
        raise ValueError("Invalid explicitly closed session")
    published = []
    for path in sorted(directory.glob(f"raw-{session_id}-*.jsonl")):
        match = NAME.fullmatch(path.name)
        if not match or match[1] != session_id:
            continue
        contained_file(directory, path.name)
        digest, size, first, last = hashlib.sha256(), 0, None, None
        with path.open("rb") as stream:
            for line in stream:
                if not line.endswith(b"\n"):
                    raise ValueError("Incomplete legacy segment retained")
                item = json.loads(line)
                sequence = item.get("record_seq")
                if (item.get("session_id") != session_id or type(sequence) is not int or sequence < 1 or
                        last is not None and sequence != last + 1):
                    raise ValueError("Legacy segment identity/sequence mismatch")
                first = first or sequence
                last = sequence
                digest.update(line)
                size += len(line)
        if not size:
            continue
        seal = {"schema_version": 1, "filename": path.name, "session_id": session_id, "bytes": size,
                "sha256": digest.hexdigest(), "first_record_seq": first, "last_record_seq": last,
                "migration": "writer-exit-verified-by-caller", "closed_at_ms": int(datetime.now(timezone.utc).timestamp() * 1000)}
        if publish_once(directory / (path.name + ".seal.json"), seal):
            published.append(path.name)
    return published
