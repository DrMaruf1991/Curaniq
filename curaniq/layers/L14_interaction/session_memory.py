"""
CURANIQ - L14-8 Clinical Session Memory + L14-3 Assumption Ledger
Multi-turn clinical conversation state.

Copy to: curaniq/layers/L14_interaction/session_memory.py

L14-8: Session Memory carries patient context, detected drugs, previous
queries, and accumulated evidence across conversation turns.
Not chat history — clinical state accumulation.

L14-3: Assumption Ledger tracks every implicit assumption made when
data is missing. "Assuming normal hepatic function" when no liver labs.
"Assuming adult patient" when age not provided. Shown to clinician
so they can correct wrong assumptions.

No hardcoded assumptions. What gets assumed depends on what data
is missing in the patient context vs what the query requires.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4


# ─────────────────────────────────────────────────────────────────
# L14-3: ASSUMPTION LEDGER
# ─────────────────────────────────────────────────────────────────

@dataclass
class ClinicalAssumption:
    """A single implicit assumption the system made."""
    assumption_id: str = field(default_factory=lambda: str(uuid4())[:8])
    category: str = ""          # 'patient', 'renal', 'hepatic', 'age', 'pregnancy', 'jurisdiction'
    description: str = ""       # Human-readable: "Assuming normal hepatic function"
    data_missing: str = ""      # What data was absent: "hepatic_function"
    default_used: str = ""      # What default was applied: "normal"
    clinical_impact: str = ""   # Why it matters: "Hepatic dose adjustments not applied"
    correctable: bool = True    # Can the clinician correct this?


class AssumptionLedger:
    """
    L14-3: Tracks every assumption made during query processing.
    
    When patient context is incomplete, the pipeline makes assumptions.
    Every assumption is logged, shown to the clinician, and correctable.
    
    This is not a hardcoded list of assumptions. It dynamically detects
    what's missing based on what the query REQUIRES.
    """

    def __init__(self):
        self._assumptions: list[ClinicalAssumption] = []

    def clear(self):
        """Clear assumptions for new query."""
        self._assumptions = []

    def assess_missing_context(
        self,
        query_text: str,
        drugs: Optional[list[str]] = None,
        patient_context: Optional[dict] = None,
        drugs_mentioned: Optional[list[str]] = None,
    ) -> list[ClinicalAssumption]:
        """
        Detect missing context based on what the query needs.
        Not a static checklist — adapts to query type.
        """
        self.clear()
        drugs = drugs if drugs is not None else (drugs_mentioned or [])
        if patient_context is None:
            ctx = {}
        elif hasattr(patient_context, "model_dump"):
            ctx = patient_context.model_dump()
        elif isinstance(patient_context, dict):
            ctx = patient_context
        else:
            ctx = {}
        query_lower = query_text.lower()

        # Renal function — needed for ANY drug dosing query
        if drugs and not ctx.get("renal"):
            self._add("renal",
                "Assuming normal renal function (no eGFR/CrCl provided)",
                "renal_function", "normal",
                "Renal dose adjustments NOT applied. Provide eGFR for accurate dosing.")

        # Hepatic function — needed for hepatically-cleared drugs
        hepatic_drugs = {"methotrexate", "paracetamol", "valproate", "statins",
                         "atorvastatin", "simvastatin", "carbamazepine"}
        if any(d in hepatic_drugs for d in [dl.lower() for dl in drugs]):
            if not ctx.get("hepatic"):
                self._add("hepatic",
                    "Assuming normal hepatic function",
                    "hepatic_function", "normal",
                    "Hepatic dose adjustments NOT applied. Provide LFTs for liver-cleared drugs.")

        # Age — needed for pediatric/geriatric dosing
        if not ctx.get("age_years"):
            self._add("age",
                "Assuming adult patient (18-65 years)",
                "age_years", "adult (18-65)",
                "Pediatric/geriatric dose adjustments NOT applied.")

        # Weight — needed for weight-based dosing
        weight_signals = {"mg/kg", "weight", "bsa", "pediatric", "child", "dose"}
        if any(s in query_lower for s in weight_signals):
            if not ctx.get("weight_kg"):
                self._add("weight",
                    "Assuming 70kg adult weight",
                    "weight_kg", "70kg",
                    "Weight-based doses calculated using assumed 70kg.")

        # Pregnancy — needed for pregnancy category drugs
        if not ctx.get("is_pregnant") and not ctx.get("sex_at_birth"):
            self._add("pregnancy",
                "Pregnancy status unknown",
                "is_pregnant", "unknown",
                "Pregnancy safety classification NOT evaluated. Confirm pregnancy status.")

        # Allergies — always relevant for drug queries
        if drugs and not ctx.get("allergies"):
            self._add("allergies",
                "No allergy information provided",
                "allergies", "none reported",
                "Cross-reactivity checks LIMITED. Provide allergy history.")

        # Current medications — needed for DDI checking
        if drugs and not ctx.get("active_medications"):
            self._add("medications",
                "No current medications listed",
                "active_medications", "none reported",
                "Drug-drug interaction checks LIMITED to query drugs only.")

        # Jurisdiction — affects guideline selection
        if not ctx.get("jurisdiction"):
            self._add("jurisdiction",
                "Using international (WHO) guidelines as default",
                "jurisdiction", "INT/WHO",
                "Local guidelines may differ. Specify jurisdiction for local protocols.")

        return self._assumptions

    def _add(self, category, description, data_missing, default_used, clinical_impact):
        self._assumptions.append(ClinicalAssumption(
            category=category,
            description=description,
            data_missing=data_missing,
            default_used=default_used,
            clinical_impact=clinical_impact,
        ))

    @property
    def assumptions(self) -> list[ClinicalAssumption]:
        return list(self._assumptions)

    @property
    def count(self) -> int:
        return len(self._assumptions)

    def format_for_clinician(self) -> str:
        """Format assumptions for display in evidence card."""
        if not self._assumptions:
            return ""
        lines = ["ASSUMPTIONS MADE (correct if wrong):"]
        for a in self._assumptions:
            lines.append(f"  - {a.description}")
            lines.append(f"    Impact: {a.clinical_impact}")
        return "\n".join(lines)

    def format_for_llm_context(self) -> str:
        """Format for LLM system prompt — so it acknowledges assumptions."""
        if not self._assumptions:
            return "All required patient context is available."
        lines = ["MISSING PATIENT DATA (assumptions applied):"]
        for a in self._assumptions:
            lines.append(f"- {a.data_missing}: assumed {a.default_used}")
        lines.append("INSTRUCTION: Mention these assumptions in your response.")
        return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────
# L14-8: CLINICAL SESSION MEMORY
# ─────────────────────────────────────────────────────────────────

@dataclass
class SessionTurn:
    """A single query-response turn in a clinical session."""
    turn_id: str = field(default_factory=lambda: str(uuid4())[:8])
    query_text: str = ""
    drugs_detected: list[str] = field(default_factory=list)
    foods_detected: list[str] = field(default_factory=list)
    assumptions_made: int = 0
    evidence_sources: int = 0
    was_refused: bool = False
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class ClinicalSessionMemory:
    """
    L14-8: Maintains clinical state across conversation turns.
    
    Not chat history — clinical context accumulation.
    Each turn adds to the patient's clinical picture:
    - Drugs mentioned accumulate (don't forget previous drugs)
    - Patient context updates merge (new lab results override old)
    - Assumptions carry forward until corrected
    - Evidence sources accumulate for the session
    
    Session bound to a session_id. Stored in memory per session.
    Production: persist to database for session recovery.
    """

    def __init__(self):
        # session_id -> session state
        self._sessions: dict[str, dict] = {}

    def get_or_create(self, session_id: Optional[str] = None) -> str:
        """Get existing session or create new one. Returns session_id."""
        if not session_id:
            session_id = str(uuid4())

        if session_id not in self._sessions:
            self._sessions[session_id] = {
                "session_id": session_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "accumulated_drugs": [],
                "accumulated_foods": [],
                "accumulated_conditions": [],
                "patient_context": {},
                "turns": [],
                "assumption_ledger": AssumptionLedger(),
                "total_evidence_sources": 0,
            }

        return session_id

    def record_turn(
        self,
        session_id: str,
        query_text: str,
        drugs: list[str],
        foods: list[str],
        evidence_count: int = 0,
        was_refused: bool = False,
    ) -> None:
        """Record a completed query turn."""
        if session_id not in self._sessions:
            self.get_or_create(session_id)

        session = self._sessions[session_id]

        # Accumulate drugs (don't lose previous turns)
        for d in drugs:
            if d not in session["accumulated_drugs"]:
                session["accumulated_drugs"].append(d)

        # Accumulate foods
        for f in foods:
            if f not in session["accumulated_foods"]:
                session["accumulated_foods"].append(f)

        session["total_evidence_sources"] += evidence_count

        # Record turn
        session["turns"].append(SessionTurn(
            query_text=query_text,
            drugs_detected=drugs,
            foods_detected=foods,
            evidence_sources=evidence_count,
            was_refused=was_refused,
        ))

    def update_patient_context(self, session_id: str, updates: dict) -> None:
        """
        Merge new patient context into session.
        New values override old (e.g., new lab result replaces previous).
        """
        if session_id not in self._sessions:
            return
        ctx = self._sessions[session_id]["patient_context"]
        for key, value in updates.items():
            if value is not None:
                ctx[key] = value

    def get_accumulated_drugs(self, session_id: str) -> list[str]:
        """All drugs mentioned across all turns in this session."""
        if session_id not in self._sessions:
            return []
        return list(self._sessions[session_id]["accumulated_drugs"])

    def get_patient_context(self, session_id: str) -> dict:
        """Accumulated patient context across turns."""
        if session_id not in self._sessions:
            return {}
        return dict(self._sessions[session_id]["patient_context"])

    def get_turn_count(self, session_id: str) -> int:
        """Number of completed turns."""
        if session_id not in self._sessions:
            return 0
        return len(self._sessions[session_id]["turns"])

    def get_session_summary(self, session_id: str) -> dict:
        """Summary of session state for LLM context."""
        if session_id not in self._sessions:
            return {}
        s = self._sessions[session_id]
        return {
            "session_id": session_id,
            "turns_completed": len(s["turns"]),
            "accumulated_drugs": s["accumulated_drugs"],
            "accumulated_foods": s["accumulated_foods"],
            "patient_context": s["patient_context"],
            "total_evidence_sources": s["total_evidence_sources"],
        }

    def build_session_context_for_llm(self, session_id: str) -> str:
        """
        Build session context string for LLM system prompt.
        Gives the LLM awareness of previous turns without full chat history.
        """
        if session_id not in self._sessions:
            return ""

        s = self._sessions[session_id]
        if not s["turns"]:
            return ""

        lines = [f"SESSION CONTEXT ({len(s['turns'])} previous turns):"]

        if s["accumulated_drugs"]:
            lines.append(f"Drugs discussed: {', '.join(s['accumulated_drugs'])}")
        if s["patient_context"]:
            ctx_parts = [f"{k}: {v}" for k, v in s["patient_context"].items()]
            lines.append(f"Patient: {' | '.join(ctx_parts)}")

        # Last 3 queries for recency context
        recent = s["turns"][-3:]
        lines.append("Recent queries:")
        for t in recent:
            status = "REFUSED" if t.was_refused else f"{t.evidence_sources} sources"
            lines.append(f"  - \"{t.query_text[:80]}\" ({status})")

        return "\n".join(lines)

    def cleanup_expired(self, max_age_hours: float = 24.0) -> int:
        """Remove sessions older than max_age_hours. Returns count removed."""
        now = datetime.now(timezone.utc)
        expired = []
        for sid, session in self._sessions.items():
            created = datetime.fromisoformat(session["created_at"])
            if (now - created).total_seconds() > max_age_hours * 3600:
                expired.append(sid)
        for sid in expired:
            del self._sessions[sid]
        return len(expired)
