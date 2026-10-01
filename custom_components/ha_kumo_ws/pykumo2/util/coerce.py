"""Lenient value coercion for wire payloads."""

import math
from collections.abc import Mapping
from typing import Any


def as_float(value: object) -> float | None:
    """Finite number as float, else None (bools rejected)."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def as_int(value: object) -> int | None:
    """Integral number as int, else None."""
    number = as_float(value)
    return int(number) if number is not None and number.is_integer() else None


def as_bool(value: object) -> bool | None:
    """Bool or 0/1, else None."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    return None


def as_str(value: object) -> str | None:
    """Non-empty string, else None."""
    return value if isinstance(value, str) and value else None


def dig(obj: object, *path: str) -> Any:
    """Walk nested mappings; None when any step is missing."""
    for key in path:
        if not isinstance(obj, Mapping):
            return None
        obj = obj.get(key)
    return obj
