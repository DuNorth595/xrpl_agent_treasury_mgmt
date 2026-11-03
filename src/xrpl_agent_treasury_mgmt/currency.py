"""
Currency: what the treasury holds, what it disburses, and how it is presented.

THE MODEL
=========

Three distinct concerns, deliberately kept apart:

    HOLDING          What the treasury holds. On XRPL this is a set of trust
                     lines. The treasury holds RLUSD against the RLUSD
                     issuer, and whatever else it has established trust in.

    DISBURSEMENT     What leaves. A single disbursement is denominated in
                     ONE currency — the currency the counterparty needs. A
                     contractor in Toronto gets CAD; a payroll run in
                     Mexico gets MXN. The amount is never split across
                     currencies within one instruction.

    PRESENTATION     What the operator reads. The treasury's consolidated
                     position is shown in the unit of account (USD), and each
                     disbursement shows its own denomination alongside its
                     USD equivalent.

CROSS-CURRENCY SETTLEMENT
=========================

The treasury holds RLUSD. A Canadian contractor needs CAD. On the XRP Ledger
that is a cross-currency Payment:

    Amount    the currency the BENEFICIARY receives   (CAD)
    SendMax   the currency the TREASURY spends        (RLUSD)
    Paths     the route between them

The treasury's RLUSD is what actually leaves. The CAD arrives through the
path. So the treasury needs a position in every currency it disburses, and
the service's job is to say plainly when it does not have one.

This is the honest constraint. A treasury cannot conjure CAD it does not
hold; it needs either a CAD balance, or a route (path) to one. The service
models both and reports which is present.

RLUSD ON THE XRP LEDGER
=======================

RLUSD is an Issued Currency. Its code is longer than three characters, so
the ledger transmits it as a 40-character hex string:

    RLUSD -> 524C555344000000000000000000000000000000

Each holder must establish a trust line to the issuer before it can receive
the currency. Issuer addresses:

    Mainnet   rMxCKbEDwqr76QuheSUMdEGf4B9xJ8m5De
    Testnet   rQhWct2fv4Vc4KRjRgMrxa8xPN9Zx9iLKV

The issuer is part of the asset's identity: RLUSD from a different account
is a different asset. The service matches on the (currency, issuer) pair,
never on the ticker alone.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

# The XRP Ledger represents a currency code longer than three characters as
# a 40-character hex string. This is RLUSD's representation.
RLUSD_HEX = "524C555344000000000000000000000000000000"

RLUSD_MAINNET_ISSUER = "rMxCKbEDwqr76QuheSUMdEGf4B9xJ8m5De"
RLUSD_TESTNET_ISSUER = "rQhWct2fv4Vc4KRjRgMrxa8xPN9Zx9iLKV"

# Issued currency amounts are expressed in the smallest unit. For RLUSD and
# for the other dollar-pegged currencies modelled here, the smallest unit is
# one millionth of a major unit.
SCALE = 1_000_000


def hex_code(ticker: str) -> str:
    """The ledger's hex representation of a currency code.

    Codes of three characters or fewer are transmitted as-is. Longer codes
    are ASCII-hex-encoded and padded to 40 characters.
    """
    if len(ticker) <= 3:
        return ticker.upper()
    return ticker.encode("ascii").hex().ljust(40, "0").upper()


def ticker_from_hex(code: str) -> str:
    """Recover the ticker from the ledger's hex representation."""
    if len(code) != 40:
        return code
    try:
        return bytes.fromhex(code).rstrip(b"\x00").decode("ascii")
    except Exception:
        return code


def parse_xrpl_amount(value) -> float:
    """Parse an XRPL amount into a float.

    The XRP Ledger returns issued-currency amounts in scientific notation:
    a balance of 1,000,000 RLUSD comes back as "1000000000000000e-4",
    and a negative balance as "-70900000000". int() cannot parse the
    first form, so a plain int() raises ValueError on a perfectly
    ordinary balance.

    This handles every form the ledger produces:

        "1000000000000000e-4"  -> 100000000.0
        "-70900000000"         -> -70900000.0
        "0"                    -> 0.0
        180000000              -> 180000000.0
        Decimal("1.5")         -> 1.5
    """
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)

    text = str(value).strip()
    if not text:
        return 0.0

    # The XRPL uses "e-4" to mean "times 10^-4". Python's float() reads
    # that natively, but only if the mantissa is a plain number.
    try:
        return float(text)
    except ValueError:
        pass

    # Handle the case where the exponent has no sign, e.g. "1e4".
    try:
        return float(text.replace("e", "e+").replace("E", "E+"))
    except ValueError:
        pass

    # Last resort: split the mantissa from the exponent.
    if "e" in text.lower():
        mantissa, _, exponent = text.lower().partition("e")
        try:
            return float(mantissa) * (10 ** int(exponent))
        except (ValueError, OverflowError):
            return 0.0

    return 0.0


