from __future__ import annotations

import html
import math
import re
from collections.abc import Iterable, Mapping
from typing import Any


_SECRET_ATTRIBUTE_NAMES = frozenset({
    "password", "passwd", "secret", "token", "api-key", "api_key",
    "authorization", "cookie", "set-cookie", "credential", "private-key",
})
_SECRET_INPUT_TYPES = frozenset({"password", "hidden"})
_PROMPT_BOUNDARY_PATTERN = re.compile(
    r"(?i)(ignore\s+(?:all\s+)?previous|system\s+message|developer\s+message|"
    r"assistant\s*:|tool\s*:|<\/?(?:system|assistant|tool|developer)[^>]*>)"
)
_WHITESPACE = re.compile(r"\s+")


def cap_text_length(text: str, max_length: int = 1000) -> str:
    """Bound untrusted page text without losing both ends of a long value."""

    value = str(text or "")
    if max_length <= 0:
        return ""
    if len(value) <= max_length:
        return value
    if max_length < 16:
        return value[:max_length]
    head = max_length * 2 // 3
    tail = max_length - head - 5
    return f"{value[:head]} ... {value[-tail:]}"


def normalize_page_text(value: Any, *, limit: int = 4000) -> str:
    text = html.unescape(str(value or "")).replace("\x00", "")
    text = _WHITESPACE.sub(" ", text).strip()
    return cap_text_length(text, limit)


def prompt_injection_signals(value: str) -> tuple[str, ...]:
    matches = {match.group(0).casefold() for match in _PROMPT_BOUNDARY_PATTERN.finditer(value or "")}
    return tuple(sorted(matches))


def sanitize_untrusted_text(value: Any, *, limit: int = 4000) -> tuple[str, tuple[str, ...]]:
    normalized = normalize_page_text(value, limit=limit)
    signals = prompt_injection_signals(normalized)
    if signals:
        normalized = _PROMPT_BOUNDARY_PATTERN.sub("[untrusted-page-instruction]", normalized)
    return normalized, signals


def is_secret_attribute(name: str, *, input_type: str = "") -> bool:
    normalized = str(name or "").strip().casefold()
    if normalized in _SECRET_ATTRIBUTE_NAMES:
        return True
    return str(input_type or "").strip().casefold() in _SECRET_INPUT_TYPES and normalized in {
        "value", "placeholder", "aria-valuetext", "valuetext"
    }


def redact_attributes(
    attributes: Mapping[str, Any] | None,
    *,
    sensitive_values: Iterable[str] = (),
) -> tuple[dict[str, str], tuple[str, ...]]:
    source = attributes or {}
    input_type = str(source.get("type") or "")
    values = tuple(sorted({str(item) for item in sensitive_values if str(item)}, key=len, reverse=True))
    result: dict[str, str] = {}
    redactions: list[str] = []
    for raw_name, raw_value in source.items():
        name = str(raw_name)
        if is_secret_attribute(name, input_type=input_type):
            result[name] = "[REDACTED]"
            redactions.append(name)
            continue
        value = str(raw_value or "")
        for sensitive in values:
            if sensitive in value:
                value = value.replace(sensitive, "[REDACTED]")
                redactions.append(name)
        clean, signals = sanitize_untrusted_text(value, limit=2000)
        if signals:
            redactions.append(f"prompt-boundary:{name}")
        result[name] = clean
    return result, tuple(sorted(set(redactions)))


def finite_number(value: Any, *, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def estimate_tokens(value: str | bytes | Mapping[str, Any] | Iterable[Any]) -> int:
    """Deterministic token estimate used for budget fencing, never billing."""

    if isinstance(value, bytes):
        size = len(value)
    elif isinstance(value, str):
        size = len(value.encode("utf-8"))
    else:
        import json

        try:
            size = len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"))
        except TypeError:
            size = len(str(value).encode("utf-8"))
    return max(1, math.ceil(size / 3.6))


def normalized_fact_key(value: Any) -> str:
    text = normalize_page_text(value, limit=500).casefold()
    return re.sub(r"[^\w\-.:/@]+", " ", text).strip()


def stable_text_fragments(value: Any, *, min_chars: int = 4, max_fragments: int = 128) -> tuple[str, ...]:
    text = normalize_page_text(value, limit=20000)
    fragments: list[str] = []
    seen: set[str] = set()
    for part in re.split(r"[\n\r.!?;|]+", text):
        fact = normalized_fact_key(part)
        if len(fact) < min_chars or fact in seen:
            continue
        seen.add(fact)
        fragments.append(fact)
        if len(fragments) >= max_fragments:
            break
    return tuple(fragments)
