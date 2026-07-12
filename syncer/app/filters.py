from __future__ import annotations

import fnmatch

from app.models import SUPPORTED_EXTENSIONS


def parse_csv(value: str) -> list[str]:
    return [item.strip() for item in (value or "").split(",") if item.strip()]


def is_hidden(path: str) -> bool:
    return any(part.startswith(".") for part in path.replace("\\", "/").split("/"))


def is_excluded(path: str, patterns: list[str]) -> bool:
    path = path.replace("\\", "/").strip("/")
    parts = path.split("/")
    for pattern in patterns:
        pattern = pattern.replace("\\", "/").strip()
        if pattern.endswith("/"):
            name = pattern.rstrip("/")
            if name in parts[:-1]:
                return True
        elif "/" in pattern:
            if fnmatch.fnmatch(path, pattern):
                return True
        elif fnmatch.fnmatch(parts[-1], pattern):
            return True
    return False


def is_included(path: str, extensions: list[str]) -> bool:
    if not extensions:
        extensions = list(SUPPORTED_EXTENSIONS)
    name = path.rsplit("/", 1)[-1]
    if "." not in name:
        return False
    return name.rsplit(".", 1)[-1].lower() in {e.lower().lstrip(".") for e in extensions}


def in_scope(path: str, include_extensions: str, sync_hidden: bool, excludes: str) -> bool:
    if not sync_hidden and is_hidden(path):
        return False
    if is_excluded(path, parse_csv(excludes)):
        return False
    return is_included(path, parse_csv(include_extensions))
