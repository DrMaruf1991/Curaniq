"""
Live RxNorm integration test.

These tests hit the real `rxnav.nlm.nih.gov` API. They are skipped by default
to keep CI offline-safe.

To run:
    CURANIQ_RUN_LIVE=1 python -m pytest tests/test_rxnorm_live.py -v

The tests validate that the RxNormConnector correctly handles the real
API response shape, not just the hand-constructed fixtures in
test_session_b_contract.py.

The drug fixtures here are well-known stable RxCUIs that are extremely
unlikely to change:
    metformin     → 6809
    acetaminophen → 161
    warfarin      → 11289

If RxNorm ever changes these mappings (it has not in 20 years), the test
fails loudly so we know to update the connector.

Also doubles as a smoke test for the user's deployment environment —
runs on the A100 box to confirm outbound HTTPS to rxnav.nlm.nih.gov is
unblocked.
"""
from __future__ import annotations

import os

import pytest

LIVE = os.getenv("CURANIQ_RUN_LIVE", "").lower() in ("1", "true", "yes")

pytestmark = pytest.mark.skipif(
    not LIVE,
    reason="set CURANIQ_RUN_LIVE=1 to run live RxNorm tests against rxnav.nlm.nih.gov",
)


@pytest.fixture(scope="module")
def conn():
    from curaniq.knowledge import RxNormConnector
    c = RxNormConnector()
    yield c
    c.close()


class TestRxNormLive:

    def test_metformin_resolves_to_known_rxcui(self, conn):
        """metformin must resolve to RxCUI 6809 (NLM-stable for 20+ years)."""
        norm = conn.normalize("metformin")
        assert norm is not None
        assert norm.rxcui == "6809"
        assert norm.canonical_name.lower() == "metformin"
        assert norm.tty == "IN"

    def test_acetaminophen_resolves_to_known_rxcui(self, conn):
        norm = conn.normalize("acetaminophen")
        assert norm is not None
        assert norm.rxcui == "161"
        assert norm.canonical_name.lower() == "acetaminophen"

    def test_paracetamol_resolves_to_acetaminophen(self, conn):
        """INN/BAN spelling resolves to USAN."""
        norm = conn.normalize("paracetamol")
        assert norm is not None
        # RxNorm maps paracetamol → acetaminophen via synonym graph
        assert norm.rxcui == "161"

    def test_warfarin_resolves(self, conn):
        norm = conn.normalize("warfarin")
        assert norm is not None
        assert norm.rxcui == "11289"

    def test_glucophage_resolves_to_metformin(self, conn):
        """Brand → ingredient via RxNorm synonym graph."""
        norm = conn.normalize("Glucophage")
        assert norm is not None
        # Either it normalizes to ingredient (rxcui 6809) or to brand entry —
        # both are valid RxNorm behaviors. Check synonyms include metformin.
        all_syns_lower = [s.lower() for s in norm.synonyms]
        assert any("metformin" in s for s in all_syns_lower) or norm.rxcui == "6809"

    def test_unknown_drug_returns_none(self, conn):
        norm = conn.normalize("xyz-totally-fake-drug-9999-not-real")
        assert norm is None

    def test_provenance_carries_real_rxnorm_version(self, conn):
        norm = conn.normalize("metformin")
        assert norm is not None
        # Real RxNorm version strings look like "RxNorm_full_03032025"
        assert "RxNorm" in norm.provenance.evidence_version or norm.provenance.evidence_version != "unknown"
        assert norm.provenance.is_authoritative is True
        assert norm.provenance.extraction_method == "live_api"

    def test_warfarin_atc_class_is_antithrombotic(self, conn):
        """warfarin ATC code must include B01 (antithrombotic) family."""
        atc = conn.get_atc("warfarin")
        assert atc is not None
        assert atc.is_in_class("B01")  # antithrombotic agents

    def test_metformin_atc_class_is_a10(self, conn):
        """metformin must be in A10 (drugs used in diabetes)."""
        atc = conn.get_atc("metformin")
        assert atc is not None
        assert atc.is_in_class("A10")

    def test_router_uses_live_when_wired(self, conn):
        """End-to-end: RouterProvider with RxNorm wired returns is_authoritative=True."""
        from curaniq.knowledge import RouterProvider
        # Force demo so vendored fallback is constructed
        os.environ["CURANIQ_ENV"] = "demo"
        import curaniq.truth_core.config as tc, importlib
        importlib.reload(tc)
        import curaniq.knowledge.router as rmod
        importlib.reload(rmod)
        from curaniq.knowledge.router import RouterProvider as Router2

        r = Router2(rxnorm_connector=conn)
        norm = r.normalize_drug("metformin")
        assert norm is not None
        assert norm.rxcui == "6809"
        # Live hit, not fallback
        assert r.stats()["live_hits"] >= 1
        assert norm.provenance.is_authoritative is True