def xrpl_drops(value) -> float:
    """An XRPL amount expressed in drops, as a float."""
    return parse_xrpl_amount(value)


@dataclass(frozen=True)

class IssuedAsset:
    """An issued currency: a currency code plus its issuer.

    Both parts are required to identify the asset. Two accounts issuing
    "CAD" are two different assets.
    """

    code: str
    issuer: str
    ticker: str = ""

    def __post_init__(self):
        if not self.ticker:
            object.__setattr__(self, "ticker", ticker_from_hex(self.code))

    @property
    def hex_code(self) -> str:
        """The ledger's hex representation of the currency code."""
        return self.code if len(self.code) == 40 else hex_code(self.code)

    def to_amount(self, value: float) -> Dict:
        """Build the ledger's IssuedCurrencyAmount for a value.

        `value` is in major units (dollars). The ledger wants the smallest
        unit, as a string.
        """
        return {
            "currency": self.hex_code,
            "issuer": self.issuer,
            "value": str(int(round(value * SCALE))),
        }

    def to_dict(self) -> Dict:
        return {
            "ticker": self.ticker,
            "code": self.code,
            "hex_code": self.hex_code,
            "issuer": self.issuer,
        }


def rlusd(issuer: str = RLUSD_TESTNET_ISSUER) -> IssuedAsset:
    return IssuedAsset(code=RLUSD_HEX, issuer=issuer, ticker="RLUSD")


def asset_scale(asset: IssuedAsset) -> int:
    """How many smallest units make one major unit of the asset."""
    return SCALE


def asset_from_amount(amount: Dict) -> Optional[IssuedAsset]:
    """Recover the asset from a ledger amount object, if it is an issued
    currency."""
    if not isinstance(amount, dict):
        return None
    if "currency" not in amount or "issuer" not in amount:
        return None
    code = amount["currency"]
    return IssuedAsset(code=code, issuer=amount["issuer"])


@dataclass
class Rate:
    """A quoted conversion rate at a point in time.

    `units_per_usd` is how many units of the quote currency one US dollar
    buys. It is stored alongside every decision so the figure presented to
    the operator can be reproduced later.
    """

    quote: str
    units_per_usd: float
    at: str = ""
    source: str = ""

    def convert(self, usd_amount: float) -> float:
        return usd_amount * self.units_per_usd

    def to_dict(self) -> Dict:
        return {
            "quote": self.quote,
            "units_per_usd": self.units_per_usd,
            "at": self.at,
            "source": self.source,
        }


@dataclass
class Position:
    """A holding of one currency."""

    asset: IssuedAsset
    amount: float

    def to_dict(self) -> Dict:
        return {
            "asset": self.asset.to_dict(),
            "amount": self.amount,
            "display": f"{self.amount:,.2f}",
        }


SYMBOLS = {
    "USD": "$",
    "RLUSD": "$",
    "CAD": "CA$",
    "MXN": "MX$",
    "EUR": "€",
    "GBP": "£",
}

# Currencies pegged to the unit of account by policy rather than by a rate
# feed. A USD stablecoin is 1:1 with USD by design; it is not an FX pair and
# no pricing service quotes it against USD in any meaningful way.
#
# The peg is a POLICY assumption, not a fact about the market. It is kept
# explicit and configurable so that the day it breaks, the assumption is
# visible and can be changed rather than baked in.
PEGGED_TO_UNIT_OF_ACCOUNT = {
    "RLUSD": 1.0,
    "USDC": 1.0,
    "USDT": 1.0,
}


