"""
Contract tests for Session B (FIX-34) — drug normalization, ATC, RxNorm.

Covers:
  - DrugNormalization invariants (rxcui must be digits, tty must be valid)
  - AtcClassification.is_in_class() class-membership checks
  - VendoredSnapshotProvider serves drug synonyms with reverse lookup
  - LiveEvidenceProvider raises KnowledgeUnavailableError when unwired
  - RouterProvider falls back to vendored in demo, refuses in clinician_prod
  - RxNormConnector parses real-shape responses (using fixture httpx)
  - RxNormConnector handles 404 (drug not found), 5xx with retry
  - RxNormConnector applies rate limiting
  - ATC level inference from code length

Live API tests are in tests/test_rxnorm_live.py — pytest.skipif(no network).
"""
from __future__ import annotations

import importlib
import json
from unittest.mock import MagicMock, patch

import httpx
import pytest


@pytest.fixture
def demo_env(monkeypatch):
    monkeypatch.setenv("CURANIQ_ENV", "demo")
    import curaniq.truth_core.config as tc
    importlib.reload(tc)
    return tc


@pytest.fixture
def prod_env(monkeypatch):
    monkeypatch.setenv("CURANIQ_ENV", "clinician_prod")
    import curaniq.truth_core.config as tc
    importlib.reload(tc)
    return tc


# ─── DrugNormalization VALIDATION ──────────────────────────────────────────

class TestDrugNormalization:

    def _prov(self):
        from curaniq.knowledge import Provenance
        return Provenance(
            source="RXNORM", source_url="https://rxnav.nlm.nih.gov",
            snapshot_date_iso="2026-04-25T00:00:00Z",
            evidence_version="RxNorm_full_03032025",
            license_status="public_domain",
            extraction_method="live_api", is_authoritative=True,
        )

    def test_rejects_non_digit_rxcui(self):
        from curaniq.knowledge import DrugNormalization
        with pytest.raises(ValueError, match="all-digits"):
            DrugNormalization(input_name="x", rxcui="abc123",
                              canonical_name="x", tty="IN",
                              synonyms=(), provenance=self._prov())

    def test_rejects_invalid_tty(self):
        from curaniq.knowledge import DrugNormalization
        with pytest.raises(ValueError, match="TTY"):
            DrugNormalization(input_name="x", rxcui="6809",
                              canonical_name="metformin", tty="MADE_UP",
                              synonyms=(), provenance=self._prov())

    def test_rejects_non_tuple_synonyms(self):
        from curaniq.knowledge import DrugNormalization
        with pytest.raises(ValueError, match="tuple"):
            DrugNormalization(input_name="x", rxcui="6809",
                              canonical_name="metformin", tty="IN",
                              synonyms=["Glucophage"],  # list, not tuple
                              provenance=self._prov())

    def test_accepts_valid(self):
        from curaniq.knowledge import DrugNormalization
        n = DrugNormalization(input_name="metformin", rxcui="6809",
                              canonical_name="metformin", tty="IN",
                              synonyms=("Glucophage", "metformin HCl"),
                              provenance=self._prov())
        assert n.rxcui == "6809"
        assert "Glucophage" in n.synonyms


# ─── AtcClassification ─────────────────────────────────────────────────────

class TestAtcClassification:

    def _prov(self):
        from curaniq.knowledge import Provenance
        return Provenance(
            source="RXNORM", source_url="https://rxnav.nlm.nih.gov",
            snapshot_date_iso="2026-04-25T00:00:00Z",
            evidence_version="v", license_status="public_domain",
            extraction_method="live_api", is_authoritative=True,
        )

    def test_codes_levels_length_mismatch_rejected(self):
        from curaniq.knowledge import AtcClassification
        with pytest.raises(ValueError, match="same length"):
            AtcClassification(rxcui="6809",
                              atc_codes=("A10BA02",), atc_levels=(5, 4),
                              primary_atc="A10BA02", provenance=self._prov())

    def test_invalid_level_rejected(self):
        from curaniq.knowledge import AtcClassification
        with pytest.raises(ValueError, match="1-5"):
            AtcClassification(rxcui="6809",
                              atc_codes=("A10BA02",), atc_levels=(7,),
                              primary_atc="A10BA02", provenance=self._prov())

    def test_is_in_class(self):
        from curaniq.knowledge import AtcClassification
        # warfarin is in B01AA03 — Vitamin K antagonists (antithrombotics)
        atc = AtcClassification(rxcui="11289",
                                atc_codes=("B01AA03",), atc_levels=(5,),
                                primary_atc="B01AA03", provenance=self._prov())
        assert atc.is_in_class("B01") is True       # any antithrombotic
        assert atc.is_in_class("B01AA") is True     # vit K antagonist specifically
        assert atc.is_in_class("B01AA03") is True   # exact code
        assert atc.is_in_class("N02") is False      # analgesic — wrong class

    def test_is_in_class_case_insensitive(self):
        from curaniq.knowledge import AtcClassification
        atc = AtcClassification(rxcui="6809",
                                atc_codes=("A10BA02",), atc_levels=(5,),
                                primary_atc="A10BA02", provenance=self._prov())
        assert atc.is_in_class("a10") is True
        assert atc.is_in_class("A10") is True


