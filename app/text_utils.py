from __future__ import annotations

from functools import lru_cache


@lru_cache(maxsize=1)
def _converter():
    try:
        from opencc import OpenCC
    except ImportError:
        return None
    return OpenCC("t2s")


def to_simplified(text: str) -> str:
    value = str(text or "")
    converter = _converter()
    if converter is None or not value:
        return value
    return converter.convert(value)


def simplify_segment(segment: dict) -> dict:
    item = dict(segment)
    item["text"] = to_simplified(str(item.get("text") or ""))
    return item


def simplify_segments(segments: list[dict]) -> list[dict]:
    return [simplify_segment(segment) for segment in segments]
