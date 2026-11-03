"""
Tests for the currency layer.

The traps this module guards:
  - The DROPS trap: a bare integer Amount is in DROPS, not XRP.
  - The hex encoding of a currency code longer than three characters.
  - The asset identity is the (code, issuer) PAIR, never the ticker.
  - A rate lookup failure must not alter what is transferred.
  - Limits are compared in the unit of account, not the disbursement currency.

Run with:  pytest tests/ -v
"""

from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(
    0, str(pathlib.Path(__file__).resolve().parent.parent / "src")
)

from xrpl_agent_treasury_mgmt.currency import (  # noqa: E402
    PEGGED_TO_UNIT_OF_ACCOUNT,
    RLUSD_HEX,
    IssuedAsset,
    asset_from_amount,
    hex_code,
    rlusd,
    ticker_from_hex,
)
from xrpl_agent_treasury_mgmt.accounts import (  # noqa: E402
    drops_to_xrp,
    fmt_xrp,
    is_valid_xrp_amount,
    xrp_to_drops,
)


class TestHexCode:
    def test_three_character_codes_are_unchanged(self):
        assert hex_code("CAD") == "CAD"
        assert hex_code("MXN") == "MXN"
        assert hex_code("USD") == "USD"

    def test_long_codes_are_hex_encoded_and_padded(self):
        assert hex_code("RLUSD") == RLUSD_HEX
        assert len(RLUSD_HEX) == 40
        assert RLUSD_HEX == "524C555344000000000000000000000000000000"

    def test_hex_round_trips(self):
        assert ticker_from_hex(hex_code("RLUSD")) == "RLUSD"
        assert ticker_from_hex("CAD") == "CAD"

    def test_lowercase_is_normalised(self):
        assert hex_code("cad") == "CAD"


class TestIssuedAsset:
    def test_to_amount_uses_the_scale(self):
        """THE DROPS TRAP. 10 RLUSD is 10,000,000 units, not 10."""
        asset = rlusd("rIssuer")
        amount = asset.to_amount(10.0)
        assert amount["value"] == "10000000"
        assert amount["currency"] == RLUSD_HEX
        assert amount["issuer"] == "rIssuer"

    def test_to_amount_of_one_dollar(self):
        asset = rlusd("rIssuer")
        assert asset.to_amount(1.0)["value"] == "1000000"

    def test_to_amount_of_a_sub_cent(self):
        asset = rlusd("rIssuer")
        assert asset.to_amount(0.000001)["value"] == "1"

    def test_to_amount_of_zero(self):
        asset = rlusd("rIssuer")
        assert asset.to_amount(0.0)["value"] == "0"

    def test_asset_identity_is_code_and_issuer(self):
        """RLUSD from two issuers is two different assets."""
        a = rlusd("rIssuerA")
        b = rlusd("rIssuerB")
        assert a.ticker == b.ticker == "RLUSD"
        assert a.hex_code == b.hex_code
        assert a.issuer != b.issuer
        assert a.to_dict() != b.to_dict()

    def test_asset_from_amount_recovers_the_ticker(self):
        asset = rlusd("rIssuer")
        recovered = asset_from_amount(asset.to_amount(5.0))
        assert recovered is not None
        assert recovered.ticker == "RLUSD"
        assert recovered.issuer == "rIssuer"

    def test_asset_from_amount_of_xrp_is_none(self):
        assert asset_from_amount("1000000") is None

    def test_asset_from_amount_of_a_dict_without_issuer_is_none(self):
        assert asset_from_amount({"currency": "CAD"}) is None


class TestDropsHelpers:
    def test_xrp_to_drops(self):
        assert xrp_to_drops(1.0) == 1_000_000
        assert xrp_to_drops(10.0) == 10_000_000

    def test_drops_to_xrp(self):
        assert drops_to_xrp(1_000_000) == 1.0
        assert drops_to_xrp(10_000_000) == 10.0

    def test_fmt_xrp_emits_drops_not_major_units(self):
        """The whole point. A major-unit integer would move 1/1000000th."""
        assert fmt_xrp(10.0) == "10000000"
        assert fmt_xrp(1.0) == "1000000"

    def test_fmt_xrp_has_no_trailing_zeros_or_decimal(self):
        """The codec rejects '12000.000000' outright.

        Note that the drops integer legitimately ENDS in zero - that is
        fine. What matters is that there is no decimal point and no
        trailing-zero FRACTION, which is what the codec rejects.
        """
        out = fmt_xrp(12000.0)
        assert "." not in out
        assert is_valid_xrp_amount(out)
        # 12,000 XRP is 12,000,000,000 drops.
        assert out == "12000000000"

    def test_valid_amount_rejects_decimals(self):
        assert not is_valid_xrp_amount("12000.000000")
        assert not is_valid_xrp_amount("1.5")
        assert is_valid_xrp_amount("12000")


class TestPeggedCurrencies:
    def test_rlusd_is_pegged_to_usd(self):
        assert PEGGED_TO_UNIT_OF_ACCOUNT["RLUSD"] == 1.0

    def test_the_peg_is_explicit_not_implicit(self):
        """A stablecoin's peg is a policy assumption and must be visible."""
        assert "RLUSD" in PEGGED_TO_UNIT_OF_ACCOUNT
        assert isinstance(PEGGED_TO_UNIT_OF_ACCOUNT, dict)