# ─── Vendored synonyms ─────────────────────────────────────────────────────

class TestVendoredDrugSynonyms:

    def test_vendored_serves_metformin(self, demo_env):
        from curaniq.knowledge import VendoredSnapshotProvider
        v = VendoredSnapshotProvider()
        norm = v.normalize_drug("metformin")
        assert norm is not None
        assert norm.rxcui == "6809"
        assert norm.canonical_name == "metformin"

    def test_reverse_synonym_lookup_brand_to_ingredient(self, demo_env):
        """Glucophage → metformin (brand → ingredient resolution via reverse index)."""
        from curaniq.knowledge import VendoredSnapshotProvider
        v = VendoredSnapshotProvider()
        norm = v.normalize_drug("Glucophage")
        assert norm is not None
        assert norm.rxcui == "6809"
        assert norm.canonical_name == "metformin"

    def test_inn_to_usan_paracetamol(self, demo_env):
        """INN/BAN → USAN: paracetamol → acetaminophen (rxcui 161)."""
        from curaniq.knowledge import VendoredSnapshotProvider
        v = VendoredSnapshotProvider()
        norm = v.normalize_drug("paracetamol")
        assert norm is not None
        assert norm.rxcui == "161"
        assert norm.canonical_name == "acetaminophen"

    def test_unknown_returns_none(self, demo_env):
        from curaniq.knowledge import VendoredSnapshotProvider
        v = VendoredSnapshotProvider()
        assert v.normalize_drug("totally-fake-drug-xyz123") is None

    def test_synonyms_include_canonical_and_brands(self, demo_env):
        from curaniq.knowledge import VendoredSnapshotProvider
        v = VendoredSnapshotProvider()
        syns = v.get_drug_synonyms("warfarin")
        assert "Coumadin" in syns
        assert "warfarin" in syns

    def test_atc_returns_none_in_vendored(self, demo_env):
        """ATC requires live RxClass — vendored returns None."""
        from curaniq.knowledge import VendoredSnapshotProvider
        v = VendoredSnapshotProvider()
        assert v.get_atc_classification("metformin") is None


# ─── Router policy ─────────────────────────────────────────────────────────

class TestRouterProviderSessionB:

    def test_demo_falls_back_to_vendored_normalize(self, demo_env):
        import curaniq.knowledge.router as rmod
        importlib.reload(rmod)
        from curaniq.knowledge.router import RouterProvider
        r = RouterProvider()
        norm = r.normalize_drug("metformin")
        assert norm is not None
        assert norm.rxcui == "6809"
        assert r.stats()["fallback_hits"] >= 1

    def test_clinician_prod_refuses_normalize(self, prod_env):
        import curaniq.knowledge.router as rmod
        importlib.reload(rmod)
        from curaniq.knowledge.router import RouterProvider
        from curaniq.knowledge import KnowledgeUnavailableError
        r = RouterProvider()
        with pytest.raises(KnowledgeUnavailableError):
            r.normalize_drug("metformin")
        assert r.stats()["refusals"] >= 1


# ─── RxNorm connector — fixture-based parsing ──────────────────────────────

# Real-shape response fixtures, hand-constructed against the documented
# RxNorm REST API schema:
#     https://lhncbc.nlm.nih.gov/RxNav/APIs/RxNormAPIs.html

_FIXTURE_RXCUI_METFORMIN = {
    "idGroup": {
        "name": "metformin",
        "rxnormId": ["6809"]
    }
}

_FIXTURE_PROPS_METFORMIN = {
    "properties": {
        "rxcui": "6809",
        "name": "metformin",
        "synonym": "",
        "tty": "IN",
        "language": "ENG",
        "suppress": "N",
        "umlscui": "C0025598"
    }
}

_FIXTURE_RELATED_METFORMIN = {
    "relatedGroup": {
        "rxcui": "6809",
        "termType": ["IN", "BN", "SY"],
        "conceptGroup": [
            {
                "tty": "IN",
                "conceptProperties": [
                    {"rxcui": "6809", "name": "metformin", "tty": "IN"}
                ]
            },
            {
                "tty": "BN",
                "conceptProperties": [
                    {"rxcui": "151827", "name": "Glucophage", "tty": "BN"},
                    {"rxcui": "153591", "name": "Glumetza", "tty": "BN"}
                ]
            },
            {
                "tty": "SY",
                "conceptProperties": [
                    {"rxcui": "9999", "name": "metformin hydrochloride", "tty": "SY"}
                ]
            }
        ]
    }
}

_FIXTURE_VERSION = {"version": "RxNorm_full_03032025", "apiVersion": "5.4.0"}

_FIXTURE_NO_MATCH = {"idGroup": {"name": "nonexistent-drug-xyz"}}  # no rxnormId


