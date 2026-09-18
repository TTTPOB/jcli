"""JSON value semantics used by shared notebook metadata."""

from __future__ import annotations

import math


def json_compatible_copy(value):
    """Return plain JSON-compatible containers or reject unsupported metadata."""
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("metadata mapping keys must be strings")
            result[key] = json_compatible_copy(item)
        return result
    if isinstance(value, list):
        return [json_compatible_copy(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("metadata numbers must be finite")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"metadata value is not JSON-compatible: {type(value).__name__}")


def json_equal(left, right) -> bool:
    """Compare JSON-like values without Python's bool/number coercion."""
    if left is right:
        return True
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left == right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return left == right
    if left is None or right is None:
        return left is None and right is None
    if isinstance(left, str) or isinstance(right, str):
        return isinstance(left, str) and isinstance(right, str) and left == right
    if isinstance(left, list) or isinstance(right, list):
        return (
            isinstance(left, list)
            and isinstance(right, list)
            and len(left) == len(right)
            and all(json_equal(a, b) for a, b in zip(left, right))
        )
    if isinstance(left, dict) or isinstance(right, dict):
        return (
            isinstance(left, dict)
            and isinstance(right, dict)
            and left.keys() == right.keys()
            and all(json_equal(left[key], right[key]) for key in left)
        )
    return type(left) is type(right) and left == right
