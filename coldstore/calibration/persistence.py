"""JSON lists and objects layered over the string key/value store."""

from __future__ import annotations

import json
from typing import Any

from coldstore.store.store import Store


def load_list(store: Store, key: str) -> list[Any]:
    """Read a JSON array, defaulting to an empty list when absent/corrupt."""

    raw, present = store.get(key)
    if not present:
        return []
    try:
        value = json.loads(raw)
    except ValueError:
        return []
    return value if isinstance(value, list) else []


def save_list(store: Store, key: str, items: list[Any]) -> None:
    store.put(key, json.dumps(items, ensure_ascii=False))


def load_dict(store: Store, key: str) -> dict[str, Any]:
    """Read a JSON object, defaulting to an empty dict when absent/corrupt."""

    raw, present = store.get(key)
    if not present:
        return {}
    try:
        value = json.loads(raw)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def save_dict(store: Store, key: str, value: dict[str, Any]) -> None:
    store.put(key, json.dumps(value, ensure_ascii=False))


def now() -> str:
    """UTC timestamp used for every calibration record."""

    import time

    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
