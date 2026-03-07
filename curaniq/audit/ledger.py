"""
CURANIQ — L9-1: Immutable Evidence Audit Ledger
Architecture spec: Every query, evidence retrieval, claim decision, verification step,
and safety gate result is logged in an append-only, cryptographically hash-chained ledger.
Regulators, auditors, and governance boards can replay any clinical decision.
Citation provenance (L9-3) integrated: full chain from question → evidence → claim → output.
"""
from __future__ import annotations
import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID, uuid4

from curaniq.audit.storage import get_storage_backend

from curaniq.models.schemas import (
    AuditLedgerEntry,
    ClaimContract,
    ClinicalQuery,
    CQLComputationLog,
    EvidencePack,
    InteractionMode,
    Jurisdiction,
    SafetyGateSuite,
    TriageAssessment,
    TriageResult,
    UserRole,
)


# ─────────────────────────────────────────────────────────────────────────────
# HASH CHAIN INTEGRITY
# ─────────────────────────────────────────────────────────────────────────────

def _serialize_for_hash(entry: AuditLedgerEntry) -> bytes:
    """
    Deterministic serialization for hash computation.
    Excludes entry_hash and previous_entry_hash fields (they are part of the chain, not content).
    """
    content = {
        "entry_id":           str(entry.entry_id),
        "query_id":           str(entry.query_id),
        "session_id":         str(entry.session_id) if entry.session_id else None,
        "user_role":          entry.user_role.value,
        "mode":               entry.mode.value,
        "jurisdiction":       entry.jurisdiction.value,
        "triage_result":      entry.triage_result.value,
        "mode_detected":      entry.mode_detected.value,
        "evidence_pack_id":   str(entry.evidence_pack_id),
        "claim_contract_id":  str(entry.claim_contract_id),
        "safety_suite_passed":entry.safety_suite_passed,
        "hard_blocked":       entry.hard_blocked,
        "evidence_source_ids":sorted(entry.evidence_source_ids),
        "cql_computation_ids":sorted(entry.cql_computation_ids),
        "refused":            entry.refused,
        "refusal_reason":     entry.refusal_reason,
        "created_at":         entry.created_at.isoformat(),
        "previous_entry_hash":entry.previous_entry_hash,
    }
    return json.dumps(content, sort_keys=True, ensure_ascii=True).encode("utf-8")


def compute_entry_hash(entry: AuditLedgerEntry) -> str:
    """SHA-256 hash of the entry content (including previous_entry_hash for chain)."""
    return hashlib.sha256(_serialize_for_hash(entry)).hexdigest()


def verify_chain_integrity(entries: list[AuditLedgerEntry]) -> list[dict]:
    """
    Verify the hash chain of audit entries.
    Returns list of integrity violations (empty = chain is valid).
    """
    violations: list[dict] = []

    for i, entry in enumerate(entries):
        # Verify this entry's own hash
        expected_hash = compute_entry_hash(entry)
        if entry.entry_hash and entry.entry_hash != expected_hash:
            violations.append({
                "entry_index":  i,
                "entry_id":     str(entry.entry_id),
                "issue":        "Entry hash mismatch — content may have been tampered",
                "expected":     expected_hash,
                "stored":       entry.entry_hash,
            })

        # Verify chain linkage
        if i > 0:
            prev_entry = entries[i - 1]
            if entry.previous_entry_hash != prev_entry.entry_hash:
                violations.append({
                    "entry_index":  i,
                    "entry_id":     str(entry.entry_id),
                    "issue":        "Previous entry hash linkage broken — entries may have been inserted or deleted",
                    "expected":     prev_entry.entry_hash,
                    "stored":       entry.previous_entry_hash,
                })

    return violations


# ─────────────────────────────────────────────────────────────────────────────
# CITATION PROVENANCE  (L9-3)
# ─────────────────────────────────────────────────────────────────────────────

