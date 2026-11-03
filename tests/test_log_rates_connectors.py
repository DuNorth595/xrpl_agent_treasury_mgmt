"""
Tests for the decision log, the rate service, and the connector layer.

The decision log's integrity claim must be CHECKABLE, not asserted. The rate
service must never turn a lookup failure into a wrong number. And a
connector failure must never look like an empty result.

Run with:  pytest tests/test_log_rates_connectors.py -v
"""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

sys.path.insert(
    0, str(pathlib.Path(__file__).resolve().parent.parent / "src")
)

from xrpl_agent_treasury_mgmt.agent_log import AgentLog  # noqa: E402
from xrpl_agent_treasury_mgmt.connectors import (  # noqa: E402
    ConnectorConfig,
    ConnectorError,
    ConnectorRegistry,
    Credential,
    credential_from_env,
)
from xrpl_agent_treasury_mgmt.currency import Rate  # noqa: E402
from xrpl_agent_treasury_mgmt.rates import (  # noqa: E402
    CachedRates,
    RateService,
    StaticRateProvider,
)


class TestAgentLog:
    def test_an_empty_log_verifies(self, tmp_path):
        log = AgentLog(tmp_path / "log.jsonl")
        result = log.verify()
        assert result["valid"] is True
        assert result["entries"] == 0

    def test_entries_chain(self, tmp_path):
        log = AgentLog(tmp_path / "log.jsonl")
        log.append("t", "plan_generated", {"n": 1})
        log.append("t", "policy_evaluated", {"n": 2})
        log.append("t", "authorisation_recorded", {"n": 3})
        assert len(log.entries()) == 3
        assert log.verify()["valid"] is True

    def test_each_entry_carries_the_previous_hash(self, tmp_path):
        log = AgentLog(tmp_path / "log.jsonl")
        first = log.append("t", "a", {})
        second = log.append("t", "b", {})
        assert second.previous_hash == first.entry_hash
        assert first.previous_hash is None

    def test_the_head_hash_is_the_latest(self, tmp_path):
        log = AgentLog(tmp_path / "log.jsonl")
        log.append("t", "a", {})
        last = log.append("t", "b", {})
        assert log.head_hash == last.entry_hash

    def test_a_tampered_entry_is_detected(self, tmp_path):
        """The integrity claim must be checkable, not asserted."""
        path = tmp_path / "log.jsonl"
        log = AgentLog(path)
        log.append("t", "plan_generated", {"amount": 100})
        log.append("t", "signed", {"amount": 100})

        # Rewrite the first entry's detail on disk.
        lines = path.read_text().strip().split("\n")
        first = json.loads(lines[0])
        first["detail"]["amount"] = 999999
        lines[0] = json.dumps(first)
        path.write_text("\n".join(lines) + "\n")

        tampered = AgentLog(path)
        result = tampered.verify()
        assert result["valid"] is False
        assert result["broken_at"] == 0
        assert result["reason"] == "entry_hash_mismatch"

    def test_a_removed_entry_is_detected(self, tmp_path):
        path = tmp_path / "log.jsonl"
        log = AgentLog(path)
        log.append("t", "a", {})
        log.append("t", "b", {})
        log.append("t", "c", {})

        lines = path.read_text().strip().split("\n")
        del lines[1]
        path.write_text("\n".join(lines) + "\n")

        result = AgentLog(path).verify()
        assert result["valid"] is False
        assert result["reason"] == "previous_hash_mismatch"

    def test_the_log_persists_and_reloads(self, tmp_path):
        path = tmp_path / "log.jsonl"
        log = AgentLog(path)
        log.append("t", "event", {"k": "v"})
        reloaded = AgentLog(path)
        assert len(reloaded.entries()) == 1
        assert reloaded.entries()[0].detail["k"] == "v"
        assert reloaded.verify()["valid"] is True

    def test_entries_can_be_filtered_by_tenant(self, tmp_path):
        log = AgentLog(tmp_path / "log.jsonl")
        log.append("tenant-a", "x", {})
        log.append("tenant-b", "y", {})
        assert len(log.entries("tenant-a")) == 1
        assert len(log.entries("tenant-b")) == 1

    def test_entries_can_be_limited(self, tmp_path):
        log = AgentLog(tmp_path / "log.jsonl")
        for i in range(10):
            log.append("t", f"event-{i}", {})
        assert len(log.entries(limit=3)) == 3


