#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Justin Douglas
# SPDX-License-Identifier: MIT
"""
Build the social preview banner for xrpl_agent_treasury_mgmt.

WHY THIS IS A SCRIPT AND NOT A ONE-OFF
======================================

The previous banner was drawn once, in a session whose drawing code was not
kept. That made three problems:

1. The wording could not be corrected without pixel-patching the JPEG, and
   every patch left a visible rectangular smudge in the description area.
2. The banner was 1280x426. GitHub's social preview slot is 1200x630, and
   the sibling repositories (xrpl_agent_id, xrpl_agent_log) already use
   1200x630.
3. Nobody could tell what the banner was supposed to say, so a stale claim
   about the project's maturity survived in the image long after it had been
   removed from the README.

So the banner is generated from source. Change the text below, run the
script, and the image is rebuilt cleanly.

THE DESIGN
==========

It follows the same system as the sibling banners:
- 1200x630, dark navy-black background (#010b17).
- Centred masthead: project name in bold white, a small uppercase
  descriptor beneath it.
- A two-line headline, first line white, second line in the cyan accent.
- A two-column feature grid with hollow cyan bullets, cyan titles and
  muted grey descriptions.
- A cyan rule and a black footer strip carrying the repository URL.

Run:
    python3 scripts/build_social_preview.py
"""

from __future__ import annotations

import pathlib

from PIL import Image, ImageDraw, ImageFont

# --------------------------------------------------------------------------
# Palette - matched to xrpl_agent_id / xrpl_agent_log
# --------------------------------------------------------------------------
BG = (1, 11, 23)
FOOTER_BG = (0, 0, 0)
WHITE = (255, 255, 255)
CYAN = (34, 178, 205)
MUTED = (154, 159, 165)

W, H = 1200, 630

# --------------------------------------------------------------------------
# Content
# --------------------------------------------------------------------------
PROJECT = "xrpl_agent_treasury_mgmt"
DESCRIPTOR = "TREASURY x XRP LEDGER x AGENT-MANAGED"

HEADLINE_1 = "Pay What the ERP Says Is Due."
HEADLINE_2 = "Nothing Moves Without Approval."

FEATURES = [
    (
        "Reconciles",
        "ERP obligations matched against on-chain payments, so nothing is funded twice.",
    ),
    (
        "Policy Before Signing",
        "Three tiers - automatic, one sign-off, two sign-offs. A hard limit is a refusal.",
    ),
    (
        "The Service Holds No Key",
        "A separate signer signs what it is given. The signature is checked against the request.",
    ),
    (
        "The Ledger Enforces the Perimeter",
        "Every payment carries a PermissionedDomain. rippled refuses anyone outside it.",
    ),
]

REPO_URL = "github.com/DuNorth595/xrpl_agent_treasury_mgmt"

HERE = pathlib.Path(__file__).resolve().parent.parent
OUT = HERE / "docs" / "social-preview.jpg"


# --------------------------------------------------------------------------
# Fonts
# --------------------------------------------------------------------------
FONT_DIR = pathlib.Path("/System/Library/Fonts/Supplemental")
FONT_REGULAR = str(FONT_DIR / "Arial.ttf")
FONT_BOLD = str(FONT_DIR / "Arial Bold.ttf")

if not pathlib.Path(FONT_REGULAR).exists():  # pragma: no cover - fallback
    FONT_REGULAR = "/System/Library/Fonts/Helvetica.ttc"
if not pathlib.Path(FONT_BOLD).exists():  # pragma: no cover - fallback
    FONT_BOLD = "/System/Library/Fonts/Helvetica.ttc"


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(FONT_BOLD if bold else FONT_REGULAR, size)


def centred(draw: ImageDraw.ImageDraw, y: int, text: str, f, fill) -> None:
    """Draw a line of text centred on the canvas."""
    w = draw.textlength(text, font=f)
    draw.text(((W - w) / 2, y), text, font=f, fill=fill)


def main() -> int:
    im = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(im)

    # ---- masthead ------------------------------------------------------
    centred(d, 42, PROJECT, font(46, bold=True), WHITE)
    centred(d, 106, DESCRIPTOR, font(15), MUTED)

    # ---- headline ------------------------------------------------------
    centred(d, 168, HEADLINE_1, font(44, bold=True), WHITE)
    centred(d, 224, HEADLINE_2, font(44, bold=True), CYAN)

    # ---- feature grid --------------------------------------------------
    col_x = [80, 640]
    row_y = [330, 452]
    title_font = font(21, bold=True)
    body_font = font(16)

    for (title, body), x, y in zip(FEATURES, [col_x[0], col_x[1], col_x[0], col_x[1]],
                                  [row_y[0], row_y[0], row_y[1], row_y[1]]):
        # hollow cyan bullet
        r = 7
        d.ellipse((x, y + 4, x + 2 * r, y + 4 + 2 * r), outline=CYAN, width=2)
        d.text((x + 26, y), title, font=title_font, fill=CYAN)
        # wrap the description
        words = body.split()
        lines = []
        cur = ""
        for word in words:
            trial = f"{cur} {word}".strip()
            if d.textlength(trial, font=body_font) > 430:
                lines.append(cur)
                cur = word
            else:
                cur = trial
        if cur:
            lines.append(cur)
        for i, line in enumerate(lines):
            d.text((x + 26, y + 34 + i * 24), line, font=body_font, fill=MUTED)

    # ---- footer --------------------------------------------------------
    rule_y = H - 74
    d.rectangle((0, rule_y, W, rule_y + 2), fill=CYAN)
    d.rectangle((0, rule_y + 2, W, H), fill=FOOTER_BG)

    url_font = font(20, bold=True)
    d.text((80, rule_y + 26), "github.com/DuNorth595/", font=url_font, fill=WHITE)
    offset = d.textlength("github.com/DuNorth595/", font=url_font)
    d.text((80 + offset, rule_y + 26), "xrpl_agent_treasury_mgmt", font=url_font, fill=CYAN)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    im.save(OUT, "JPEG", quality=95)
    print(f"wrote {OUT} ({W}x{H})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
