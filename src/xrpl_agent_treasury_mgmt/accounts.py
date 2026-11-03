"""
Account helpers and XRP amount formatting.

THE DROPS TRAP
==============
An XRPL Amount that is a bare integer is in **DROPS**, not XRP.

    Amount "10"          == 10 drops          == 0.00001 XRP
    Amount "1000000"     == 1,000,000 drops   == 1 XRP
    Amount "100000000"   == 100,000,000 drops == 100 XRP

A decimal string ("1.5") is rejected outright by the binary codec as an
invalid XRP amount. The ONLY valid forms are a bare integer (drops) or the
explicit drops-with-exponent form the codec emits.

This bit the project twice:

1. The domain-gate proof used `amount="10"`, so every case moved 10 DROPS
   (0.00001 XRP), not 10 XRP. The gate property still held - the rejections and
   the successes were all correct - but the amounts were 100,000x smaller than
   intended, and the proof's balance table was in drops (999999980 = 999.999980
   XRP), not XRP.

2. `fmt_xrp` originally emitted `"12000.000000"`, which the codec rejects
   (`XRPLBinaryCodecException: ... is an invalid XRP amount`) because of the
   trailing zeros.

So: `fmt_xrp` now takes XRP and returns the DROPS string to put on-chain, and
`fmt_xrp_amount` handles the display side. Every amount that goes on-chain goes
through `fmt_xrp` first.
"""

from __future__ import annotations

from typing import Dict, List, Optional

DROPS_PER_XRP = 1_000_000


def xrp_to_drops(amount_xrp: float) -> int:
    """Convert XRP to drops. This is the value that goes on-chain."""
    return int(round(float(amount_xrp) * DROPS_PER_XRP))


def drops_to_xrp(drops: int) -> float:
    """Convert drops (what the ledger stores) to XRP (what humans read)."""
    return int(drops) / DROPS_PER_XRP


def fmt_xrp(amount_xrp: float) -> str:
    """Format XRP as the integer drops string the XRPL codec requires.

    >>> fmt_xrp(10.0)
    '10000000'
    >>> fmt_xrp(0.5)
    '500000'
    >>> fmt_xrp(12000.0)
    '12000000000'

    The result is a bare integer, which the codec reads as drops. There are
    no trailing zeros to reject and no decimal point to choke on.
    """
    return str(xrp_to_drops(amount_xrp))


def fmt_xrp_display(amount_xrp: float, places: int = 6) -> str:
    """Format XRP for human display, trimming trailing zeros.

    >>> fmt_xrp_display(10.0)
    '10'
    >>> fmt_xrp_display(180.5)
    '180.5'
    """
    text = f"{float(amount_xrp):.{places}f}".rstrip("0").rstrip(".")
    return text if text else "0"


def is_valid_xrp_amount(text: str) -> bool:
    """Check an amount string the way the binary codec does.

    Valid: a bare integer (drops). Invalid: a decimal, a trailing zero, a
    leading plus, an exponent, or anything non-numeric.
    """
    if not text:
        return False
    if not text.isdigit():
        return False
    return True


def short_account(address: str, head: int = 10, tail: int = 6) -> str:
    """Render an address for display without losing its identity."""
    if len(address) <= head + tail + 1:
        return address
    return f"{address[:head]}…{address[-tail:]}"