class TestRateService:
    def test_a_provider_that_works_is_used(self):
        service = RateService(
            providers=[StaticRateProvider({"CAD": 1.4}, at="2026-01-01")]
        )
        cache = service.refresh(["CAD"])
        assert cache.provider == "static"
        assert cache.rates["CAD"].units_per_usd == 1.4

    def test_a_failing_provider_falls_through_to_the_next(self):
        class Broken:
            name = "broken"

            def fetch(self, tickers):
                raise RuntimeError("unreachable")

        service = RateService(
            providers=[
                Broken(),
                StaticRateProvider({"CAD": 1.4}, at="2026-01-01"),
            ]
        )
        cache = service.refresh(["CAD"])
        assert cache.provider == "static"
        assert service.last_error is None

    def test_every_provider_failing_serves_the_stale_cache(self):
        """A rate lookup failure must not blank the table.

        A stale indicative rate beats no rate, as long as it is labelled
        stale. And it must never raise into a payment path.
        """
        class Broken:
            name = "broken"

            def fetch(self, tickers):
                raise RuntimeError("unreachable")

        service = RateService(providers=[Broken()])
        service.cache = CachedRates(
            rates={"CAD": Rate(quote="CAD", units_per_usd=1.4)},
            provider="previous",
        )
        cache = service.refresh(["CAD"])
        assert cache.provider == "previous"
        assert "unreachable" in (service.last_error or "")

    def test_the_service_never_raises_on_a_total_failure(self):
        class Broken:
            name = "broken"

            def fetch(self, tickers):
                raise RuntimeError("unreachable")

        service = RateService(providers=[Broken()])
        # This must not raise.
        service.get(["CAD"], force_refresh=True)
        assert service.last_error is not None

    def test_a_stale_cache_is_flagged(self):
        service = RateService(
            providers=[StaticRateProvider({"CAD": 1.4})],
            max_age_seconds=0.0,
        )
        cache = service.get(["CAD"], force_refresh=True)
        # max_age 0 means anything is stale immediately.
        assert cache.is_stale(0.0) is True

    def test_a_fresh_cache_is_not_stale(self):
        service = RateService(
            providers=[StaticRateProvider({"CAD": 1.4})],
            max_age_seconds=3600.0,
        )
        cache = service.get(["CAD"], force_refresh=True)
        assert cache.is_stale(3600.0) is False


class TestConnectorRegistry:
    def test_a_registered_connector_is_retrieved_by_name(self):
        registry = ConnectorRegistry()
        registry.register("erp", object())
        assert registry.get("erp") is not None

    def test_a_missing_connector_is_a_typed_error(self):
        registry = ConnectorRegistry()
        with pytest.raises(ConnectorError) as exc:
            registry.get("nope")
        assert "nope" in str(exc.value)
        assert "erp" in str(exc.value) or exc.value.connector == "nope"

    def test_duplicate_registration_is_refused(self):
        registry = ConnectorRegistry()
        registry.register("erp", object())
        with pytest.raises(ConnectorError):
            registry.register("erp", object())

    def test_describe_exposes_no_secrets(self):
        registry = ConnectorRegistry()
        registry.register(
            "custody",
            Credential(key="K", secret="super-secret", account_id="a"),
        )
        described = registry.describe()
        assert described[0]["name"] == "custody"
        assert "super-secret" not in json.dumps(described)

    def test_the_configuration_is_reported(self):
        registry = ConnectorRegistry()
        registry.register("erp", object())
        registry.configure(
            ConnectorConfig(name="erp", kind="erp", adapter="generic-rest-erp")
        )
        described = registry.describe()[0]
        assert described["kind"] == "erp"
        assert described["adapter"] == "generic-rest-erp"


class TestCredentials:
    def test_a_credential_is_built_from_the_environment(self, monkeypatch):
        monkeypatch.setenv("TEST_ERP_KEY", "s3cret")
        monkeypatch.setenv("TEST_ERP_KEY_ACCOUNT", "acct-1")
        monkeypatch.setenv("TEST_ERP_KEY_URL", "http://example.test")
        cred = credential_from_env("TEST_ERP_KEY")
        assert cred.secret == "s3cret"
        assert cred.account_id == "acct-1"
        assert cred.base_url == "http://example.test"

    def test_a_missing_optional_credential_returns_none(self, monkeypatch):
        monkeypatch.delenv("TEST_ABSENT_KEY", raising=False)
        assert credential_from_env("TEST_ABSENT_KEY", required=False) is None

    def test_the_credential_never_reprs_its_secret(self):
        cred = Credential(key="K", secret="super-secret", account_id="a")
        assert "super-secret" not in repr(cred)