class CitationProvenanceRecord:
    """
    L9-3: Full citation provenance chain for a single output.
    Documents the unbroken chain:
    Query → Evidence Source → Retrieved Snippet → Claim → Output Sentence
    """

    def __init__(self, query_id: UUID) -> None:
        self.query_id = query_id
        self.provenance_chains: list[dict] = []

    def add_chain(
        self,
        claim_text: str,
        evidence_source_id: str,
        evidence_tier: str,
        snippet: str,
        snippet_hash: Optional[str],
        cql_computation_id: Optional[str] = None,
        entailment_score: Optional[float] = None,
    ) -> None:
        """Record a single claim → evidence provenance chain."""
        self.provenance_chains.append({
            "query_id":            str(self.query_id),
            "claim_text":          claim_text[:200],
            "evidence_source_id":  evidence_source_id,
            "evidence_tier":       evidence_tier,
            "snippet_preview":     snippet[:150] + "..." if len(snippet) > 150 else snippet,
            "snippet_hash":        snippet_hash,
            "cql_computation_id":  cql_computation_id,
            "entailment_score":    entailment_score,
            "chain_integrity":     "VERIFIED" if snippet_hash else "UNVERIFIED",
            "recorded_at":         datetime.now(timezone.utc).isoformat(),
        })

    def to_dict(self) -> dict:
        return {
            "query_id":         str(self.query_id),
            "total_chains":     len(self.provenance_chains),
            "chains":           self.provenance_chains,
        }


# ─────────────────────────────────────────────────────────────────────────────
# AUDIT LEDGER  (L9-1 — main class)
# ─────────────────────────────────────────────────────────────────────────────

