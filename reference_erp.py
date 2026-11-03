#!/usr/bin/env python3
"""
A reference ERP server — the thing a real plug-in talks to.

This exists so the integration path can be PROVEN end to end rather than
asserted. It is a deliberately small HTTP service that behaves like the
open-obligations endpoint of a real finance system:

    GET /obligations?status=open   ->  a JSON list of records
    GET /health                     ->  {"status": "ok"}

The records use field names that a real ERP would use — `invoice_number`,
`total_due`, `expected_payment_date`, `counterparty_account` — so the
`GenericRestErp` adapter is exercised against a NON-trivial field mapping
rather than a convenient one.

    python3 reference_erp.py --port 8096

It also serves a `GET /down` that returns 503, so the failure path is
testable: a treasury must not silently plan nothing when the ERP is down.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from xrpl_agent_treasury_mgmt.devnet_accounts import address_for as _address

# The accounts the treasury service uses on the devnet, resolved by
# role. The addresses are NOT hardcoded: the devnet accounts file holds
# the seeds for them, so publishing the addresses would publish half of
# what is needed to spend from them.
COST_CENTRE_A = _address("COST_CENTRE_A")
COST_CENTRE_B = _address("COST_CENTRE_B")

STATE = {"down": False}


def obligations() -> list:
    now = datetime.now(timezone.utc)
    return [
        {
            "invoice_number": "INV-2026-0417",
            "description": "Q4 electricity - facilities",
            "total_due": 180.00,
            "expected_payment_date": (now + timedelta(hours=30)).isoformat(),
            "counterparty_account": COST_CENTRE_A,
            "cost_centre": "Facilities & Utilities",
            "supplier_name": "Northwind Grid Co.",
            "currency_code": "RLUSD",
        },
        {
            "invoice_number": "INV-2026-0422",
            "description": "Site works - Toronto contractor",
            "total_due": 3750.00,
            "expected_payment_date": (now + timedelta(hours=36)).isoformat(),
            "counterparty_account": COST_CENTRE_A,
            "cost_centre": "Facilities & Utilities",
            "supplier_name": "Maple Ridge Construction",
            "currency_code": "CAD",
        },
        {
            "invoice_number": "INV-2026-0423",
            "description": "Payroll run - Mexico City plant",
            "total_due": 48000.00,
            "expected_payment_date": (now + timedelta(hours=26)).isoformat(),
            "counterparty_account": COST_CENTRE_B,
            "cost_centre": "Payroll",
            "supplier_name": "Internal payroll run - MX",
            "currency_code": "MXN",
        },
    ]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"  [reference-erp] {fmt % args}", flush=True)

    def _send(self, code: int, payload: dict):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/health":
            return self._send(200, {"status": "ok"})
        if path == "/down":
            STATE["down"] = not STATE["down"]
            return self._send(200, {"down": STATE["down"]})
        if path == "/obligations":
            if STATE["down"]:
                return self._send(503, {"error": "ERP temporarily unavailable"})
            return self._send(200, {"records": obligations()})
        return self._send(404, {"error": "not found"})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8096)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    server = HTTPServer((args.host, args.port), Handler)
    print(f"reference ERP on http://{args.host}:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
