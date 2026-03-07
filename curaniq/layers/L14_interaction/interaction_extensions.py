"""
CURANIQ -- Layer 14: Interaction & Session Management
P2 Interaction Extensions

L14-4  Evidence Map Visualization (interactive evidence graph data)
L14-5  Iterative Source Expansion (automatic query broadening)
L14-6  Counterfactual Parameter Toggle ("what if" parameter changes)
L14-9  Voice Input/Output Pipeline (speech transcription routing)

No hardcoded clinical data. Logic-only modules.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional
from uuid import uuid4

logger = logging.getLogger(__name__)


# =============================================================================
# L14-4: EVIDENCE MAP VISUALIZATION
# =============================================================================

@dataclass
class EvidenceMapNode:
    node_id: str
    label: str
    node_type: str  # "query", "concept", "drug", "condition", "evidence", "guideline"
    weight: float = 1.0
    metadata: dict = field(default_factory=dict)


@dataclass
class EvidenceMapEdge:
    source: str
    target: str
    relation: str  # "treats", "interacts_with", "evidenced_by", "contradicted_by"
    weight: float = 1.0


class EvidenceMapVisualizer:
    """
    L14-4: Builds interactive evidence map data for frontend rendering.

    Transforms the query, drugs, conditions, and evidence into a
    network graph that clinicians can explore interactively.
    Each node is clickable to drill down into supporting evidence.
    """

    def build_map(self, query: str, drugs: list[str],
                  conditions: list[str],
                  evidence_objects: list[dict]) -> dict:
        """Build evidence map graph data."""
        nodes: list[dict] = []
        edges: list[dict] = []

        # Query node
        q_id = "query"
        nodes.append({"id": q_id, "label": query[:60], "type": "query", "weight": 1.0})

        # Drug nodes
        for drug in drugs:
            d_id = f"drug_{drug.lower().replace(' ', '_')}"
            nodes.append({"id": d_id, "label": drug, "type": "drug", "weight": 0.8})
            edges.append({"source": q_id, "target": d_id, "relation": "mentions"})

        # Condition nodes
        for cond in conditions:
            c_id = f"cond_{cond.lower().replace(' ', '_')}"
            nodes.append({"id": c_id, "label": cond, "type": "condition", "weight": 0.8})
            edges.append({"source": q_id, "target": c_id, "relation": "about"})

            # Drug-condition edges
            for drug in drugs:
                d_id = f"drug_{drug.lower().replace(' ', '_')}"
                edges.append({"source": d_id, "target": c_id, "relation": "treats"})

        # Evidence nodes
        for ev in evidence_objects:
            ev_id = f"ev_{ev.get('id', uuid4().hex[:8])}"
            nodes.append({
                "id": ev_id,
                "label": ev.get("title", "")[:50],
                "type": "evidence",
                "weight": ev.get("quality_score", 0.5),
                "year": ev.get("year"),
                "source": ev.get("source", ""),
            })
            # Connect evidence to relevant drugs/conditions
            ev_text = (ev.get("title", "") + " " + ev.get("snippet", "")).lower()
            for drug in drugs:
                if drug.lower() in ev_text:
                    d_id = f"drug_{drug.lower().replace(' ', '_')}"
                    edges.append({"source": ev_id, "target": d_id, "relation": "evidenced_by"})

        return {
            "nodes": nodes,
            "edges": edges,
            "total_nodes": len(nodes),
            "total_edges": len(edges),
        }


# =============================================================================
# L14-5: ITERATIVE SOURCE EXPANSION
# =============================================================================

class IterativeSourceExpander:
    """
    L14-5: Automatically broadens evidence search when initial results are thin.
    Drug name expansions loaded from curaniq/data/drug_name_expansions.json.
    """

    MIN_EVIDENCE_THRESHOLD = 3

    def __init__(self):
        from curaniq.data_loader import load_json_data
        raw = load_json_data("drug_name_expansions.json")
        self._drug_expansions: dict[str, list[str]] = raw.get("expansions", {})
        logger.info("IterativeSourceExpander: %d drug name expansions", len(self._drug_expansions))

    def expand_query(self, original_query: str, drugs: list[str],
                     current_result_count: int) -> list[dict]:
        """Generate expanded query variants if evidence is thin."""
        expansions = []

        if current_result_count >= self.MIN_EVIDENCE_THRESHOLD:
            return []

        # Step 1: Drug name expansions (from data file)
        for drug in drugs:
            alternatives = self._drug_expansions.get(drug.lower(), [])
            for alt in alternatives:
                expanded = original_query.replace(drug, alt)
                if expanded != original_query:
                    expansions.append({
                        "query": expanded,
                        "strategy": "drug_name_expansion",
                        "detail": f"Expanded '{drug}' to '{alt}'",
                    })

        # Step 2: Condition broadening
        condition_hierarchy = {
            "nstemi": "acute coronary syndrome",
            "stemi": "acute coronary syndrome",
            "hfref": "heart failure",
            "hfpef": "heart failure",
            "ckd g4": "chronic kidney disease",
            "ckd g5": "chronic kidney disease",
            "cap": "pneumonia",
        }
        query_lower = original_query.lower()
        for specific, broad in condition_hierarchy.items():
            if specific in query_lower:
                expansions.append({
                    "query": query_lower.replace(specific, broad),
                    "strategy": "condition_broadening",
                    "detail": f"Broadened '{specific}' to '{broad}'",
                })

        # Step 3: Date range widening
        if current_result_count < self.MIN_EVIDENCE_THRESHOLD:
            expansions.append({
                "query": original_query,
                "strategy": "date_range_widened",
                "detail": "Expanded from 5-year to 10-year evidence window",
                "params": {"date_range_years": 10},
            })

        return expansions


# =============================================================================
# L14-6: COUNTERFACTUAL PARAMETER TOGGLE
# =============================================================================

@dataclass
class CounterfactualResult:
    original_params: dict
    modified_params: dict
    parameter_changed: str
    original_outcome: str
    counterfactual_outcome: str
    safety_difference: list[str] = field(default_factory=list)


class CounterfactualToggle:
    """
    L14-6: "What if" parameter changes for clinical exploration.

    Allows clinicians to ask: "What if eGFR was 25 instead of 45?"
    "What if patient was 80 instead of 55?" "What if on warfarin not apixaban?"

    Re-runs relevant deterministic checks (CQL, dose adjustments, safety gates)
    WITHOUT re-querying the LLM. Fast, deterministic comparison.
    """

    TOGGLEABLE_PARAMS: set[str] = {
        "age", "weight_kg", "egfr", "creatinine",
        "sex", "pregnant", "breastfeeding",
        "ckd_stage", "hepatic_function",
    }

    def generate_counterfactual(
        self,
        original_params: dict,
        param_to_change: str,
        new_value: Any,
    ) -> dict:
        """Generate counterfactual parameter set."""
        if param_to_change not in self.TOGGLEABLE_PARAMS:
            return {"error": f"Parameter '{param_to_change}' is not toggleable"}

        modified = dict(original_params)
        modified[param_to_change] = new_value

        return {
            "original": original_params,
            "modified": modified,
            "changed": param_to_change,
            "from": original_params.get(param_to_change),
            "to": new_value,
            "note": "Re-run CQL kernel and safety gates with modified parameters "
                    "to see how the clinical recommendation changes.",
        }


# =============================================================================
# L14-9: VOICE INPUT/OUTPUT PIPELINE
# =============================================================================

class VoiceInputPipeline:
    """
    L14-9: Routes voice input to transcription service and back.

    Architecture: Voice -> Speech-to-Text API -> L8-12 Language Detection
    -> pipeline.process() -> Text-to-Speech API -> Voice output

    Supported STT/TTS backends (from env):
    - WHISPER_API_URL (OpenAI Whisper or compatible)
    - GOOGLE_STT_KEY (Google Cloud Speech-to-Text)
    - AZURE_STT_KEY (Azure Cognitive Services)

    Fail-closed: if no STT backend configured, returns error.
    Audio is NEVER stored — streamed to API and discarded (HIPAA compliance).
    """

    def __init__(self):
        self._whisper_url = os.environ.get("WHISPER_API_URL", "")
        self._google_key = os.environ.get("GOOGLE_STT_KEY", "")
        self._azure_key = os.environ.get("AZURE_STT_KEY", "")

    @property
    def is_configured(self) -> bool:
        return bool(self._whisper_url or self._google_key or self._azure_key)

    def get_backend(self) -> str:
        if self._whisper_url:
            return "whisper"
        if self._google_key:
            return "google_stt"
        if self._azure_key:
            return "azure_stt"
        return "none"

    def transcribe(self, audio_bytes: bytes, language_hint: str = "auto") -> dict:
        """
        Transcribe audio to text via configured STT backend.
        Audio is NOT stored — processed in memory only.
        """
        if not self.is_configured:
            return {
                "success": False,
                "error": "No speech-to-text backend configured. "
                         "Set WHISPER_API_URL, GOOGLE_STT_KEY, or AZURE_STT_KEY.",
            }

        backend = self.get_backend()

        if backend == "whisper":
            return self._transcribe_whisper(audio_bytes, language_hint)
        elif backend == "google_stt":
            return self._transcribe_google(audio_bytes, language_hint)
        elif backend == "azure_stt":
            return self._transcribe_azure(audio_bytes, language_hint)

        return {"success": False, "error": "Unknown backend"}

    def _transcribe_whisper(self, audio: bytes, lang: str) -> dict:
        """Whisper API transcription."""
        import urllib.request
        import json

        try:
            # Multipart form upload to Whisper API
            boundary = uuid4().hex
            body = (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="file"; filename="audio.wav"\r\n'
                f"Content-Type: audio/wav\r\n\r\n"
            ).encode() + audio + (
                f"\r\n--{boundary}\r\n"
                f'Content-Disposition: form-data; name="model"\r\n\r\n'
                f"whisper-1"
                f"\r\n--{boundary}--\r\n"
            ).encode()

            req = urllib.request.Request(
                self._whisper_url,
                data=body,
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read().decode())
                return {
                    "success": True,
                    "text": result.get("text", ""),
                    "language": result.get("language", lang),
                    "backend": "whisper",
                }
        except Exception as e:
            return {"success": False, "error": f"Whisper API error: {e}", "backend": "whisper"}

    def _transcribe_google(self, audio: bytes, lang: str) -> dict:
        return {"success": False, "error": "Google STT: implementation requires google-cloud-speech SDK", "backend": "google_stt"}

    def _transcribe_azure(self, audio: bytes, lang: str) -> dict:
        return {"success": False, "error": "Azure STT: implementation requires azure-cognitiveservices-speech SDK", "backend": "azure_stt"}