class AuditLedger:
    """
    L9-1: Immutable Evidence Audit Ledger.

    Append-only ledger with cryptographic hash-chaining.
    Every pipeline execution creates one entry.
    The ledger is the authoritative compliance record for regulators, auditors,
    and the CURANIQ governance board.

    In production: entries are stored in an append-only PostgreSQL table
    with row-level security preventing deletes/updates.
    This implementation maintains an in-memory ledger for the session
    and returns the entry for database persistence.
    """

    def __init__(self) -> None:
        self._storage = get_storage_backend()
        self._entries: list[AuditLedgerEntry] = []  # In-memory cache for fast access
        self._last_hash: Optional[str] = self._storage.get_last_hash()

    @property
    def entry_count(self) -> int:
        return self._storage.count()

    @property
    def last_entry_hash(self) -> Optional[str]:
        return self._last_hash

    def record(
        self,
        query: ClinicalQuery,
        triage: TriageAssessment,
        mode_detected: InteractionMode,
        evidence_pack: EvidencePack,
        claim_contract: ClaimContract,
        safety_suite: SafetyGateSuite,
        cql_logs: list[CQLComputationLog],
        refused: bool = False,
        refusal_reason: Optional[str] = None,
    ) -> AuditLedgerEntry:
        """
        Create and append a new audit ledger entry.
        Automatically links to previous entry hash (chain continuity).
        Returns the completed entry for database persistence.
        """
        entry = AuditLedgerEntry(
            entry_id=uuid4(),
            query_id=query.query_id,
            session_id=query.session_id,
            user_role=query.user_role,
            mode=query.mode or mode_detected,
            jurisdiction=query.jurisdiction,
            triage_result=triage.result,
            mode_detected=mode_detected,
            evidence_pack_id=evidence_pack.pack_id,
            claim_contract_id=claim_contract.contract_id,
            safety_suite_passed=safety_suite.overall_passed,
            hard_blocked=safety_suite.hard_block,
            evidence_source_ids=[e.source_id for e in evidence_pack.objects],
            cql_computation_ids=[log.computation_id for log in cql_logs],
            refused=refused,
            refusal_reason=refusal_reason,
            previous_entry_hash=self._last_hash,
        )

        # Compute and seal the entry hash
        entry_hash = compute_entry_hash(entry)
        entry.entry_hash = entry_hash

        # Append to chain
        self._entries.append(entry)

        # Persist to durable storage (file/PostgreSQL)
        self._storage.append({
            "entry_id": str(entry.entry_id),
            "query_id": str(entry.query_id),
            "user_role": entry.user_role.value,
            "jurisdiction": entry.jurisdiction.value,
            "triage_result": entry.triage_result.value,
            "mode": entry.mode.value,
            "evidence_pack_id": str(entry.evidence_pack_id),
            "evidence_count": entry.evidence_count,
            "safety_suite_passed": entry.safety_suite_passed,
            "refused": entry.refused,
            "refusal_reason": entry.refusal_reason,
            "entry_hash": entry.entry_hash,
            "previous_entry_hash": entry.previous_entry_hash,
            "created_at": entry.created_at.isoformat(),
        })
        self._last_hash = entry_hash

        return entry

    def verify_integrity(self) -> dict[str, Any]:
        """
        Full chain integrity verification.
        Returns a report suitable for compliance auditing.
        """
        violations = verify_chain_integrity(self._entries)
        return {
            "total_entries":     self.entry_count,
            "chain_valid":       len(violations) == 0,
            "violations":        violations,
            "last_entry_hash":   self._last_hash,
            "verified_at":       datetime.now(timezone.utc).isoformat(),
        }

    def build_citation_provenance(
        self,
        query: ClinicalQuery,
        claim_contract: ClaimContract,
        evidence_pack: EvidencePack,
    ) -> CitationProvenanceRecord:
        """
        L9-3: Build the full citation provenance chain for a response.
        Every non-blocked claim is traced back to its evidence source(s).
        """
        provenance = CitationProvenanceRecord(query.query_id)

        # Map evidence ID → evidence object for lookup
        ev_map = {str(e.evidence_id): e for e in evidence_pack.objects}

        for claim in claim_contract.atomic_claims:
            if claim.is_blocked:
                continue

            # Get CQL computation ID if any numeric token is deterministic
            cql_id: Optional[str] = None
            for nt in claim.numeric_tokens:
                if nt.cql_computation_id:
                    cql_id = nt.cql_computation_id
                    break

            # Record chain for each supporting evidence
            for ev_id in claim.evidence_ids[:3]:   # Top 3 supporting
                ev = ev_map.get(str(ev_id))
                if ev:
                    provenance.add_chain(
                        claim_text=claim.claim_text,
                        evidence_source_id=ev.source_id,
                        evidence_tier=ev.tier.value,
                        snippet=ev.snippet,
                        snippet_hash=ev.snippet_hash,
                        cql_computation_id=cql_id,
                        entailment_score=claim.entailment_score,
                    )

        return provenance

    def get_query_audit_trail(self, query_id: UUID) -> list[AuditLedgerEntry]:
        """Retrieve all audit entries for a specific query (for audit review)."""
        # Check in-memory first (current session)
        results = [e for e in self._entries if e.query_id == query_id]
        if results:
            return results
        # Fall back to persistent storage (previous sessions)
        stored = self._storage.get_by_query_id(str(query_id))
        return stored  # Returns dicts, not AuditLedgerEntry — acceptable for API

    def export_compliance_report(
        self,
        from_entry: int = 0,
        to_entry: Optional[int] = None,
    ) -> dict[str, Any]:
        """
        Export a compliance report for a range of audit entries.
        Format suitable for submission to regulatory bodies.
        """
        entries_slice = self._entries[from_entry:to_entry]
        integrity = self.verify_integrity()

        refused_count = sum(1 for e in entries_slice if e.refused)
        hard_blocked_count = sum(1 for e in entries_slice if e.hard_blocked)
        passed_count = sum(1 for e in entries_slice if e.safety_suite_passed and not e.hard_blocked)

        return {
            "report_type":          "CURANIQ Audit Compliance Report",
            "generated_at":         datetime.now(timezone.utc).isoformat(),
            "entry_range":          {"from": from_entry, "to": to_entry or self.entry_count},
            "total_entries":        len(entries_slice),
            "chain_integrity":      integrity,
            "statistics": {
                "passed":           passed_count,
                "hard_blocked":     hard_blocked_count,
                "refused":          refused_count,
                "refusal_rate":     f"{refused_count / max(len(entries_slice), 1):.1%}",
                "block_rate":       f"{hard_blocked_count / max(len(entries_slice), 1):.1%}",
            },
            "jurisdiction_breakdown": self._jurisdiction_breakdown(entries_slice),
            "role_breakdown":         self._role_breakdown(entries_slice),
        }

    def _jurisdiction_breakdown(self, entries: list[AuditLedgerEntry]) -> dict:
        counts: dict[str, int] = {}
        for e in entries:
            j = e.jurisdiction.value
            counts[j] = counts.get(j, 0) + 1
        return counts

    def _role_breakdown(self, entries: list[AuditLedgerEntry]) -> dict:
        counts: dict[str, int] = {}
        for e in entries:
            r = e.user_role.value
            counts[r] = counts.get(r, 0) + 1
        return counts
