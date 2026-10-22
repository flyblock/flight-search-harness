"""Single Price Freshness Policy shared by Product and Harness projections."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


DEFAULT_PRICE_TTL_SECONDS = 900


def offset_timestamp(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise ValueError(f"{field} must be an ISO datetime") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def offer_price_deadline(
    observed_at: str,
    expires_at: str | None = None,
    *,
    ttl_seconds: int = DEFAULT_PRICE_TTL_SECONDS,
) -> datetime:
    observed = offset_timestamp(observed_at, "observed_at")
    ttl_deadline = observed + timedelta(seconds=ttl_seconds)
    if expires_at is None:
        return ttl_deadline
    provider_deadline = offset_timestamp(expires_at, "expires_at")
    return min(provider_deadline, ttl_deadline)
