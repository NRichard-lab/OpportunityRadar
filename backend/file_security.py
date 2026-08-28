from __future__ import annotations

import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


EXPORT_WRITE_LOCK = threading.RLock()
FORMULA_PREFIXES = ("=", "+", "-", "@")
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def sanitize_spreadsheet_value(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    cleaned = _CONTROL_CHARACTERS.sub("", value)
    stripped = cleaned.lstrip()
    if stripped.startswith(FORMULA_PREFIXES):
        return "'" + cleaned
    if _has_unsafe_uri_scheme(stripped):
        return "'" + cleaned
    return cleaned


def atomic_write_text(path: Path, contents: str, *, encoding: str = "utf-8") -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    with EXPORT_WRITE_LOCK:
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding=encoding,
                dir=target.parent,
                prefix=f".{target.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                temporary.write(contents)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_path, target)
            temporary_path = None
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)


def atomic_save_workbook(path: Path, workbook: Any) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    with EXPORT_WRITE_LOCK:
        try:
            descriptor, raw_path = tempfile.mkstemp(
                dir=target.parent,
                prefix=f".{target.name}.",
                suffix=".xlsx.tmp",
            )
            os.close(descriptor)
            temporary_path = Path(raw_path)
            workbook.save(temporary_path)
            with temporary_path.open("r+b") as handle:
                os.fsync(handle.fileno())
            os.replace(temporary_path, target)
            temporary_path = None
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)


def _has_unsafe_uri_scheme(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return True
    return bool(parsed.scheme and parsed.scheme.casefold() not in {"http", "https", "mailto"})
