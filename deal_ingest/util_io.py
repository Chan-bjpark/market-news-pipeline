"""Shared IO utilities for deal_ingest.

Critical: atomic_write_json includes fsync to defeat OS write-back cache
loss during PC sleep/hibernate transitions. The original implementation
(plain write_text + replace) caused mid-write corruption on 2026-05-16
even though Python logged success — OS buffered the writes but the
buffered pages never flushed to disk before sleep.
"""

from __future__ import annotations

import json
import os
from pathlib import Path


def atomic_write_json(out_path: Path, payload, validate: bool = True) -> None:
    """Atomic JSON write with fsync + post-write validation.

    Steps:
      1. Serialize payload to JSON string (in memory, all-or-nothing)
      2. Write to <out>.tmp, fsync to force disk flush
      3. os.replace(tmp, out) — atomic rename on same filesystem
      4. fsync the directory (POSIX) so the rename itself is durable
      5. Re-open out_path and json.load to verify integrity

    Raises:
      json.JSONDecodeError if post-write validation fails (caller
      should treat this as corruption signal and write a fail flag).
    """
    out_path = Path(out_path)
    body = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")

    # Write tmp and force flush to disk before rename
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(body)
        f.flush()
        os.fsync(f.fileno())

    # Atomic rename
    os.replace(tmp, out_path)

    # POSIX: fsync parent dir so the rename metadata is also durable.
    # On Windows this no-ops (os.open of a directory not supported);
    # NTFS journals the rename itself so this is best-effort only.
    try:
        dir_fd = os.open(str(out_path.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except (OSError, AttributeError, PermissionError):
        pass

    # Post-write validation — re-read from disk and parse
    if validate:
        with open(out_path, "r", encoding="utf-8") as f:
            json.load(f)  # raises on corruption


def validate_json_file(path: Path) -> tuple[bool, str, int]:
    """Read+parse a JSON file. Returns (ok, error_message, size_bytes)."""
    path = Path(path)
    if not path.exists():
        return False, "file does not exist", 0
    size = path.stat().st_size
    try:
        with open(path, "r", encoding="utf-8") as f:
            json.load(f)
        return True, "", size
    except json.JSONDecodeError as e:
        return False, f"JSONDecodeError: {e}", size
    except Exception as e:
        return False, f"{type(e).__name__}: {e}", size
