"""Run-scoped currency normalization from ECB reference rates."""

from __future__ import annotations

from datetime import datetime, timezone
from functools import lru_cache
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from xml.etree import ElementTree


ECB_DAILY_RATES_URL = (
    "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
)


def _parse_ecb_rates(payload: bytes) -> tuple[str | None, dict[str, float]]:
    root = ElementTree.fromstring(payload)
    reference_date = None
    euro_rates = {"EUR": 1.0}
    for element in root.iter():
        local_name = element.tag.rsplit("}", 1)[-1]
        if local_name != "Cube":
            continue
        if element.get("time"):
            reference_date = element.get("time")
        currency = element.get("currency")
        rate = element.get("rate")
        if currency and rate:
            euro_rates[currency.upper()] = float(rate)
    return reference_date, euro_rates


@lru_cache(maxsize=16)
def system_fx_context(base_currency: str) -> dict[str, Any]:
    """Return rates into Base Currency, falling back to Base Currency only."""

    base = base_currency.upper()
    fallback = {"rates": {base: 1.0}, "snapshot": None}
    if base == "XTS":
        return fallback
    request = Request(
        ECB_DAILY_RATES_URL,
        headers={"User-Agent": "flight-search-harness/1.0"},
    )
    try:
        with urlopen(request, timeout=5) as response:
            payload = response.read()
        reference_date, euro_rates = _parse_ecb_rates(payload)
    except (HTTPError, URLError, TimeoutError, OSError, ElementTree.ParseError, ValueError):
        return fallback
    base_per_euro = euro_rates.get(base)
    if base_per_euro is None:
        return fallback
    rates = {
        currency: base_per_euro / currency_per_euro
        for currency, currency_per_euro in euro_rates.items()
    }
    rates[base] = 1.0
    observed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return {
        "rates": rates,
        "snapshot": {
            "base_currency": base,
            "rates": rates,
            "source_url": ECB_DAILY_RATES_URL,
            "provider": "European Central Bank",
            "observed_at": observed_at,
            "reference_date": reference_date,
        },
    }
