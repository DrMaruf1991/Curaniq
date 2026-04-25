"""
Contract tests for curaniq.knowledge.

These tests pin the knowledge-layer contract:
  - Vendored snapshots must carry full provenance metadata.
  - Vendored snapshots must refuse instantiation in clinician_prod.
  - LiveEvidenceProvider with no connectors must raise KnowledgeUnavailableError
    (never silently degrade).
  - RouterProvider must fall back to vendored in demo, refuse in prod.
  - Fatal-error rules are universal (served in every env).
  - DoseBounds and FatalErrorRule reject malformed input at construction.
  - The methotrexate-weekly safe pattern correctly suppresses the daily warning.
  - Vincristine-IT triggers severity=emergency.

Adding a new provider implementation: copy the test class and run it
against your provider — the contract is the test surface.
"""
from __future__ import annotations

import importlib
import os
import re
import uuid

import pytest

# Test isolation: each test forces its own env via the `env` fixture below.


@pytest.fixture
def demo_env(monkeypatch):
    """Force CURANIQ_ENV=demo and reload truth_core.config so is_clinician_prod() is False."""
    monkeypatch.setenv("CURANIQ_ENV", "demo")
    import curaniq.truth_core.config as tc
    importlib.reload(tc)
    return tc


@pytest.fixture
def prod_env(monkeypatch):
    """Force CURANIQ_ENV=clinician_prod and reload truth_core.config."""
    monkeypatch.setenv("CURANIQ_ENV", "clinician_prod")
    import curaniq.truth_core.config as tc
    importlib.reload(tc)
    return tc


# ─── PROVENANCE INVARIANTS ─────────────────────────────────────────────────

class TestProvenance:
    """Provenance is the unforgivable thing — every fact must carry it."""

    def test_provenance_rejects_invalid_iso(self):
        from curaniq.knowledge import Provenance
        with pytest.raises(ValueError, match="ISO 8601"):
            Provenance(
                source="DAILYMED", source_url="https://x", snapshot_date_iso="not-a-date",
                evidence_version="v1", license_status="public_domain",
                extraction_method="manual_curation", is_authoritative=False,
            )

    def test_provenance_rejects_invalid_license(self):
        from curaniq.knowledge import Provenance
        with pytest.raises(ValueError, match="license_status"):
            Provenance(
                source="DAILYMED", source_url="https://x",
                snapshot_date_iso="2026-04-25T00:00:00Z",
                evidence_version="v1", license_status="MADE_UP",
                extraction_method="manual_curation", is_authoritative=False,
            )

    def test_provenance_rejects_invalid_extraction_method(self):
        from curaniq.knowledge import Provenance
        with pytest.raises(ValueError, match="extraction_method"):
            Provenance(
                source="DAILYMED", source_url="https://x",
                snapshot_date_iso="2026-04-25T00:00:00Z",
                evidence_version="v1", license_status="open",
                extraction_method="hallucinated", is_authoritative=False,
            )

    def test_provenance_accepts_valid(self):
        from curaniq.knowledge import Provenance
        p = Provenance(
            source="DAILYMED", source_url="https://dailymed.nlm.nih.gov/dailymed/",
            snapshot_date_iso="2026-04-25T00:00:00Z",
            evidence_version="vendored-2026.04.25.1", license_status="public_domain",
            extraction_method="manual_curation", is_authoritative=False,
        )
        assert p.is_authoritative is False
        assert p.source == "DAILYMED"


# ─── DOSE BOUNDS VALIDATION ────────────────────────────────────────────────

class TestDoseBounds:

    def _prov(self):
        from curaniq.knowledge import Provenance
        return Provenance(
            source="DAILYMED", source_url="https://x",
            snapshot_date_iso="2026-04-25T00:00:00Z",
            evidence_version="v1", license_status="public_domain",
            extraction_method="manual_curation", is_authoritative=False,
        )

    def test_rejects_uppercase_drug(self):
        from curaniq.knowledge import DoseBounds
        with pytest.raises(ValueError, match="lowercase"):
            DoseBounds(drug="Metformin", min_single_dose_mg=250, max_single_dose_mg=1000,
                       route_context="oral", provenance=self._prov())

    def test_rejects_negative_min(self):
        from curaniq.knowledge import DoseBounds
        with pytest.raises(ValueError, match="min_single_dose_mg"):
            DoseBounds(drug="metformin", min_single_dose_mg=-1, max_single_dose_mg=1000,
                       route_context="oral", provenance=self._prov())

    def test_rejects_max_below_min(self):
        from curaniq.knowledge import DoseBounds
        with pytest.raises(ValueError, match="max < min"):
            DoseBounds(drug="metformin", min_single_dose_mg=1000, max_single_dose_mg=500,
                       route_context="oral", provenance=self._prov())

    def test_rejects_tolerance_below_one(self):
        from curaniq.knowledge import DoseBounds
        with pytest.raises(ValueError, match="tolerance_factor"):
            DoseBounds(drug="metformin", min_single_dose_mg=250, max_single_dose_mg=1000,
                       route_context="oral", tolerance_factor=0.5, provenance=self._prov())


# ─── VENDORED PROVIDER ─────────────────────────────────────────────────────

