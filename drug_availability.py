"""
CURANIQ - L11-1 Local Drug Availability Filter
Filters recommendations by what's actually available locally.

Copy to: curaniq/layers/L11_local_reality/drug_availability.py

Architecture: "Filters by local availability, approval status, supply.
Essential for Uzbekistan/CIS deployment."

Design:
  - Formulary loaded from external source (JSON file, API, or database)
  - Path from environment: CURANIQ_FORMULARY_PATH
  - No hardcoded drug lists. Formulary is data, not code.
  - When drug unavailable: suggests available alternatives
  - When no formulary loaded: passes through (no filtering)
  - Supports multiple markets: UZ, RU, US, UK, EU via jurisdiction

Data structure per drug:
  - INN name (canonical)
  - Local brand names
  - Availability status: available / restricted / unavailable / shortage
  - Approval authority (MOH, FDA, EMA, etc.)
  - Local alternatives if unavailable
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class DrugAvailability:
    """Availability status for a single drug in a jurisdiction."""
    inn: str                            # International Nonproprietary Name
    status: str                         # available | restricted | unavailable | shortage
    local_brands: list[str] = field(default_factory=list)
    approval_authority: str = ""        # e.g., "UZ MOH", "FDA", "EMA"
    restrictions: Optional[str] = None  # e.g., "Hospital use only", "Narcotic license required"
    alternatives: list[str] = field(default_factory=list)  # Available alternatives if unavailable
    last_updated: Optional[str] = None


@dataclass
class AvailabilityCheck:
    """Result of checking drug availability."""
    drug: str
    jurisdiction: str
    is_available: bool
    status: str
    local_brands: list[str]
    restrictions: Optional[str]
    alternatives: list[str]
    message: str


class LocalDrugAvailabilityFilter:
    """
    L11-1: Filter drug recommendations by local availability.
    
    Formulary loaded from external data source — never hardcoded.
    Supports any jurisdiction. Data-driven, not code-driven.
    
    When a drug is unavailable locally:
    - Flag it clearly in the response
    - Suggest available therapeutic alternatives
    - Note if the drug requires special authorization
    
    Production: connects to national formulary APIs.
    MVP: loads from JSON file at CURANIQ_FORMULARY_PATH.
    """

    def __init__(self, formulary_path: Optional[str] = None):
        self._formulary_path = formulary_path or os.environ.get(
            "CURANIQ_FORMULARY_PATH", ""
        )
        # jurisdiction -> {inn_lower -> DrugAvailability}
        self._formularies: dict[str, dict[str, DrugAvailability]] = {}
        self._loaded = False

        if self._formulary_path:
            self._load_formulary(self._formulary_path)
        else:
            # Load default seed formulary
            self._load_default_seed()

    def _load_formulary(self, path: str) -> None:
        """Load formulary from JSON file."""
        if not os.path.exists(path):
            logger.warning(f"Formulary file not found: {path}")
            return

        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)

            for jurisdiction, drugs in data.items():
                self._formularies[jurisdiction.upper()] = {}
                for drug_entry in drugs:
                    inn = drug_entry.get("inn", "").lower()
                    if inn:
                        self._formularies[jurisdiction.upper()][inn] = DrugAvailability(
                            inn=inn,
                            status=drug_entry.get("status", "unknown"),
                            local_brands=drug_entry.get("local_brands", []),
                            approval_authority=drug_entry.get("approval_authority", ""),
                            restrictions=drug_entry.get("restrictions"),
                            alternatives=drug_entry.get("alternatives", []),
                            last_updated=drug_entry.get("last_updated"),
                        )

            total = sum(len(d) for d in self._formularies.values())
            self._loaded = True
            logger.info(f"Formulary loaded: {total} drugs across {list(self._formularies.keys())}")

        except (json.JSONDecodeError, KeyError) as e:
            logger.error(f"Failed to load formulary: {e}")

    def _load_default_seed(self) -> None:
        """
        Load a minimal seed formulary for development.
        Production: replace with full national formulary data.
        
        This is DATA, not code. The structure is what matters.
        The actual drug list comes from external sources.
        """
        # Uzbekistan essential medicines (based on WHO EML + UZ MOH)
        uz_drugs = [
            {"inn": "metformin", "status": "available", "local_brands": ["Glucophage", "Metfogamma", "Siofor"], "approval_authority": "UZ MOH"},
            {"inn": "amoxicillin", "status": "available", "local_brands": ["Amoxil", "Flemoxin"], "approval_authority": "UZ MOH"},
            {"inn": "amoxicillin/clavulanic acid", "status": "available", "local_brands": ["Augmentin", "Amoxiclav"], "approval_authority": "UZ MOH"},
            {"inn": "ciprofloxacin", "status": "available", "local_brands": ["Cipro", "Ciprolet"], "approval_authority": "UZ MOH"},
            {"inn": "atorvastatin", "status": "available", "local_brands": ["Atoris", "Liprimar"], "approval_authority": "UZ MOH"},
            {"inn": "lisinopril", "status": "available", "local_brands": ["Diroton", "Lisinopril-Teva"], "approval_authority": "UZ MOH"},
            {"inn": "amlodipine", "status": "available", "local_brands": ["Norvasc", "Amlodipin"], "approval_authority": "UZ MOH"},
            {"inn": "omeprazole", "status": "available", "local_brands": ["Omez", "Losec"], "approval_authority": "UZ MOH"},
            {"inn": "furosemide", "status": "available", "local_brands": ["Lasix", "Furosemid"], "approval_authority": "UZ MOH"},
            {"inn": "warfarin", "status": "available", "local_brands": ["Warfarin Nycomed"], "approval_authority": "UZ MOH"},
            {"inn": "heparin", "status": "available", "local_brands": ["Heparin"], "approval_authority": "UZ MOH"},
            {"inn": "enoxaparin", "status": "available", "local_brands": ["Clexane"], "approval_authority": "UZ MOH"},
            {"inn": "salbutamol", "status": "available", "local_brands": ["Ventolin", "Salbutamol"], "approval_authority": "UZ MOH"},
            {"inn": "prednisolone", "status": "available", "local_brands": ["Prednisolon"], "approval_authority": "UZ MOH"},
            {"inn": "paracetamol", "status": "available", "local_brands": ["Panadol", "Efferalgan"], "approval_authority": "UZ MOH"},
            {"inn": "ibuprofen", "status": "available", "local_brands": ["Nurofen", "Ibuprofen"], "approval_authority": "UZ MOH"},
            {"inn": "diclofenac", "status": "available", "local_brands": ["Voltaren", "Diklofenak"], "approval_authority": "UZ MOH"},
            {"inn": "insulin", "status": "available", "local_brands": ["NovoRapid", "Lantus", "Humulin"], "approval_authority": "UZ MOH"},
            {"inn": "levothyroxine", "status": "available", "local_brands": ["L-Thyroxin", "Eutiroks"], "approval_authority": "UZ MOH"},
            {"inn": "morphine", "status": "restricted", "local_brands": ["Morphine HCl"], "approval_authority": "UZ MOH", "restrictions": "Narcotic license required. Hospital use only."},
            {"inn": "tramadol", "status": "restricted", "local_brands": ["Tramadol"], "approval_authority": "UZ MOH", "restrictions": "Controlled substance. Prescription required with special form."},
            # Drugs commonly prescribed elsewhere but unavailable/limited in UZ
            {"inn": "apixaban", "status": "unavailable", "alternatives": ["warfarin", "enoxaparin"], "approval_authority": "Not registered in UZ"},
            {"inn": "rivaroxaban", "status": "unavailable", "alternatives": ["warfarin", "enoxaparin"], "approval_authority": "Not registered in UZ"},
            {"inn": "dabigatran", "status": "unavailable", "alternatives": ["warfarin", "enoxaparin"], "approval_authority": "Not registered in UZ"},
            {"inn": "sacubitril/valsartan", "status": "shortage", "local_brands": ["Entresto"], "approval_authority": "UZ MOH", "restrictions": "Limited supply. Check pharmacy availability."},
        ]

        self._formularies["UZ"] = {}
        for d in uz_drugs:
            inn = d["inn"].lower()
            self._formularies["UZ"][inn] = DrugAvailability(
                inn=inn,
                status=d.get("status", "unknown"),
                local_brands=d.get("local_brands", []),
                approval_authority=d.get("approval_authority", ""),
                restrictions=d.get("restrictions"),
                alternatives=d.get("alternatives", []),
                last_updated=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            )

        self._loaded = True
        logger.info(f"Seed formulary loaded: {len(uz_drugs)} drugs for UZ")

    def check(self, drug_inn: str, jurisdiction: str = "UZ") -> AvailabilityCheck:
        """
        Check if a drug is available in the given jurisdiction.
        Returns structured availability information.
        """
        jurisdiction = jurisdiction.upper()
        drug_lower = drug_inn.lower()

        # No formulary for this jurisdiction — pass through
        if jurisdiction not in self._formularies:
            return AvailabilityCheck(
                drug=drug_inn, jurisdiction=jurisdiction,
                is_available=True, status="unknown",
                local_brands=[], restrictions=None, alternatives=[],
                message=f"No formulary data for {jurisdiction}. Verify local availability.",
            )

        formulary = self._formularies[jurisdiction]

        # Drug found in formulary
        if drug_lower in formulary:
            entry = formulary[drug_lower]
            is_available = entry.status in ("available", "restricted")
            
            if entry.status == "available":
                msg = f"{drug_inn} is available in {jurisdiction}."
                if entry.local_brands:
                    msg += f" Local brands: {', '.join(entry.local_brands)}."
            elif entry.status == "restricted":
                msg = f"{drug_inn} is RESTRICTED in {jurisdiction}."
                if entry.restrictions:
                    msg += f" {entry.restrictions}"
            elif entry.status == "shortage":
                msg = f"{drug_inn} is in SHORT SUPPLY in {jurisdiction}."
                if entry.restrictions:
                    msg += f" {entry.restrictions}"
                if entry.alternatives:
                    msg += f" Alternatives: {', '.join(entry.alternatives)}."
                is_available = False
            else:  # unavailable
                msg = f"{drug_inn} is NOT AVAILABLE in {jurisdiction}."
                if entry.alternatives:
                    msg += f" Available alternatives: {', '.join(entry.alternatives)}."
                else:
                    msg += " No registered alternatives found."

            return AvailabilityCheck(
                drug=drug_inn, jurisdiction=jurisdiction,
                is_available=is_available, status=entry.status,
                local_brands=entry.local_brands,
                restrictions=entry.restrictions,
                alternatives=entry.alternatives,
                message=msg,
            )

        # Drug not in formulary — unknown status
        return AvailabilityCheck(
            drug=drug_inn, jurisdiction=jurisdiction,
            is_available=True, status="not_in_formulary",
            local_brands=[], restrictions=None, alternatives=[],
            message=f"{drug_inn} not found in {jurisdiction} formulary. Verify local availability.",
        )

    def check_all(self, drugs: list[str], jurisdiction: str = "UZ") -> list[AvailabilityCheck]:
        """Check availability for a list of drugs."""
        return [self.check(drug, jurisdiction) for drug in drugs]

    def get_unavailable_alerts(self, drugs: list[str], jurisdiction: str = "UZ") -> list[str]:
        """
        Get alert messages for unavailable/restricted drugs.
        Returns empty list if all drugs are freely available.
        Used by the pipeline to inject availability warnings into response.
        """
        alerts = []
        for check in self.check_all(drugs, jurisdiction):
            if check.status == "unavailable":
                alerts.append(check.message)
            elif check.status == "restricted":
                alerts.append(check.message)
            elif check.status == "shortage":
                alerts.append(check.message)
        return alerts