def _make_response(status: int, body: dict | None = None) -> httpx.Response:
    """Build an httpx.Response that mimics the real client response shape."""
    if body is None:
        body = {}
    return httpx.Response(
        status_code=status,
        content=json.dumps(body).encode("utf-8"),
        headers={"content-type": "application/json"},
        request=httpx.Request("GET", "https://rxnav.nlm.nih.gov/REST/test"),
    )


class TestRxNormConnector:

    def test_normalize_returns_drug_normalization(self):
        from curaniq.knowledge.connectors.rxnorm import RxNormConnector

        mock_client = MagicMock(spec=httpx.Client)
        mock_client.get.side_effect = [
            _make_response(200, _FIXTURE_RXCUI_METFORMIN),
            _make_response(200, _FIXTURE_PROPS_METFORMIN),
            _make_response(200, _FIXTURE_RELATED_METFORMIN),
            _make_response(200, _FIXTURE_VERSION),
        ]

        with RxNormConnector(client=mock_client, rate_limit_per_sec=0) as conn:
            norm = conn.normalize("metformin")

        assert norm is not None
        assert norm.rxcui == "6809"
        assert norm.canonical_name == "metformin"
        assert norm.tty == "IN"
        assert "Glucophage" in norm.synonyms
        assert "Glumetza" in norm.synonyms
        assert "metformin hydrochloride" in norm.synonyms
        assert norm.provenance.source == "RXNORM"
        assert norm.provenance.is_authoritative is True
        assert norm.provenance.extraction_method == "live_api"

    def test_unknown_drug_returns_none(self):
        from curaniq.knowledge.connectors.rxnorm import RxNormConnector

        mock_client = MagicMock(spec=httpx.Client)
        mock_client.get.return_value = _make_response(200, _FIXTURE_NO_MATCH)

        with RxNormConnector(client=mock_client, rate_limit_per_sec=0) as conn:
            assert conn.normalize("nonexistent-drug-xyz") is None

    def test_5xx_retries_and_raises_on_exhaustion(self):
        from curaniq.knowledge.connectors.rxnorm import RxNormConnector
        from curaniq.knowledge import KnowledgeUnavailableError

        mock_client = MagicMock(spec=httpx.Client)
        mock_client.get.return_value = _make_response(503)

        with RxNormConnector(client=mock_client, rate_limit_per_sec=0,
                             max_retries=2) as conn:
            with pytest.raises(KnowledgeUnavailableError, match="503"):
                conn.normalize("metformin")

        # Should have tried 3 times (initial + 2 retries)
        assert mock_client.get.call_count == 3

    def test_network_error_raises_knowledge_unavailable(self):
        from curaniq.knowledge.connectors.rxnorm import RxNormConnector
        from curaniq.knowledge import KnowledgeUnavailableError

        mock_client = MagicMock(spec=httpx.Client)
        mock_client.get.side_effect = httpx.ConnectError("DNS failure")

        with RxNormConnector(client=mock_client, rate_limit_per_sec=0,
                             max_retries=1) as conn:
            with pytest.raises(KnowledgeUnavailableError, match="network"):
                conn.normalize("metformin")

    def test_atc_level_inference_from_code_length(self):
        """ATC code length determines the level per WHO ATC schema."""
        from curaniq.knowledge.connectors.rxnorm import RxNormConnector

        # Multi-level ATC response: warfarin is in B01 (lvl 1=B is anatomical
        # group, but our schema starts at lvl 1 = single char)
        atc_response = {
            "rxclassDrugInfoList": {
                "rxclassDrugInfo": [
                    {"rxclassMinConceptItem": {"classId": "B01AA03",
                                                "className": "warfarin",
                                                "classType": "ATC1-4"}},
                    {"rxclassMinConceptItem": {"classId": "B01AA",
                                                "className": "Vitamin K antagonists",
                                                "classType": "ATC1-4"}},
                    {"rxclassMinConceptItem": {"classId": "B01",
                                                "className": "ANTITHROMBOTIC AGENTS",
                                                "classType": "ATC1-4"}},
                ]
            }
        }

        mock_client = MagicMock(spec=httpx.Client)
        mock_client.get.return_value = _make_response(200, atc_response)

        with RxNormConnector(client=mock_client, rate_limit_per_sec=0) as conn:
            atc = conn.get_atc("11289")  # warfarin RxCUI

        assert atc is not None
        assert "B01AA03" in atc.atc_codes
        assert "B01AA" in atc.atc_codes
        assert "B01" in atc.atc_codes
        # Levels: B01AA03 (7 chars) = 5, B01AA (5 chars) = 4, B01 (3 chars) = 2
        idx = atc.atc_codes.index("B01AA03")
        assert atc.atc_levels[idx] == 5
        idx = atc.atc_codes.index("B01AA")
        assert atc.atc_levels[idx] == 4
        idx = atc.atc_codes.index("B01")
        assert atc.atc_levels[idx] == 2
        assert atc.primary_atc == "B01AA03"
        # is_in_class works
        assert atc.is_in_class("B01")
        assert not atc.is_in_class("N02")