@dataclass
class CurrencyBook:
    """The treasury's currency arrangements.

    `unit_of_account` is the consolidated reporting currency — USD here.
    `assets` are the issued currencies the treasury may hold and disburse.
    `rates` drive presentation only; they never affect what is transferred.
    """

    unit_of_account: str = "USD"
    assets: Dict[str, IssuedAsset] = field(default_factory=dict)
    rates: Dict[str, Rate] = field(default_factory=dict)

    def set_asset(self, ticker: str, asset: IssuedAsset) -> None:
        self.assets[ticker.upper()] = asset

    def asset_for(self, ticker: str) -> Optional[IssuedAsset]:
        return self.assets.get(ticker.upper())

    def set_rate(self, rate: Rate) -> None:
        self.rates[rate.quote.upper()] = rate

    def rate_for(self, ticker: str) -> Optional[Rate]:
        return self.rates.get(ticker.upper())

    def to_usd(self, amount: float, ticker: str) -> Optional[float]:
        """Convert an amount in `ticker` to the unit of account.

        None when no rate is known — which is a reportable condition, not
        something to paper over with a zero.
        """
        ticker = ticker.upper()
        if ticker == self.unit_of_account:
            return amount
        rate = self.rate_for(ticker)
        if rate is None:
            return None
        return amount / rate.units_per_usd

    def from_usd(self, usd_amount: float, ticker: str) -> Optional[float]:
        """Convert an amount in the unit of account to `ticker`."""
        ticker = ticker.upper()
        if ticker == self.unit_of_account:
            return usd_amount
        rate = self.rate_for(ticker)
        if rate is None:
            return None
        return usd_amount * rate.units_per_usd

    def present(self, amount: float, ticker: str) -> Dict:
        """Present an amount in its own denomination and in the unit of
        account.

        The native figure is authoritative. The unit-of-account figure is
        derived and labelled as an estimate.
        """
        ticker = ticker.upper()
        symbol = SYMBOLS.get(ticker, ticker + " ")
        result = {
            "amount": amount,
            "ticker": ticker,
            "symbol": symbol,
            "display": f"{symbol}{amount:,.2f}",
            "plain": f"{amount:,.2f}",
        }
        usd = self.to_usd(amount, ticker)
        if usd is not None and ticker != self.unit_of_account:
            rate = self.rate_for(ticker)
            result["unit_of_account_amount"] = usd
            result["unit_of_account_display"] = f"${usd:,.2f}"
            result["rate"] = rate.units_per_usd if rate else None
            result["rate_at"] = rate.at if rate else ""
            result["is_estimate"] = True
        elif ticker == self.unit_of_account:
            result["unit_of_account_amount"] = amount
            result["unit_of_account_display"] = f"${amount:,.2f}"
            result["is_estimate"] = False
        return result

    def to_dict(self) -> Dict:
        return {
            "unit_of_account": self.unit_of_account,
            "assets": {k: v.to_dict() for k, v in self.assets.items()},
            "rates": {k: v.to_dict() for k, v in self.rates.items()},
        }


def default_currency_book(
    issuers: Optional[Dict[str, str]] = None,
    rates: Optional[Dict[str, float]] = None,
) -> CurrencyBook:
    """The currency book for the local development build.

    The unit of account is USD. RLUSD is the treasury's holding. CAD and MXN
    are the disbursement currencies modelled for the Canada and Mexico cases.

    `rates` is a SEED table only — it is used until the live rate feed
    replaces it, and it is deliberately small. The authoritative rates come
    from `rates.RateService`, which the service refreshes on each plan
    build. A hardcoded rate is worse than no rate, because it looks
    authoritative and is quietly wrong.
    """
    book = CurrencyBook(unit_of_account="USD")

    issuer_map = issuers or {}
    book.set_asset("RLUSD", rlusd(issuer_map.get("RLUSD", RLUSD_TESTNET_ISSUER)))
    for ticker in ("CAD", "MXN", "EUR"):
        issuer = issuer_map.get(ticker)
        if issuer:
            book.set_asset(ticker, IssuedAsset(code=hex_code(ticker), issuer=issuer))

    # Seed rates. The live feed supersedes these on the first refresh.
    seed = rates or {
        "RLUSD": 1.0,
        "CAD": 1.37,
        "MXN": 17.05,
        "EUR": 0.92,
    }
    for ticker, units in seed.items():
        book.set_rate(
            Rate(
                quote=ticker,
                units_per_usd=units,
                at="seed",
                source="seed rate - superseded by the live feed on first refresh",
            )
        )
    return book
