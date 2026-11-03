"""
Exchange-rate providers: live FX for the presentation layer.

WHY THIS EXISTS
===============

The treasury's figures are presented in a unit of account (USD), and a
disbursement may be denominated in CAD, MXN or RLUSD. Converting between
them needs a rate. A hardcoded rate is worse than no rate, because it looks
authoritative and is quietly wrong.

So rates come from a PROVIDER, are CACHED, carry the timestamp they were
fetched at, and have a defined staleness policy.

THE RULES
=========

1. **A rate lookup failure never changes what is transferred.** The
   disbursement still settles its native amount; only the displayed
   equivalent is missing. A missing rate is a reportable condition.

2. **Every rate carries its as-of time.** The rate in force at decision
   time is stored with the decision, so a historical figure can always be
   reproduced even after the rate moves.

3. **A stale rate is flagged, not hidden.** The cache has a maximum age. Past
   it, the rate is still returned — a stale quote beats no quote — but it is
   marked stale so the UI can say so.

4. **Providers are pluggable.** The protocol is the contract; the providers
   are interchangeable. A deployment can point at its own pricing service.

5. **RLUSD is a USD stablecoin and is pegged 1:1** by policy, not by a rate
   feed. It is handled separately from the FX path, and the peg is
   configurable for the day the peg breaks.

PROVIDERS
=========

    Frankfurter  ECB reference rates. No API key. Business-day only —
                  on a weekend it returns the last business day's rates,
                  which is correct but not intraday.
    ExchangeRate  exchangerate-api.com's free tier. No key. Daily.

Both are free, keyless, and adequate for a treasury presenting indicative
conversions. Neither is a source of executable pricing — a production
settlement path would use the venue's own quote.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Protocol

from .currency import PEGGED_TO_UNIT_OF_ACCOUNT, Rate


class RateError(Exception):
    """Raised when rates cannot be obtained. Never fatal to a payment."""


class RateProvider(Protocol):
    """Something that supplies exchange rates.

    `units_per_usd` is how many units of the quote currency one US dollar
    buys, matching the `Rate` dataclass.
    """

    name: str

    def fetch(self, tickers: List[str]) -> Dict[str, Rate]:
        ...


@dataclass
class CachedRates:
    """The cached rate table with its provenance and freshness."""

    rates: Dict[str, Rate] = field(default_factory=dict)
    fetched_at: str = ""
    fetched_at_epoch: float = 0.0
    provider: str = ""

    def age_seconds(self) -> float:
        if not self.fetched_at_epoch:
            return float("inf")
        return max(0.0, time.time() - self.fetched_at_epoch)

    def is_stale(self, max_age_seconds: float) -> bool:
        return self.age_seconds() > max_age_seconds

    def to_dict(self) -> Dict:
        return {
            "provider": self.provider,
            "fetched_at": self.fetched_at,
            "age_seconds": round(self.age_seconds(), 1),
            "rates": {k: v.to_dict() for k, v in self.rates.items()},
        }


class FrankfurterProvider:
    """ECB reference rates via frankfurter.app. No API key.

    Returns business-day reference rates. On a weekend or holiday the last
    published business day's rates are returned, with that day's date.
    """

    name = "frankfurter"

    def __init__(self, base: str = "USD", timeout: float = 15.0):
        self.base = base
        self.timeout = timeout

    def fetch(self, tickers: List[str]) -> Dict[str, Rate]:
        wanted = ",".join(t.upper() for t in tickers if t.upper() != self.base)
        if not wanted:
            return {}
        url = f"https://api.frankfurter.app/latest?from={self.base}&to={wanted}"
        with urllib.request.urlopen(url, timeout=self.timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
        as_of = body.get("date", "")
        out: Dict[str, Rate] = {}
        for ticker, value in (body.get("rates") or {}).items():
            out[ticker.upper()] = Rate(
                quote=ticker.upper(),
                units_per_usd=float(value),
                at=as_of,
                source=f"frankfurter.app (ECB reference), {as_of}",
            )
        return out


class ExchangeRateApiProvider:
    """exchangerate-api.com free tier. No API key, daily rates."""

    name = "exchangerate-api"

    def __init__(self, base: str = "USD", timeout: float = 15.0):
        self.base = base
        self.timeout = timeout

    def fetch(self, tickers: List[str]) -> Dict[str, Rate]:
        url = f"https://open.er-api.com/v6/latest/{self.base}"
        with urllib.request.urlopen(url, timeout=self.timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
        if body.get("result") != "success":
            raise RateError(f"provider returned {body.get('result')}")
        as_of = body.get("time_last_update_utc", "")
        rates = body.get("rates") or {}
        out: Dict[str, Rate] = {}
        for ticker in tickers:
            key = ticker.upper()
            if key == self.base or key not in rates:
                continue
            out[key] = Rate(
                quote=key,
                units_per_usd=float(rates[key]),
                at=as_of,
                source=f"exchangerate-api.com, {as_of}",
            )
        return out


class StaticRateProvider:
    """Fixed rates. The last resort, and explicitly labelled as such."""

    name = "static"

    def __init__(self, rates: Dict[str, float], at: str = "static"):
        self._rates = rates
        self._at = at

    def fetch(self, tickers: List[str]) -> Dict[str, Rate]:
        out: Dict[str, Rate] = {}
        for ticker in tickers:
            key = ticker.upper()
            if key in self._rates:
                out[key] = Rate(
                    quote=key,
                    units_per_usd=float(self._rates[key]),
                    at=self._at,
                    source="static fallback rate - NOT a market quote",
                )
        return out


@dataclass
class RateService:
    """Fetches, caches and serves rates with a provider fallback chain.

    The chain is tried in order until one returns a usable table. If every
    provider fails, the CACHED rates are served (stale) rather than nothing,
    because a stale indicative rate is more useful to an operator than a
    blank screen — and it is labelled stale.
    """

    providers: List[RateProvider] = field(default_factory=list)
    max_age_seconds: float = 3600.0
    cache: CachedRates = field(default_factory=CachedRates)
    last_error: Optional[str] = None
    # The currency the rates are quoted against.
    base: str = "USD"

    def refresh(self, tickers: List[str]) -> CachedRates:
        """Try each provider in turn. On total failure, serve the cache."""
        if not self.providers:
            raise RateError("no rate providers configured")
        errors: List[str] = []

        # Pegged currencies (USD stablecoins) are handled by policy, not by
        # a rate feed. They are added to whatever the provider returns, so
        # the treasury's settlement currency always has a rate.
        wanted_fx = [
            t for t in tickers
            if t.upper() not in PEGGED_TO_UNIT_OF_ACCOUNT
        ]

        for provider in self.providers:
            try:
                rates = provider.fetch(wanted_fx)
            except Exception as exc:
                errors.append(f"{getattr(provider, 'name', '?')}: {exc!r}")
                continue
            if not rates and wanted_fx:
                errors.append(f"{getattr(provider, 'name', '?')}: empty result")
                continue
            for ticker, value in PEGGED_TO_UNIT_OF_ACCOUNT.items():
                if ticker in [t.upper() for t in tickers]:
                    rates[ticker] = Rate(
                        quote=ticker,
                        units_per_usd=value,
                        at=datetime.now(timezone.utc).isoformat(),
                        source=f"pegged to {self.base} by policy",
                    )
            self.cache = CachedRates(
                rates=rates,
                fetched_at=datetime.now(timezone.utc).isoformat(),
                fetched_at_epoch=time.time(),
                provider=getattr(provider, "name", "?"),
            )
            self.last_error = None
            return self.cache
        self.last_error = "; ".join(errors)
        # Every provider failed. Serve the cache if there is one, clearly
        # stale. Do not raise: a rate lookup must never block a payment.
        return self.cache

    def get(
        self, tickers: List[str], force_refresh: bool = False
    ) -> CachedRates:
        """Serve rates, refreshing if the cache is empty or stale."""
        if force_refresh or not self.cache.rates or self.cache.is_stale(
            self.max_age_seconds
        ):
            self.refresh(tickers)
        return self.cache

    def rate_for(self, ticker: str) -> Optional[Rate]:
        key = ticker.upper()
        if key in self.cache.rates:
            return self.cache.rates[key]
        return None

    def to_dict(self) -> Dict:
        return {
            "cache": self.cache.to_dict(),
            "max_age_seconds": self.max_age_seconds,
            "stale": self.cache.is_stale(self.max_age_seconds),
            "last_error": self.last_error,
            "providers": [
                getattr(p, "name", "?") for p in self.providers
            ],
        }


def default_rate_service(
    tickers: Optional[List[str]] = None,
    fallback_rates: Optional[Dict[str, float]] = None,
) -> RateService:
    """The rate service for the local development build.

    Two live keyless providers, with a static fallback so the presentation
    layer degrades rather than blanks out.
    """
    fallback = fallback_rates or {
        "RLUSD": 1.0,
        "CAD": 1.37,
        "MXN": 17.05,
        "EUR": 0.92,
        "GBP": 0.79,
    }
    service = RateService(
        providers=[
            FrankfurterProvider(),
            ExchangeRateApiProvider(),
            StaticRateProvider(fallback, at="static fallback"),
        ],
        max_age_seconds=3600.0,
    )
    if tickers:
        service.refresh(tickers)
    return service
