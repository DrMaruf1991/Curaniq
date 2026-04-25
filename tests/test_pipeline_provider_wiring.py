"""
FIX-34b regression tests — guard against the pipeline-wiring bug found in
audit. The audit revealed that `pipeline.py` constructed `OntologyNormalizer`,
`SafetyGateSuiteRunner`, and `ExtendedCQLEngine` WITHOUT injecting the
`RouterProvider`. As a result, drug-name normalization in production fell
back to "input as-is" mode — Tylenol stayed Tylenol, Glucophage stayed
Glucophage, Coumadin stayed Coumadin. The previous test suite did not
catch this because it tested engines directly with provider injection.

These tests construct the FULL `CURANIQPipeline` and verify the provider
flows through to every engine that needs it, AND that brand-name
resolution actually fires through the production code path.

If `pipeline.py` ever drops the provider injection again, these tests fail.
"""
from __future__ import annotations

import importlib
import os

import pytest


@pytest.fixture(scope="module")
def demo_pipeline():
    os.environ["CURANIQ_ENV"] = "demo"
    import curaniq.truth_core.config as tc
    importlib.reload(tc)
    from curaniq.core.pipeline import CURANIQPipeline
    return CURANIQPipeline()


class TestPipelineWiresProvider:
    """The pipeline MUST construct one RouterProvider and pass it to all engines."""

    def test_pipeline_has_knowledge_provider(self, demo_pipeline):
        from curaniq.knowledge import RouterProvider
        assert hasattr(demo_pipeline, "knowledge_provider")
        assert isinstance(demo_pipeline.knowledge_provider, RouterProvider)

    def test_ontology_normalizer_received_provider(self, demo_pipeline):
        from curaniq.knowledge import RouterProvider
        assert demo_pipeline.ontology._kp is demo_pipeline.knowledge_provider
        assert isinstance(demo_pipeline.ontology._kp, RouterProvider)

    def test_extended_cql_received_provider(self, demo_pipeline):
        from curaniq.knowledge import RouterProvider
        assert demo_pipeline.extended_cql._knowledge_provider is demo_pipeline.knowledge_provider
        assert isinstance(demo_pipeline.extended_cql._knowledge_provider, RouterProvider)

    def test_safety_suite_received_provider(self, demo_pipeline):
        from curaniq.knowledge import RouterProvider
        assert demo_pipeline.safety_suite._knowledge_provider is demo_pipeline.knowledge_provider
        assert isinstance(demo_pipeline.safety_suite._knowledge_provider, RouterProvider)


class TestBrandNameResolutionInProduction:
    """The actual regression: brand name → ingredient must work via pipeline."""

    @pytest.mark.parametrize("input_name,expected_rxcui,expected_canonical_substring", [
        ("Tylenol", "161", "acetaminophen"),
        ("acetaminophen", "161", "acetaminophen"),
        ("paracetamol", "161", "acetaminophen"),
        ("Glucophage", "6809", "metformin"),
        ("metformin", "6809", "metformin"),
        ("Coumadin", "11289", "warfarin"),
        ("warfarin", "11289", "warfarin"),
        ("Lipitor", "83367", "atorvastatin"),
        ("Plavix", "32968", "clopidogrel"),
    ])
    def test_brand_to_ingredient_resolution(
        self, demo_pipeline, input_name, expected_rxcui, expected_canonical_substring
    ):
        """Production path: pipeline.ontology.normalize_drug() must resolve brand→ingredient."""
        mapping = demo_pipeline.ontology.normalize_drug(input_name)
        assert mapping.rxcui == expected_rxcui, (
            f"Brand-name resolution regression: {input_name!r} should map to "
            f"rxcui {expected_rxcui!r}, got {mapping.rxcui!r}. "
            f"This means pipeline.ontology lost its knowledge_provider injection."
        )
        assert expected_canonical_substring in mapping.canonical_term.lower(), (
            f"Canonical mismatch for {input_name!r}: expected to contain "
            f"{expected_canonical_substring!r}, got {mapping.canonical_term!r}"
        )

    def test_cis_cyrillic_still_resolves(self, demo_pipeline):
        """Cyrillic brand names must still resolve via the legacy CIS-variants fallback."""
        mapping = demo_pipeline.ontology.normalize_drug("Панадол")
        assert "paracetamol" in mapping.canonical_term.lower() or \
               "acetaminophen" in mapping.canonical_term.lower(), \
               f"Cyrillic 'Панадол' lost resolution: got {mapping.canonical_term!r}"

    def test_unknown_drug_returns_low_confidence(self, demo_pipeline):
        """Unknown drugs should return the input with low confidence — never raise."""
        mapping = demo_pipeline.ontology.normalize_drug("totally-fake-drug-xyz")
        assert mapping.canonical_term == "totally-fake-drug-xyz"
        assert mapping.rxcui is None
        assert mapping.confidence < 1.0