class TestVendoredSnapshotProvider:

    def test_loads_dose_bounds_with_provenance(self, demo_env):
        from curaniq.knowledge import VendoredSnapshotProvider
        v = VendoredSnapshotProvider()
        bounds = v.get_dose_bounds("methotrexate")
        assert bounds is not None
        assert bounds.min_single_dose_mg == 2.5
        assert bounds.max_single_dose_mg == 30.0
        assert bounds.provenance.source == "DAILYMED"
        assert bounds.provenance.is_authoritative is False
        assert bounds.provenance.snapshot_date_iso.startswith("2026-")

    def test_returns_none_for_unknown_drug(self, demo_env):
        from curaniq.knowledge import VendoredSnapshotProvider
        v = VendoredSnapshotProvider()
        assert v.get_dose_bounds("totally-fake-drug-xyz") is None

    def test_loads_six_fatal_rules(self, demo_env):
        from curaniq.knowledge import VendoredSnapshotProvider
        v = VendoredSnapshotProvider()
        rules = list(v.iter_fatal_error_rules())
        drugs = {r.drug for r in rules}
        # The architecture's named fatal patterns ALL must be present
        for required in {"methotrexate", "vincristine", "heparin", "colchicine", "insulin", "morphine"}:
            assert required in drugs, f"missing fatal rule for {required}"

    def test_methotrexate_daily_triggers(self, demo_env):
        from curaniq.knowledge import VendoredSnapshotProvider
        v = VendoredSnapshotProvider()
        rule = next(r for r in v.iter_fatal_error_rules() if r.drug == "methotrexate")
        violated, msg = rule.evaluate("Methotrexate 15 mg by mouth daily for rheumatoid arthritis.")
        assert violated is True
        assert "FATAL" in msg

    def test_methotrexate_weekly_safe(self, demo_env):
        from curaniq.knowledge import VendoredSnapshotProvider
        v = VendoredSnapshotProvider()
        rule = next(r for r in v.iter_fatal_error_rules() if r.drug == "methotrexate")
        violated, _ = rule.evaluate("Methotrexate 15 mg by mouth weekly for rheumatoid arthritis.")
        assert violated is False

    def test_vincristine_IT_is_emergency(self, demo_env):
        from curaniq.knowledge import VendoredSnapshotProvider
        v = VendoredSnapshotProvider()
        rule = next(r for r in v.iter_fatal_error_rules() if r.drug == "vincristine")
        assert rule.severity == "emergency"
        violated, _ = rule.evaluate("Administer vincristine 2 mg intrathecal.")
        assert violated is True

    def test_refuses_in_clinician_prod(self, prod_env):
        from curaniq.knowledge import VendoredSnapshotProvider, VendoredDataRefusedError
        with pytest.raises(VendoredDataRefusedError):
            VendoredSnapshotProvider()

    def test_allow_in_prod_for_rules_only(self, prod_env):
        """Rules artifact has is_authoritative=True — must be loadable in prod."""
        from curaniq.knowledge import VendoredSnapshotProvider
        # allow_in_prod=True is the documented escape hatch for rules
        v = VendoredSnapshotProvider(allow_in_prod=True)
        rules = list(v.iter_fatal_error_rules())
        assert len(rules) >= 6


# ─── LIVE PROVIDER FAIL-CLOSED ─────────────────────────────────────────────

class TestLiveEvidenceProvider:

    def test_unwired_dose_bounds_raises(self):
        from curaniq.knowledge import LiveEvidenceProvider, KnowledgeUnavailableError
        live = LiveEvidenceProvider()
        with pytest.raises(KnowledgeUnavailableError) as exc:
            live.get_dose_bounds("metformin")
        assert "no live connector" in str(exc.value).lower() or "unavailable" in str(exc.value).lower()

    def test_unwired_fatal_rules_raises(self):
        from curaniq.knowledge import LiveEvidenceProvider, KnowledgeUnavailableError
        live = LiveEvidenceProvider()
        with pytest.raises(KnowledgeUnavailableError):
            list(live.iter_fatal_error_rules())


# ─── ROUTER POLICY ─────────────────────────────────────────────────────────

class TestRouterProvider:

    def test_demo_falls_back_to_vendored_for_dose_bounds(self, demo_env):
        # Reload router so it picks up demo env
        import curaniq.knowledge.router as rmod; importlib.reload(rmod)
        from curaniq.knowledge.router import RouterProvider
        r = RouterProvider()
        bounds = r.get_dose_bounds("methotrexate")
        assert bounds is not None
        assert bounds.provenance.is_authoritative is False
        assert r.stats()["fallback_hits"] >= 1

    def test_clinician_prod_refuses_dose_bounds(self, prod_env):
        import curaniq.knowledge.router as rmod; importlib.reload(rmod)
        from curaniq.knowledge.router import RouterProvider
        from curaniq.knowledge import KnowledgeUnavailableError
        r = RouterProvider()
        with pytest.raises(KnowledgeUnavailableError):
            r.get_dose_bounds("metformin")
        assert r.stats()["refusals"] >= 1

    def test_clinician_prod_still_serves_fatal_rules(self, prod_env):
        """Fatal rules are SAFETY LOGIC, not vendored data — must work in prod."""
        import curaniq.knowledge.router as rmod; importlib.reload(rmod)
        from curaniq.knowledge.router import RouterProvider
        r = RouterProvider()
        rules = list(r.iter_fatal_error_rules())
        assert len(rules) >= 6
