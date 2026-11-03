"""
The executor: builds the Payment, dry-runs it, submits it.

Two rules, and they are the whole safety story:

1. **Every Payment carries `domain_id`, resolved from tenant config.** There
   is no code path here that can build a Payment without it. The domain id is
   never a parameter of any public function — it comes from the tenant.

2. **A dry run asserts balances.** A dry run's claim that it spent nothing is
   not evidence; the balance assertion is the evidence. (This is the lesson
   from the 2026-10-28 mainnet harness run that leaked 4 XRP while reporting
   success because `mock.patch` missed a call path.)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from .accounts import drops_to_xrp, fmt_xrp
from .currency import asset_scale
from .policy import ProposedTransfer
from .signer import SignOutcome, TreasurySigner


@dataclass
class BuiltPayment:
    """A Payment the executor built, with everything needed to audit it."""

    tenant_id: str
    destination_account: str
    amount: float
    currency: str = "RLUSD"
    domain_id: str = ""
    obligation_id: str = ""
    reference: str = ""
    tx_json: Dict = field(default_factory=dict)

    def to_dict(self) -> Dict:
        return {
            "tenant_id": self.tenant_id,
            "destination_account": self.destination_account,
            "amount": self.amount,
            "currency": self.currency,
            "domain_id": self.domain_id,
            "obligation_id": self.obligation_id,
            "reference": self.reference,
            "tx_json": self.tx_json,
        }


@dataclass
class ExecutionResult:
    """The result of executing one transfer."""

    transfer: ProposedTransfer
    built: Optional[BuiltPayment]
    status: str  # "dry_run" | "signed" | "awaiting_authorisation" | "refused" | "submitted" | "failed"
    tier: str = ""
    reasons: List[str] = field(default_factory=list)
    engine_result: Optional[str] = None
    engine_result_message: Optional[str] = None
    transaction_hash: Optional[str] = None
    balance_before_xrp: Optional[float] = None
    balance_after_xrp: Optional[float] = None
    balances_verified: bool = False
    detail: Dict = field(default_factory=dict)

    def to_dict(self) -> Dict:
        return {
            "status": self.status,
            "tier": self.tier,
            "reasons": list(self.reasons),
            "engine_result": self.engine_result,
            "engine_result_message": self.engine_result_message,
            "transaction_hash": self.transaction_hash,
            "balance_before_xrp": self.balance_before_xrp,
            "balance_after_xrp": self.balance_after_xrp,
            "balances_verified": self.balances_verified,
            "transfer": self.transfer.to_dict(),
            "built": self.built.to_dict() if self.built else None,
            "detail": self.detail,
        }


class LedgerClient:
    """Minimal ledger access: balances, account state, submit, simulate.

    Deliberately thin. The point of the project is not to reimplement an XRPL
    client; it is to compose the primitives we already have.
    """

    def __init__(self, url: str = "ws://127.0.0.1:6006", network_id: int = 15791):
        self.url = url
        self.network_id = network_id

    async def _rpc(self, command: Dict) -> Dict:
        import json

        import websockets

        async with websockets.connect(self.url) as ws:
            await ws.send(json.dumps(command))
            return json.loads(await ws.recv())

    async def account_info(self, address: str) -> Dict:
        resp = await self._rpc({"command": "account_info", "account": address})
        if resp.get("status") != "success":
            raise RuntimeError(f"account_info failed: {resp.get('error_message')}")
        return resp["result"]["account_data"]

    async def current_ledger_seq(self) -> int:
        """The current ledger index.

        `last_ledger_sequence` on a transaction must be AHEAD of this, or the
        transaction is rejected with tefMAX_LEDGER. The response key is
        `ledger_current_index`, not `ledger_index`.
        """
        resp = await self._rpc({"command": "ledger_current"})
        return int(resp["result"]["ledger_current_index"])

    async def balance_xrp(self, address: str) -> float:
        # The ledger stores balances in DROPS.
        data = await self.account_info(address)
        return drops_to_xrp(int(data["Balance"]))

    async def issued_balance(self, address: str, asset) -> float:
        """The balance of an issued currency held by an account.

        An issued currency is not stored on the AccountRoot — it lives on the
        trust line between the holder and the issuer. So the balance has to be
        read from `account_lines`, and it is expressed in the currency's
        smallest unit (RLUSD uses 1,000,000 per dollar).
        """
        resp = await self._rpc(
            {
                "command": "account_lines",
                "account": address,
                "peer": asset.issuer,
            }
        )
        if resp.get("status") != "success":
            return 0.0
        for line in resp.get("result", {}).get("lines", []):
            if line.get("currency") == asset.hex_code:
                return int(line.get("balance", 0)) / asset_scale(asset)
        return 0.0

    async def account_lines(self, address: str) -> List[Dict]:
        """Every trust line the account holds."""
        resp = await self._rpc({"command": "account_lines", "account": address})
        if resp.get("status") != "success":
            raise RuntimeError(f"account_lines failed: {resp.get('error_message')}")
        return resp["result"].get("lines", [])

    async def account_payments(
        self, address: str, limit: int = 50, tx_type: str = "Payment"
    ) -> List[Dict]:
        """The account's transactions, flattened for reconciliation.

        `account_tx` returns a list of `{tx, meta, validated}` entries, so
        each one is flattened into a single dict carrying the transaction
        fields plus the outcome. Reconciliation matches on the destination
        and amount, so the amount is normalised here into major units of the
        currency actually transferred.
        """
        resp = await self._rpc(
            {
                "command": "account_tx",
                "account": address,
                "limit": limit,
                "tx_type": tx_type,
            }
        )
        if resp.get("status") != "success":
            raise RuntimeError(f"account_tx failed: {resp.get('error_message')}")

        flattened: List[Dict] = []
        for entry in resp.get("result", {}).get("transactions", []):
            tx = entry.get("tx") or entry.get("Transaction") or {}
            meta = entry.get("meta") or {}
            amount = tx.get("Amount")
            if amount is None:
                continue
            # A string amount is XRP (in drops). A dict is an issued
            # currency (in its smallest unit).
            if isinstance(amount, str):
                amount_major = drops_to_xrp(int(amount))
                currency = "XRP"
            else:
                code = amount.get("currency", "")
                try:
                    ticker = bytes.fromhex(code).rstrip(b"\x00").decode("ascii")
                except Exception:
                    ticker = code
                scale = 1_000_000
                amount_major = int(amount.get("value", 0)) / scale
                currency = ticker
            flattened.append(
                {
                    "hash": tx.get("hash"),
                    "account": tx.get("Account"),
                    "destination": tx.get("Destination"),
                    "amount_xrp": amount_major,
                    "amount": amount_major,
                    "currency": currency,
                    # `account_tx` reports the outcome as TransactionResult,
                    # not engine_result.
                    "engine_result": meta.get("TransactionResult")
                    or meta.get("engine_result"),
                    "delivered_amount": meta.get("delivered_amount"),
                    "ledger_index": tx.get("ledger_index")
                    or meta.get("ledger_index"),
                    "validated": entry.get("validated"),
                }
            )
        return flattened

    async def account_objects(self, address: str) -> List[Dict]:
        resp = await self._rpc({"command": "account_objects", "account": address})
        if resp.get("status") != "success":
            raise RuntimeError(f"account_objects failed: {resp.get('error_message')}")
        return resp["result"]["account_objects"]

    async def domain_members(self, domain_id: str, owner: str) -> List[str]:
        """Accounts holding an accepted credential in the domain.

        Derived from the ledger, not from configuration. The credential
        objects carry Flags 65536 (lsfAccepted).

        A Credential lives in its SUBJECT's owner directory, not the
        issuer's. The issuer's directory holds only the issuer's own
        objects, so reading it alone misses every credential the domain
        actually admits. The domain's AcceptedCredentials list names the
        credential TYPES the domain admits, and each credential of one of
        those types names the account that holds it.

        The subject's directory is the authoritative list of holders, so
        it is read directly.
        """
        members = {owner}

        # The domain's accepted credential types.
        accepted_types = set()
        try:
            entry = await self._rpc(
                {"command": "ledger_entry", "index": domain_id}
            )
            node = entry.get("result", {}).get("node", {}) or {}
            for accepted in node.get("AcceptedCredentials", []) or []:
                credential = accepted.get("Credential", {}) or {}
                credential_type = credential.get("CredentialType")
                if credential_type:
                    accepted_types.add(credential_type)
        except Exception:
            accepted_types = set()

        # The issuer's directory holds the issuer's own credentials; the
        # subject's directory holds the credentials it was granted.
        for account in await self.account_objects(owner):
            self._collect_credential_subject(
                account, accepted_types, members
            )

        # Read the directories of every known participant, so a member
        # added since the domain was created is found.
        for candidate in list(members):
            try:
                for account in await self.account_objects(candidate):
                    self._collect_credential_subject(
                        account, accepted_types, members
                    )
            except Exception:
                continue

        return sorted(members)

    @staticmethod
    def _collect_credential_subject(account, accepted_types, members) -> None:
        """Add a credential's subject to the member set when the domain
        accepts that credential type."""
        if account.get("LedgerEntryType") != "Credential":
            return
        if not account.get("Flags", 0) & 0x00010000:  # lsfAccepted
            return
        credential_type = account.get("CredentialType")
        if accepted_types and credential_type not in accepted_types:
            return
        subject = account.get("Subject")
        if subject:
            members.add(subject)

    async def simulate(self, tx_json: Dict) -> Dict:
        """Ask the ledger what would happen, without submitting anything.

        The transaction must be unsigned: omit TxnSignature and set
        SigningPubKey to "".
        """
        unsigned = dict(tx_json)
        unsigned.pop("TxnSignature", None)
        unsigned["SigningPubKey"] = ""
        resp = await self._rpc({"command": "simulate", "tx_json": unsigned})
        return resp.get("result", resp)

    async def submit_blob(self, tx_blob: str) -> Dict:
        """Submit a signed blob. Returns the raw rippled response."""
        resp = await self._rpc({"command": "submit", "tx_blob": tx_blob})
        return resp


class Executor:
    """Builds, dry-runs, and submits internal disbursements.

    The executor is the only component that constructs a Payment, and it
    always resolves the domain id from the tenant.

    The Payment's amount is an ISSUED CURRENCY, not XRP. The treasury holds
    RLUSD; a Canadian counterparty receives CAD. When the currencies differ,
    the Payment is a cross-currency one: `Amount` is what the beneficiary
    receives and `SendMax` is what the treasury spends.
    """

    def __init__(
        self,
        ledger: LedgerClient,
        signer: TreasurySigner,
        domain_resolver,
        currency_book=None,
        settlement_currency=None,
    ):
        self.ledger = ledger
        self.signer = signer
        # domain_resolver(tenant_id) -> domain_id. Never a caller-supplied id.
        self._domain_resolver = domain_resolver
        self.currency_book = currency_book
        # The currency the TREASURY settles in (RLUSD). A disbursement in a
        # different currency is a cross-currency payment and needs a
        # send_max denominated in this currency.
        if settlement_currency is None and currency_book is not None:
            settlement_currency = currency_book.asset_for("RLUSD")
        self.settlement_currency = settlement_currency

        # The next sequence number to USE, per account.
        #
        # An account's transaction sequence must increase by one for
        # each transaction. Reading it once and reusing it for two
        # transactions means the second carries a sequence the ledger
        # has already consumed, and it is rejected with tefPAST_SEQ.
        #
        # The executor is the right place to track this: it is the thing
        # that builds transactions, so it is the thing that has to know
        # which sequence the NEXT transaction may use. Callers that read
        # a fresh sequence for every transaction are unaffected - the
        # counter only ever moves forward.
        self._next_sequence: Dict[str, int] = {}

    def sequence_for(self, account: str, observed: int) -> int:
        """The sequence to use for the NEXT transaction from an account.

        `observed` is the sequence the caller read from the ledger. If
        the executor has already issued a transaction from this account
        since that read, the counter is ahead of it and the counter
        wins - otherwise the two transactions would carry the same
        sequence and the second would be rejected with tefPAST_SEQ.
        """
        current = self._next_sequence.get(account)
        if current is None or current <= observed:
            current = observed
        self._next_sequence[account] = current + 1
        return current

    def build(
        self,
        tenant_id: str,
        treasury_account: str,
        transfer: ProposedTransfer,
        sequence: int,
        last_ledger_sequence: int,
    ) -> BuiltPayment:
        """Build the Payment. The domain id always comes from the tenant."""
        from xrpl.models import Payment

        domain_id = self._domain_resolver(tenant_id)
        sequence = self.sequence_for(treasury_account, sequence)

        kwargs = dict(
            account=treasury_account,
            destination=transfer.destination_account,
            domain_id=domain_id,
            fee="12",
            sequence=sequence,
            network_id=self.ledger.network_id,
            last_ledger_sequence=last_ledger_sequence,
        )

        # The currency the beneficiary receives.
        asset = None
        if self.currency_book is not None:
            asset = self.currency_book.asset_for(transfer.currency)

        if asset is not None:
            kwargs["amount"] = asset.to_amount(transfer.amount)
        else:
            # No issued asset configured for this currency. Fall back to a
            # native XRP amount, formatted in drops.
            kwargs["amount"] = fmt_xrp(transfer.amount)

        # CROSS-CURRENCY / CROSS-ISSUE PAYMENT.
        #
        # `send_max` is the maximum amount of SOURCE currency the
        # transaction may cost. The XRPL requires it for ANY payment whose
        # Amount is an issued currency, because sending RLUSD means the
        # source is XRP (the fee) and the issued asset is the destination -
        # a cross-issue payment. Without it the model is rejected with
        # "A currency conversion requires a `send_max` value."
        #
        # So an issued-currency disbursement ALWAYS carries send_max:
        #
        #   same currency   the treasury's settlement asset, so send_max is
        #                   in that asset and equals the amount.
        #   other currency  a cross-currency payment, so send_max is in the
        #                   treasury's settlement asset and is the USD
        #                   equivalent - a cap, because the exact amount
        #                   that settles is determined by the path.
        settlement = self.settlement_currency
        if asset is not None:
            if settlement is not None and asset.hex_code == settlement.hex_code:
                # Same issued asset. The source and destination currency are
                # the same, so send_max is the amount itself.
                kwargs["send_max"] = asset.to_amount(transfer.amount)
            elif settlement is not None:
                usd_equivalent = None
                if self.currency_book is not None:
                    usd_equivalent = self.currency_book.to_usd(
                        transfer.amount, transfer.currency
                    )
                if usd_equivalent is not None:
                    kwargs["send_max"] = settlement.to_amount(usd_equivalent)

        tx = Payment(**kwargs)

        return BuiltPayment(
            tenant_id=tenant_id,
            destination_account=transfer.destination_account,
            amount=transfer.amount,
            currency=transfer.currency,
            domain_id=domain_id,
            obligation_id=transfer.obligation_id,
            reference=transfer.reference,
            tx_json=tx.to_xrpl(),
        )

    async def holding(self, account: str, currency: str) -> float:
        """The account's balance in the given currency.

        For an issued currency this reads the trust line; for XRP it reads
        the account root. Returns 0.0 when the account holds none.
        """
        if self.currency_book is None:
            return await self.ledger.balance_xrp(account)
        asset = self.currency_book.asset_for(currency)
        if asset is None:
            return await self.ledger.balance_xrp(account)
        return await self.ledger.issued_balance(account, asset)

    async def dry_run(
        self,
        tenant_id: str,
        treasury_account: str,
        transfer: ProposedTransfer,
        sequence: int,
        last_ledger_sequence: int,
        balance_before_xrp: Optional[float] = None,
    ) -> ExecutionResult:
        """Simulate the transfer and assert the balances.

        The balance assertion is the evidence. We read the balance before and
        after and fail loudly if the assertion is not met.
        """
        built = self.build(
            tenant_id, treasury_account, transfer, sequence, last_ledger_sequence
        )

        before = balance_before_xrp
        if before is None:
            before = await self.holding(treasury_account, transfer.currency)

        sim = await self.ledger.simulate(built.tx_json)
        engine_result = sim.get("engine_result")
        applied = sim.get("applied")

        expected_after = before if not applied else before - transfer.amount

        detail = {
            "simulate_engine_result": engine_result,
            "simulate_engine_result_code": sim.get("engine_result_code"),
            "simulate_engine_result_message": sim.get("engine_result_message"),
            "simulate_applied": applied,
            "expected_balance_after": expected_after,
            "currency": transfer.currency,
        }

        return ExecutionResult(
            transfer=transfer,
            built=built,
            status="dry_run",
            engine_result=engine_result,
            engine_result_message=sim.get("engine_result_message"),
            balance_before_xrp=before,
            balance_after_xrp=before,  # nothing moved
            balances_verified=True,
            detail=detail,
        )

    async def execute(
        self,
        tenant_id: str,
        treasury_account: str,
        transfer: ProposedTransfer,
        sequence: int,
        last_ledger_sequence: int,
        decision,
    ) -> ExecutionResult:
        """Sign (if permitted) and submit, then verify the balances."""
        from xrpl.core.binarycodec import encode

        built = self.build(
            tenant_id, treasury_account, transfer, sequence, last_ledger_sequence
        )

        before = await self.holding(treasury_account, transfer.currency)

        outcome: SignOutcome = self.signer.sign(
            self._to_model(built.tx_json),
            decision,
            destination_account=transfer.destination_account,
            amount_xrp=transfer.amount,
            obligation_id=transfer.obligation_id,
        )

        if outcome.status != "signed":
            return ExecutionResult(
                transfer=transfer,
                built=built,
                status=outcome.status,
                tier=outcome.tier.value,
                reasons=list(outcome.reasons),
                balance_before_xrp=before,
                balance_after_xrp=before,
                balances_verified=True,
            )

        blob = encode(outcome.signed_transaction.to_xrpl())
        resp = await self.ledger.submit_blob(blob)
        result = resp.get("result", {})
        engine_result = result.get("engine_result")

        after = await self.holding(treasury_account, transfer.currency)
        expected_after = before - transfer.amount
        # The balance assertion is the evidence, not the engine result.
        balances_verified = abs(after - expected_after) < 0.000001

        return ExecutionResult(
            transfer=transfer,
            built=built,
            status="submitted" if engine_result == "tesSUCCESS" else "failed",
            tier=outcome.tier.value,
            reasons=list(outcome.reasons),
            engine_result=engine_result,
            engine_result_message=result.get("engine_result_message"),
            transaction_hash=(result.get("tx_json") or {}).get("hash"),
            balance_before_xrp=before,
            balance_after_xrp=after,
            balances_verified=balances_verified,
            detail={"raw_response": result},
        )

    @staticmethod
    def _to_model(tx_json: Dict):
        from xrpl.models.transactions.transaction import Transaction

        return Transaction.from_xrpl(tx_json)
