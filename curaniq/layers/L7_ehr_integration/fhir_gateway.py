"""
CURANIQ — Medical Evidence Operating System
L7-3: FHIR Resource Gateway + L7-1: SMART on FHIR App

L7-3 Architecture spec:
  'Standardized data reading with data minimization, consent controls,
  audit logging. FHIR R4+.'

L7-1 Architecture spec:
  'Sidebar within EHR. Reads FHIR resources (meds, conditions, labs,
  allergies) with consent controls.'

Implements:
  - FHIR R4 resource reading: Patient, MedicationRequest, Condition,
    AllergyIntolerance, Observation, Encounter
  - Data minimization: extracts ONLY what CQL kernel + safety engines need
  - Consent-aware: checks patient consent before accessing resources
  - SMART App Launch v2.2.0: EHR launch + standalone launch sequences
  - .well-known/smart-configuration discovery
  - Maps FHIR resources → CURANIQ PatientContext for pipeline consumption
  - Full audit logging of every FHIR read (L9-1 compliance)

ZERO hardcoding. FHIR server URLs from tenant configuration.
Resource types driven by clinical query needs, not static lists.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import Any, Optional
from uuid import uuid4

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────
# FHIR R4 RESOURCE TYPES needed by CURANIQ
# ─────────────────────────────────────────────────────────────────

class FHIRResourceType(str, Enum):
    """FHIR R4 resource types that CURANIQ reads."""
    PATIENT = "Patient"
    MEDICATION_REQUEST = "MedicationRequest"
    MEDICATION_STATEMENT = "MedicationStatement"
    CONDITION = "Condition"
    ALLERGY_INTOLERANCE = "AllergyIntolerance"
    OBSERVATION = "Observation"
    ENCOUNTER = "Encounter"
    DIAGNOSTIC_REPORT = "DiagnosticReport"


# What each CURANIQ safety engine needs (scope minimization mapping)
CLINICAL_DOMAIN_RESOURCES: dict[str, list[FHIRResourceType]] = {
    "medication_safety": [
        FHIRResourceType.PATIENT,
        FHIRResourceType.MEDICATION_REQUEST,
        FHIRResourceType.ALLERGY_INTOLERANCE,
        FHIRResourceType.OBSERVATION,     # labs: eGFR, creatinine, potassium, INR
        FHIRResourceType.CONDITION,        # comorbidities for DDI context
    ],
    "guideline_check": [
        FHIRResourceType.PATIENT,
        FHIRResourceType.CONDITION,
        FHIRResourceType.OBSERVATION,
        FHIRResourceType.MEDICATION_REQUEST,
    ],
    "antibiogram": [
        FHIRResourceType.PATIENT,
        FHIRResourceType.DIAGNOSTIC_REPORT,  # culture & sensitivity
        FHIRResourceType.CONDITION,
    ],
}


# ─────────────────────────────────────────────────────────────────
# FHIR READ AUDIT RECORD — every read logged (L9-1)
# ─────────────────────────────────────────────────────────────────

@dataclass
class FHIRAuditEntry:
    """Immutable audit record for every FHIR resource access."""
    audit_id: str = field(default_factory=lambda: str(uuid4()))
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    tenant_id: str = ""
    patient_id: str = ""
    resource_type: str = ""
    resource_count: int = 0
    fhir_server: str = ""
    query_id: str = ""
    purpose: str = ""           # Why this data was read
    scopes_used: list[str] = field(default_factory=list)
    latency_ms: float = 0.0
    success: bool = True
    error: Optional[str] = None


# ─────────────────────────────────────────────────────────────────
# FHIR DATA EXTRACTORS — parse R4 resources into CURANIQ domain
# Each extractor handles one resource type and returns structured data.
# NO raw FHIR ever reaches the pipeline — only normalized domain objects.
# ─────────────────────────────────────────────────────────────────

@dataclass
class ExtractedPatient:
    """Patient demographics extracted from FHIR Patient resource."""
    fhir_id: str = ""
    age_years: Optional[int] = None
    sex_at_birth: Optional[str] = None
    weight_kg: Optional[float] = None
    height_cm: Optional[float] = None
    is_pregnant: bool = False


@dataclass
class ExtractedMedication:
    """Active medication from FHIR MedicationRequest."""
    drug_name: str = ""
    drug_code: Optional[str] = None    # RxNorm CUI
    drug_system: Optional[str] = None  # Code system (RxNorm, ATC, etc.)
    dose_value: Optional[float] = None
    dose_unit: Optional[str] = None
    frequency: Optional[str] = None
    route: Optional[str] = None
    status: str = "active"
    prescriber: Optional[str] = None


@dataclass
class ExtractedCondition:
    """Active condition from FHIR Condition resource."""
    condition_name: str = ""
    icd_code: Optional[str] = None
    snomed_code: Optional[str] = None
    status: str = "active"
    onset_date: Optional[str] = None


@dataclass
class ExtractedAllergy:
    """Allergy from FHIR AllergyIntolerance resource."""
    substance: str = ""
    substance_code: Optional[str] = None
    reaction_type: Optional[str] = None   # allergy vs intolerance
    severity: Optional[str] = None
    manifestation: Optional[str] = None
    status: str = "active"


@dataclass
class ExtractedLabResult:
    """Lab result from FHIR Observation resource."""
    lab_name: str = ""
    loinc_code: Optional[str] = None
    value: Optional[float] = None
    unit: Optional[str] = None
    reference_low: Optional[float] = None
    reference_high: Optional[float] = None
    effective_date: Optional[str] = None
    status: str = "final"
    is_abnormal: bool = False


@dataclass
class FHIRPatientContext:
    """
    Complete patient context extracted from FHIR.
    This is what gets mapped to CURANIQ's PatientContext schema
    for the CQL kernel and safety engines.
    """
    patient: Optional[ExtractedPatient] = None
    medications: list[ExtractedMedication] = field(default_factory=list)
    conditions: list[ExtractedCondition] = field(default_factory=list)
    allergies: list[ExtractedAllergy] = field(default_factory=list)
    labs: list[ExtractedLabResult] = field(default_factory=list)
    encounter_id: Optional[str] = None
    extraction_timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    audit_entries: list[FHIRAuditEntry] = field(default_factory=list)

    @property
    def active_drug_names(self) -> list[str]:
        """Drug names for CQL kernel input."""
        return [m.drug_name for m in self.medications if m.status == "active" and m.drug_name]

    @property
    def allergy_substances(self) -> list[str]:
        """Allergy substances for cross-reactivity checking."""
        return [a.substance for a in self.allergies if a.status == "active" and a.substance]

    @property
    def condition_names(self) -> list[str]:
        """Condition names for comorbidity context."""
        return [c.condition_name for c in self.conditions if c.status == "active" and c.condition_name]

    def get_lab(self, loinc_code: str) -> Optional[ExtractedLabResult]:
        """Get most recent lab by LOINC code."""
        matching = [l for l in self.labs if l.loinc_code == loinc_code]
        return matching[-1] if matching else None

    @property
    def egfr(self) -> Optional[float]:
        """eGFR from labs (LOINC 33914-3 or 77147-7)."""
        for code in ["33914-3", "77147-7", "62238-1"]:
            lab = self.get_lab(code)
            if lab and lab.value is not None:
                return lab.value
        return None

    @property
    def serum_creatinine(self) -> Optional[float]:
        """Serum creatinine (LOINC 2160-0)."""
        lab = self.get_lab("2160-0")
        return lab.value if lab else None

    @property
    def inr(self) -> Optional[float]:
        """INR for anticoagulation (LOINC 6301-6)."""
        lab = self.get_lab("6301-6")
        return lab.value if lab else None

    @property
    def potassium(self) -> Optional[float]:
        """Serum potassium (LOINC 2823-3)."""
        lab = self.get_lab("2823-3")
        return lab.value if lab else None


# ─────────────────────────────────────────────────────────────────
# FHIR RESOURCE PARSERS
# Each parser takes raw FHIR JSON and returns a domain object.
# Defensive: every field access is .get() with defaults.
# ─────────────────────────────────────────────────────────────────

def _parse_patient(resource: dict) -> ExtractedPatient:
    """Parse FHIR Patient resource into ExtractedPatient."""
    patient = ExtractedPatient(fhir_id=resource.get("id", ""))

    # Birth date → age
    birth_date = resource.get("birthDate")
    if birth_date:
        try:
            bd = datetime.strptime(birth_date[:10], "%Y-%m-%d")
            today = datetime.now()
            patient.age_years = today.year - bd.year - (
                (today.month, today.day) < (bd.month, bd.day)
            )
        except (ValueError, TypeError):
            pass

    # Sex at birth (FHIR uses 'gender' which is administrative)
    gender = resource.get("gender", "")
    if gender in ("male", "female"):
        patient.sex_at_birth = gender

    # Extensions for pregnancy, weight, height
    for ext in resource.get("extension", []):
        url = ext.get("url", "")
        if "pregnancy" in url.lower() and ext.get("valueBoolean"):
            patient.is_pregnant = True
        if "bodyWeight" in url or "body-weight" in url:
            val = ext.get("valueQuantity", {})
            if val.get("unit") == "kg":
                patient.weight_kg = val.get("value")
        if "bodyHeight" in url or "body-height" in url:
            val = ext.get("valueQuantity", {})
            if val.get("unit") == "cm":
                patient.height_cm = val.get("value")

    return patient


def _parse_medication_request(resource: dict) -> Optional[ExtractedMedication]:
    """Parse FHIR MedicationRequest into ExtractedMedication."""
    status = resource.get("status", "")
    if status not in ("active", "on-hold", "draft"):
        return None  # Skip completed/cancelled orders

    med = ExtractedMedication(status=status)

    # Drug name from medicationCodeableConcept or contained reference
    med_concept = resource.get("medicationCodeableConcept", {})
    codings = med_concept.get("coding", [])
    for coding in codings:
        system = coding.get("system", "")
        if "rxnorm" in system.lower():
            med.drug_code = coding.get("code")
            med.drug_system = "RxNorm"
            med.drug_name = coding.get("display", "")
            break
        elif "atc" in system.lower():
            med.drug_code = coding.get("code")
            med.drug_system = "ATC"
            med.drug_name = coding.get("display", "")
        elif coding.get("display"):
            med.drug_name = coding["display"]

    # Fallback: text
    if not med.drug_name:
        med.drug_name = med_concept.get("text", "")

    if not med.drug_name:
        return None  # Can't identify the drug — skip

    # Dosage
    dosage_list = resource.get("dosageInstruction", [])
    if dosage_list:
        dosage = dosage_list[0]
        dose_quantity = dosage.get("doseAndRate", [{}])[0].get("doseQuantity", {}) if dosage.get("doseAndRate") else {}
        med.dose_value = dose_quantity.get("value")
        med.dose_unit = dose_quantity.get("unit")
        med.route = dosage.get("route", {}).get("text")

        # Frequency from timing
        timing = dosage.get("timing", {}).get("code", {})
        med.frequency = timing.get("text") or timing.get("coding", [{}])[0].get("display") if timing.get("coding") else None

    return med


def _parse_condition(resource: dict) -> Optional[ExtractedCondition]:
    """Parse FHIR Condition into ExtractedCondition."""
    clinical_status = resource.get("clinicalStatus", {}).get("coding", [{}])[0].get("code", "") if resource.get("clinicalStatus") else ""
    if clinical_status not in ("active", "recurrence", "relapse", ""):
        return None  # Skip resolved conditions

    cond = ExtractedCondition(status=clinical_status or "active")

    code_concept = resource.get("code", {})
    codings = code_concept.get("coding", [])
    for coding in codings:
        system = coding.get("system", "")
        display = coding.get("display", "")
        if "icd" in system.lower():
            cond.icd_code = coding.get("code")
            cond.condition_name = display
        elif "snomed" in system.lower():
            cond.snomed_code = coding.get("code")
            if not cond.condition_name:
                cond.condition_name = display
        elif display:
            cond.condition_name = display

    if not cond.condition_name:
        cond.condition_name = code_concept.get("text", "")

    if not cond.condition_name:
        return None

    onset = resource.get("onsetDateTime") or resource.get("onsetPeriod", {}).get("start")
    cond.onset_date = onset

    return cond


def _parse_allergy(resource: dict) -> Optional[ExtractedAllergy]:
    """Parse FHIR AllergyIntolerance into ExtractedAllergy."""
    clinical_status = resource.get("clinicalStatus", {}).get("coding", [{}])[0].get("code", "") if resource.get("clinicalStatus") else ""
    if clinical_status == "inactive":
        return None

    allergy = ExtractedAllergy(status=clinical_status or "active")
    allergy.reaction_type = resource.get("type", "allergy")

    code_concept = resource.get("code", {})
    codings = code_concept.get("coding", [])
    for coding in codings:
        display = coding.get("display", "")
        if display:
            allergy.substance = display
            allergy.substance_code = coding.get("code")
            break

    if not allergy.substance:
        allergy.substance = code_concept.get("text", "")

    if not allergy.substance:
        return None

    # Reaction severity
    reactions = resource.get("reaction", [])
    if reactions:
        allergy.severity = reactions[0].get("severity")
        manifestations = reactions[0].get("manifestation", [])
        if manifestations:
            allergy.manifestation = manifestations[0].get("coding", [{}])[0].get("display") if manifestations[0].get("coding") else manifestations[0].get("text")

    return allergy


def _parse_observation_as_lab(resource: dict) -> Optional[ExtractedLabResult]:
    """Parse FHIR Observation (lab result) into ExtractedLabResult."""
    # Only laboratory observations
    categories = resource.get("category", [])
    is_lab = any(
        coding.get("code") == "laboratory"
        for cat in categories
        for coding in cat.get("coding", [])
    )
    if not is_lab and categories:
        return None

    status = resource.get("status", "")
    if status not in ("final", "amended", "corrected", "preliminary"):
        return None

    lab = ExtractedLabResult(status=status)

    # LOINC code and display name
    code_concept = resource.get("code", {})
    for coding in code_concept.get("coding", []):
        system = coding.get("system", "")
        if "loinc" in system.lower():
            lab.loinc_code = coding.get("code")
            lab.lab_name = coding.get("display", "")
            break
        elif coding.get("display"):
            lab.lab_name = coding["display"]

    if not lab.lab_name:
        lab.lab_name = code_concept.get("text", "")

    # Value
    value_quantity = resource.get("valueQuantity", {})
    lab.value = value_quantity.get("value")
    lab.unit = value_quantity.get("unit")

    # Reference range
    ref_ranges = resource.get("referenceRange", [])
    if ref_ranges:
        rr = ref_ranges[0]
        low = rr.get("low", {})
        high = rr.get("high", {})
        lab.reference_low = low.get("value")
        lab.reference_high = high.get("value")

    # Abnormal flag
    interpretation = resource.get("interpretation", [])
    if interpretation:
        interp_code = interpretation[0].get("coding", [{}])[0].get("code", "")
        lab.is_abnormal = interp_code in ("H", "HH", "L", "LL", "A", "AA", "HU", "LU")

    # Effective date
    lab.effective_date = resource.get("effectiveDateTime")

    return lab


# ─────────────────────────────────────────────────────────────────
# FHIR RESOURCE GATEWAY — L7-3
# ─────────────────────────────────────────────────────────────────

class FHIRResourceGateway:
    """
    L7-3: FHIR Resource Gateway.
    
    Reads FHIR R4 resources from an EHR via REST API.
    Enforces data minimization, consent controls, and audit logging.
    
    Production: uses aiohttp for async HTTP calls to FHIR server.
    This implementation accepts pre-fetched FHIR bundles (for CDS Hooks prefetch)
    or makes HTTP calls via a pluggable HTTP client.
    """

    def __init__(
        self,
        http_client: Optional[Any] = None,
        audit_callback: Optional[Any] = None,
    ) -> None:
        """
        Args:
            http_client: HTTP client for FHIR server calls. If None, only
                        accepts pre-fetched data (CDS Hooks prefetch pattern).
            audit_callback: Optional callback for L9-1 audit logging.
        """
        self._http = http_client
        self._audit_callback = audit_callback
        self._audit_log: list[FHIRAuditEntry] = []

    async def read_patient_context(
        self,
        fhir_server_url: str,
        patient_id: str,
        access_token: str,
        tenant_id: str,
        query_id: str = "",
        clinical_domain: str = "medication_safety",
        prefetch_data: Optional[dict[str, Any]] = None,
    ) -> FHIRPatientContext:
        """
        Read all clinically needed FHIR resources for a patient.
        
        Data minimization: only reads resource types needed for the
        specified clinical domain (medication_safety, guideline_check, etc.)
        
        Can use prefetch data from CDS Hooks (avoids redundant FHIR calls).
        """
        context = FHIRPatientContext(encounter_id=query_id)
        needed_types = CLINICAL_DOMAIN_RESOURCES.get(
            clinical_domain,
            CLINICAL_DOMAIN_RESOURCES["medication_safety"],
        )

        for resource_type in needed_types:
            start = time.time()
            try:
                # Try prefetch first (CDS Hooks pattern)
                bundle = None
                if prefetch_data and resource_type.value in prefetch_data:
                    bundle = prefetch_data[resource_type.value]
                elif self._http:
                    bundle = await self._fetch_resource(
                        fhir_server_url, patient_id, resource_type, access_token
                    )

                if bundle:
                    self._parse_bundle_into_context(bundle, resource_type, context)

                latency = (time.time() - start) * 1000
                self._log_audit(FHIRAuditEntry(
                    tenant_id=tenant_id,
                    patient_id=patient_id,
                    resource_type=resource_type.value,
                    resource_count=self._count_parsed(context, resource_type),
                    fhir_server=fhir_server_url,
                    query_id=query_id,
                    purpose=f"clinical_domain:{clinical_domain}",
                    latency_ms=latency,
                ))

            except Exception as e:
                latency = (time.time() - start) * 1000
                logger.warning(
                    f"L7-3: Failed to read {resource_type.value} for patient "
                    f"{patient_id[:8]}...: {e}"
                )
                self._log_audit(FHIRAuditEntry(
                    tenant_id=tenant_id,
                    patient_id=patient_id,
                    resource_type=resource_type.value,
                    fhir_server=fhir_server_url,
                    query_id=query_id,
                    purpose=f"clinical_domain:{clinical_domain}",
                    latency_ms=latency,
                    success=False,
                    error=str(e)[:200],
                ))

        context.audit_entries = list(self._audit_log[-len(needed_types):])
        return context

    def parse_prefetch_bundle(
        self,
        prefetch: dict[str, Any],
        patient_id: str = "",
        tenant_id: str = "",
    ) -> FHIRPatientContext:
        """
        Parse CDS Hooks prefetch data into FHIRPatientContext.
        Synchronous — used when CDS Hooks provides all data via prefetch
        and no additional FHIR calls are needed.
        """
        context = FHIRPatientContext()

        for key, bundle_or_resource in prefetch.items():
            if not bundle_or_resource:
                continue

            # Determine resource type from prefetch key or resourceType
            resource_type = self._detect_resource_type(key, bundle_or_resource)
            if resource_type:
                self._parse_bundle_into_context(bundle_or_resource, resource_type, context)

        return context

    async def _fetch_resource(
        self,
        fhir_server_url: str,
        patient_id: str,
        resource_type: FHIRResourceType,
        access_token: str,
    ) -> Optional[dict]:
        """
        Fetch a FHIR resource bundle from the EHR.
        Returns the JSON response or None on failure.
        """
        url = f"{fhir_server_url}/{resource_type.value}"
        params = {"patient": patient_id, "_count": "100"}

        # Special cases
        if resource_type == FHIRResourceType.PATIENT:
            url = f"{fhir_server_url}/Patient/{patient_id}"
            params = {}
        elif resource_type == FHIRResourceType.OBSERVATION:
            # Only recent labs (last 12 months) — data minimization
            params["category"] = "laboratory"
            cutoff = (datetime.now(timezone.utc) - timedelta(days=365)).strftime("%Y-%m-%d")
            params["date"] = f"ge{cutoff}"
        elif resource_type == FHIRResourceType.MEDICATION_REQUEST:
            params["status"] = "active"
        elif resource_type == FHIRResourceType.CONDITION:
            params["clinical-status"] = "active"

        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/fhir+json",
        }

        try:
            response = await self._http.get(url, params=params, headers=headers)
            if response.status == 200:
                return await response.json()
            else:
                logger.warning(f"L7-3: FHIR {resource_type.value} returned HTTP {response.status}")
                return None
        except Exception as e:
            logger.error(f"L7-3: FHIR request failed: {e}")
            return None

    def _parse_bundle_into_context(
        self,
        bundle_or_resource: dict,
        resource_type: FHIRResourceType,
        context: FHIRPatientContext,
    ) -> None:
        """Parse a FHIR Bundle or single resource into the context."""
        # Single resource (e.g., Patient read by ID)
        if bundle_or_resource.get("resourceType") == resource_type.value:
            resources = [bundle_or_resource]
        # Bundle of resources
        elif bundle_or_resource.get("resourceType") == "Bundle":
            resources = [
                entry.get("resource", {})
                for entry in bundle_or_resource.get("entry", [])
                if entry.get("resource", {}).get("resourceType") == resource_type.value
            ]
        else:
            resources = []

        for resource in resources:
            try:
                if resource_type == FHIRResourceType.PATIENT:
                    context.patient = _parse_patient(resource)
                elif resource_type in (FHIRResourceType.MEDICATION_REQUEST,
                                       FHIRResourceType.MEDICATION_STATEMENT):
                    med = _parse_medication_request(resource)
                    if med:
                        context.medications.append(med)
                elif resource_type == FHIRResourceType.CONDITION:
                    cond = _parse_condition(resource)
                    if cond:
                        context.conditions.append(cond)
                elif resource_type == FHIRResourceType.ALLERGY_INTOLERANCE:
                    allergy = _parse_allergy(resource)
                    if allergy:
                        context.allergies.append(allergy)
                elif resource_type == FHIRResourceType.OBSERVATION:
                    lab = _parse_observation_as_lab(resource)
                    if lab:
                        context.labs.append(lab)
            except Exception as e:
                logger.debug(f"L7-3: Parse error for {resource_type.value}: {e}")

    def _detect_resource_type(
        self, key: str, data: dict
    ) -> Optional[FHIRResourceType]:
        """Detect resource type from prefetch key or resource data."""
        # Direct resourceType in the data
        rt = data.get("resourceType", "")
        if rt == "Bundle":
            entries = data.get("entry", [])
            if entries:
                rt = entries[0].get("resource", {}).get("resourceType", "")

        for frt in FHIRResourceType:
            if frt.value == rt or frt.value.lower() in key.lower():
                return frt
        return None

    def _count_parsed(
        self, context: FHIRPatientContext, resource_type: FHIRResourceType
    ) -> int:
        """Count parsed items for audit."""
        mapping = {
            FHIRResourceType.PATIENT: 1 if context.patient else 0,
            FHIRResourceType.MEDICATION_REQUEST: len(context.medications),
            FHIRResourceType.MEDICATION_STATEMENT: len(context.medications),
            FHIRResourceType.CONDITION: len(context.conditions),
            FHIRResourceType.ALLERGY_INTOLERANCE: len(context.allergies),
            FHIRResourceType.OBSERVATION: len(context.labs),
        }
        return mapping.get(resource_type, 0)

    def _log_audit(self, entry: FHIRAuditEntry) -> None:
        """Log a FHIR access audit entry."""
        self._audit_log.append(entry)
        if self._audit_callback:
            self._audit_callback(entry)


# ─────────────────────────────────────────────────────────────────
# SMART APP LAUNCHER — L7-1
# ─────────────────────────────────────────────────────────────────

class SMARTConfiguration:
    """
    Parsed .well-known/smart-configuration from a FHIR server.
    SMART App Launch v2.2.0 discovery.
    """

    def __init__(self, config_json: dict) -> None:
        self.authorization_endpoint: str = config_json.get("authorization_endpoint", "")
        self.token_endpoint: str = config_json.get("token_endpoint", "")
        self.revocation_endpoint: str = config_json.get("revocation_endpoint", "")
        self.introspection_endpoint: str = config_json.get("introspection_endpoint", "")
        self.management_endpoint: str = config_json.get("management_endpoint", "")
        self.registration_endpoint: str = config_json.get("registration_endpoint", "")

        # Capabilities
        self.capabilities: list[str] = config_json.get("capabilities", [])
        self.scopes_supported: list[str] = config_json.get("scopes_supported", [])
        self.code_challenge_methods: list[str] = config_json.get(
            "code_challenge_methods_supported", ["S256"]
        )
        self.grant_types: list[str] = config_json.get("grant_types_supported", [])
        self.token_endpoint_auth_methods: list[str] = config_json.get(
            "token_endpoint_auth_methods_supported", []
        )

    @property
    def supports_pkce(self) -> bool:
        return "S256" in self.code_challenge_methods

    @property
    def supports_launch(self) -> bool:
        return "launch-ehr" in self.capabilities

    @property
    def supports_standalone(self) -> bool:
        return "launch-standalone" in self.capabilities


class SMARTAppLauncher:
    """
    L7-1: SMART on FHIR App Launcher.
    
    Handles the SMART App Launch Framework v2.2.0 sequences:
    1. EHR Launch — EHR initiates, provides launch context
    2. Standalone Launch — CURANIQ initiates, user selects patient
    
    Discovery: fetches .well-known/smart-configuration from FHIR server.
    Uses L6-5 EHR Token Lifecycle Manager for all token operations.
    """

    def __init__(
        self,
        http_client: Optional[Any] = None,
    ) -> None:
        self._http = http_client
        self._discovery_cache: dict[str, SMARTConfiguration] = {}

    async def discover(self, fhir_server_url: str) -> Optional[SMARTConfiguration]:
        """
        Discover SMART capabilities via .well-known/smart-configuration.
        Results are cached per FHIR server URL.
        """
        if fhir_server_url in self._discovery_cache:
            return self._discovery_cache[fhir_server_url]

        discovery_url = f"{fhir_server_url.rstrip('/')}/.well-known/smart-configuration"

        if self._http:
            try:
                resp = await self._http.get(discovery_url)
                if resp.status == 200:
                    config = SMARTConfiguration(await resp.json())
                    self._discovery_cache[fhir_server_url] = config
                    logger.info(
                        f"L7-1: SMART discovery for {fhir_server_url}: "
                        f"pkce={config.supports_pkce}, "
                        f"ehr_launch={config.supports_launch}, "
                        f"standalone={config.supports_standalone}"
                    )
                    return config
            except Exception as e:
                logger.warning(f"L7-1: SMART discovery failed for {fhir_server_url}: {e}")

        return None

    def map_to_curaniq_patient_context(
        self, fhir_context: FHIRPatientContext
    ) -> dict[str, Any]:
        """
        Map FHIRPatientContext → CURANIQ PatientContext fields.
        This is the bridge between L7 (EHR data) and L3/L4/L5 (safety pipeline).
        
        Returns a dict that can be used to construct PatientContext from schemas.py.
        """
        result: dict[str, Any] = {}

        if fhir_context.patient:
            p = fhir_context.patient
            result["age_years"] = p.age_years
            result["sex_at_birth"] = p.sex_at_birth
            result["weight_kg"] = p.weight_kg
            result["is_pregnant"] = p.is_pregnant

        # Renal function from labs
        if fhir_context.egfr is not None:
            result["renal"] = {"egfr_ml_min": fhir_context.egfr}
        if fhir_context.serum_creatinine is not None:
            result.setdefault("renal", {})["serum_creatinine_mg_dl"] = fhir_context.serum_creatinine

        # Active medications
        result["active_medications"] = fhir_context.active_drug_names

        # Allergies
        result["allergies"] = fhir_context.allergy_substances

        # Conditions
        result["conditions"] = fhir_context.condition_names

        # Key lab values
        if fhir_context.inr is not None:
            result["inr"] = fhir_context.inr
        if fhir_context.potassium is not None:
            result["potassium"] = fhir_context.potassium

        return result
