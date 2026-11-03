"""
The decision log.

Every decision — forecast, proposal, policy evaluation, transaction hash,
outcome — becomes a log entry. This is the artifact an auditor actually
reads, and it is what makes "validate the payment once it completes"
verifiable rather than a promise.

The format is a hash-chained append-only log: each entry carries the hash of
the previous entry, so a rewritten or removed entry is detectable. That is the
same shape as `xrpl_agent_log`, and Ripple Custody's own vault architecture
validates the design (Merkle tree over trusted entities, notary-signed root,
per-entity signatures, independently verifiable).

This module is the adapter, not a new log format. The chain logic is small
enough to stand alone for the demo; the production path binds `xrpl_agent_log`
and anchors the root on-chain.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional


def _entry_hash(payload: Dict) -> str:
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest().upper()


@dataclass
class LogEntry:
    """One entry in the decision log."""

    sequence: int
    timestamp: str
    tenant_id: str
    event: str
    detail: Dict = field(default_factory=dict)
    previous_hash: Optional[str] = None
    entry_hash: Optional[str] = None

    def compute_hash(self) -> str:
        payload = {
            "sequence": self.sequence,
            "timestamp": self.timestamp,
            "tenant_id": self.tenant_id,
            "event": self.event,
            "detail": self.detail,
            "previous_hash": self.previous_hash,
        }
        return _entry_hash(payload)

    def to_dict(self) -> Dict:
        return {
            "sequence": self.sequence,
            "timestamp": self.timestamp,
            "tenant_id": self.tenant_id,
            "event": self.event,
            "detail": self.detail,
            "previous_hash": self.previous_hash,
            "entry_hash": self.entry_hash,
        }


class AgentLog:
    """A hash-chained, append-only decision log."""

    def __init__(self, path: Optional[pathlib.Path] = None):
        self.path = pathlib.Path(path) if path else None
        self._entries: List[LogEntry] = []
        self._load()

    def _load(self) -> None:
        if not self.path or not self.path.exists():
            return
        for line in self.path.read_text().splitlines():
            if not line.strip():
                continue
            try:
                self._entries.append(LogEntry(**json.loads(line)))
            except Exception:
                continue

    @property
    def head_hash(self) -> Optional[str]:
        return self._entries[-1].entry_hash if self._entries else None

    def append(self, tenant_id: str, event: str, detail: Optional[Dict] = None) -> LogEntry:
        entry = LogEntry(
            sequence=len(self._entries),
            timestamp=datetime.now(timezone.utc).isoformat(),
            tenant_id=tenant_id,
            event=event,
            detail=detail or {},
            previous_hash=self.head_hash,
        )
        entry.entry_hash = entry.compute_hash()
        self._entries.append(entry)
        self._persist(entry)
        return entry

    def _persist(self, entry: LogEntry) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a") as handle:
            handle.write(json.dumps(entry.to_dict()) + "\n")

    def entries(self, tenant_id: Optional[str] = None, limit: Optional[int] = None):
        result = self._entries
        if tenant_id:
            result = [e for e in result if e.tenant_id == tenant_id]
        if limit:
            result = result[-limit:]
        return result

    def verify(self) -> Dict:
        """Recompute the chain and report where it breaks, if anywhere."""
        previous = None
        for entry in self._entries:
            if entry.previous_hash != previous:
                return {
                    "valid": False,
                    "broken_at": entry.sequence,
                    "reason": "previous_hash_mismatch",
                }
            expected = entry.compute_hash()
            if expected != entry.entry_hash:
                return {
                    "valid": False,
                    "broken_at": entry.sequence,
                    "reason": "entry_hash_mismatch",
                }
            previous = entry.entry_hash
        return {"valid": True, "entries": len(self._entries), "head": previous}
