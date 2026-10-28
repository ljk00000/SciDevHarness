"""Bounded, prompt-free accounting for model request attempts."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from typing import Any


_BYTES_PER_TOKEN_ESTIMATE = 3.25


def estimate_text_tokens(text: object) -> int:
    """Conservatively estimate tokens without retaining prompt text or needing a tokenizer."""
    if not isinstance(text, str) or not text:
        return 0
    return math.ceil(len(text.encode("utf-8", errors="replace")) / _BYTES_PER_TOKEN_ESTIMATE)


def estimate_request_context_tokens(messages: object, tools: object = None) -> int:
    """Estimate model input tokens from the serialized message and tool payload."""
    try:
        payload = json.dumps(
            {"messages": messages, "tools": tools or []},
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )
    except (TypeError, ValueError):
        payload = str(messages) + str(tools or "")
    return estimate_text_tokens(payload)


def format_token_count(value: object) -> str:
    """Format a token count compactly for the narrow Agent panel."""
    count = 0
    if isinstance(value, (int, float, str)) and not isinstance(value, bool):
        try:
            count = max(0, int(value))
        except (TypeError, ValueError, OverflowError):
            count = 0
    if count < 1_000:
        return str(count)
    divisor, suffix = (1_000_000, "M") if count >= 1_000_000 else (1_000, "k")
    formatted = f"{count / divisor:.1f}".rstrip("0").rstrip(".")
    return f"{formatted}{suffix}"


class ModelRequestUsage:
    """Persist aggregate payload sizes and retry counts without message contents."""

    MAX_RECENT_RECORDS = 100
    MAX_REQUEST_KEYS = 128
    COUNTERS = (
        "request_attempts",
        "retry_attempts",
        "failed_attempts",
        "proxy_fallback_attempts",
        "request_body_bytes",
        "response_body_bytes",
        "elapsed_ms",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "token_reports",
    )

    @staticmethod
    def estimate_context_tokens(messages: object, tools: object = None) -> int:
        return estimate_request_context_tokens(messages, tools)

    @classmethod
    def empty(cls) -> dict[str, int]:
        return {name: 0 for name in cls.COUNTERS}

    @staticmethod
    def _count(value: object) -> int:
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            return 0
        try:
            return max(0, min(int(value), 2**53 - 1))
        except (TypeError, ValueError, OverflowError):
            return 0

    @classmethod
    def snapshot(cls, session: dict[str, Any]) -> dict[str, int]:
        stored = session.get("model_usage")
        stored = stored if isinstance(stored, dict) else {}
        return {name: cls._count(stored.get(name, 0)) for name in cls.COUNTERS}

    @classmethod
    def record(
        cls,
        session: dict[str, Any],
        *,
        logical_request_key: str,
        kind: str,
        model: str,
        metrics: dict[str, Any],
    ) -> dict[str, Any]:
        keys = session.get("model_usage_request_keys")
        keys = dict(keys) if isinstance(keys, dict) else {}
        key = hashlib.sha256(logical_request_key.encode("utf-8", errors="replace")).hexdigest()[:24]
        attempt = cls._count(keys.get(key, 0)) + 1
        keys[key] = attempt
        while len(keys) > cls.MAX_REQUEST_KEYS:
            keys.pop(next(iter(keys)))
        session["model_usage_request_keys"] = keys

        usage = cls.snapshot(session)
        request_bytes = cls._count(metrics.get("request_body_bytes"))
        response_bytes = cls._count(metrics.get("response_body_bytes"))
        elapsed_ms = cls._count(metrics.get("elapsed_ms"))
        prompt_tokens = cls._count(metrics.get("prompt_tokens"))
        completion_tokens = cls._count(metrics.get("completion_tokens"))
        total_tokens = cls._count(metrics.get("total_tokens"))
        has_token_usage = any(
            name in metrics and metrics.get(name) is not None
            for name in ("prompt_tokens", "completion_tokens", "total_tokens")
        )
        if has_token_usage and not total_tokens:
            total_tokens = prompt_tokens + completion_tokens

        usage["request_attempts"] += 1
        usage["retry_attempts"] += int(attempt > 1)
        usage["failed_attempts"] += int(not bool(metrics.get("success")))
        transport_route = str(metrics.get("transport_route") or "direct")[:32]
        usage["proxy_fallback_attempts"] += int(transport_route == "proxy_fallback")
        usage["request_body_bytes"] += request_bytes
        usage["response_body_bytes"] += response_bytes
        usage["elapsed_ms"] += elapsed_ms
        usage["prompt_tokens"] += prompt_tokens
        usage["completion_tokens"] += completion_tokens
        usage["total_tokens"] += total_tokens
        usage["token_reports"] += int(has_token_usage)
        session["model_usage"] = usage

        record = {
            "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "request_key": key,
            "kind": str(kind)[:40],
            "attempt": attempt,
            "model": str(model or "unknown")[:120],
            "request_body_bytes": request_bytes,
            "response_body_bytes": response_bytes,
            "response_body_complete": bool(metrics.get("response_body_complete")),
            "elapsed_ms": elapsed_ms,
            "success": bool(metrics.get("success")),
            "status_code": cls._count(metrics.get("status_code")) or None,
            "transport_route": transport_route,
        }
        if has_token_usage:
            record.update({
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": total_tokens,
            })
        records = session.get("model_usage_records")
        records = list(records) if isinstance(records, list) else []
        records.append(record)
        session["model_usage_records"] = records[-cls.MAX_RECENT_RECORDS:]
        return {"record": record, "usage": dict(usage)}

    @staticmethod
    def format_bytes(value: object) -> str:
        count = max(0, ModelRequestUsage._count(value))
        if count < 1024:
            return f"{count} B"
        if count < 1024 * 1024:
            return f"{count / 1024:.1f} KB"
        return f"{count / (1024 * 1024):.2f} MB"
