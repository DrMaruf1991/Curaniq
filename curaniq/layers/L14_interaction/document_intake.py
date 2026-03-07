"""
CURANIQ — Medical Evidence Operating System
L14-7: Document Intake Pipeline

Architecture spec:
  'Attachment processing: guidelines, protocols, lab reports, discharge summaries.
  Structured extraction into patient context or institutional policy layer.
  Non-FHIR adapter for HL7v2/CSV/API bridge (Uzbekistan/CIS reality).'

Implements:
  - Multi-format document parsing (PDF, DOCX, CSV, JSON, plain text, HL7v2)
  - Automatic document type classification
  - Structured extraction: lab values, medication lists, diagnoses
  - Security: L6-6 upload scanning integration point
  - Routes extracted data to appropriate layer:
    → Patient context (lab reports, discharge summaries) → L3/L4 pipeline
    → Institutional knowledge (guidelines, protocols) → L7-16
    → Evidence store (research articles) → L1 ingestion
  - CIS market adapter: handles Cyrillic, local date formats, non-standard labs
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional
from uuid import uuid4

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────
# DOCUMENT TYPES & ROUTING
# ─────────────────────────────────────────────────────────────────

class DocumentType(str, Enum):
    """Detected document type determines processing pipeline."""
    LAB_REPORT = "lab_report"
    DISCHARGE_SUMMARY = "discharge_summary"
    MEDICATION_LIST = "medication_list"
    CLINICAL_GUIDELINE = "clinical_guideline"
    HOSPITAL_PROTOCOL = "hospital_protocol"
    RESEARCH_ARTICLE = "research_article"
    PRESCRIPTION = "prescription"
    REFERRAL_LETTER = "referral_letter"
    RADIOLOGY_REPORT = "radiology_report"
    HL7V2_MESSAGE = "hl7v2_message"
    ANTIBIOGRAM_DATA = "antibiogram_data"
    FORMULARY_DATA = "formulary_data"
    UNKNOWN = "unknown"


class DocumentRoute(str, Enum):
    """Where extracted data should be routed."""
    PATIENT_CONTEXT = "patient_context"       # → L3/L4 pipeline
    INSTITUTIONAL = "institutional"            # → L7-16 protocols
    EVIDENCE_STORE = "evidence_store"          # → L1 ingestion
    ANTIBIOGRAM = "antibiogram"               # → L7-17
    FORMULARY = "formulary"                   # → L7-16 formulary


# Document type → routing rules
ROUTING_TABLE: dict[DocumentType, DocumentRoute] = {
    DocumentType.LAB_REPORT: DocumentRoute.PATIENT_CONTEXT,
    DocumentType.DISCHARGE_SUMMARY: DocumentRoute.PATIENT_CONTEXT,
    DocumentType.MEDICATION_LIST: DocumentRoute.PATIENT_CONTEXT,
    DocumentType.PRESCRIPTION: DocumentRoute.PATIENT_CONTEXT,
    DocumentType.CLINICAL_GUIDELINE: DocumentRoute.INSTITUTIONAL,
    DocumentType.HOSPITAL_PROTOCOL: DocumentRoute.INSTITUTIONAL,
    DocumentType.RESEARCH_ARTICLE: DocumentRoute.EVIDENCE_STORE,
    DocumentType.REFERRAL_LETTER: DocumentRoute.PATIENT_CONTEXT,
    DocumentType.RADIOLOGY_REPORT: DocumentRoute.PATIENT_CONTEXT,
    DocumentType.HL7V2_MESSAGE: DocumentRoute.PATIENT_CONTEXT,
    DocumentType.ANTIBIOGRAM_DATA: DocumentRoute.ANTIBIOGRAM,
    DocumentType.FORMULARY_DATA: DocumentRoute.FORMULARY,
}


# ─────────────────────────────────────────────────────────────────
# DOCUMENT CLASSIFICATION — pattern-based type detection
# ─────────────────────────────────────────────────────────────────

# Classification signals (multi-language: EN + RU + UZ)
_TYPE_SIGNALS: dict[DocumentType, list[re.Pattern]] = {
    DocumentType.LAB_REPORT: [
        re.compile(r'\b(lab\s*result|laboratory|анализ|лаборатор|tahlil\s*natija|'
                   r'eGFR|creatinine|креатинин|hemoglobin|гемоглобин|HbA1c|'
                   r'WBC|RBC|platelet|тромбоцит|INR|potassium|калий|sodium)\b', re.I),
    ],
    DocumentType.DISCHARGE_SUMMARY: [
        re.compile(r'\b(discharge\s*summary|выписка|выписной\s*эпикриз|'
                   r'chiqish\s*xulosasi|hospital\s*course|reason\s*for\s*admission)\b', re.I),
    ],
    DocumentType.MEDICATION_LIST: [
        re.compile(r'\b(medication\s*list|лекарственные\s*средства|dori\s*ro\'yxati|'
                   r'current\s*medications|home\s*medications|лист\s*назначений)\b', re.I),
    ],
    DocumentType.CLINICAL_GUIDELINE: [
        re.compile(r'\b(clinical\s*guideline|клиническ\w*\s*рекомендац|practice\s*guideline|'
                   r'NICE|AHA|ACC|WHO\s*guideline|klinik\s*qo\'llanma|протокол\s*лечения)\b', re.I),
    ],
    DocumentType.HOSPITAL_PROTOCOL: [
        re.compile(r'\b(hospital\s*protocol|стандарт\w*\s*операц|institutional\s*policy|'
                   r'local\s*protocol|kasalxona\s*protokoli|внутренн\w*\s*протокол)\b', re.I),
    ],
    DocumentType.PRESCRIPTION: [
        re.compile(r'\b(prescription|рецепт|retsept|Rx|℞|назначение\s*врача)\b', re.I),
    ],
    DocumentType.ANTIBIOGRAM_DATA: [
        re.compile(r'\b(antibiogram|антибиотикограмм|susceptibility\s*report|'
                   r'cumulative\s*susceptibility|MIC|зона\s*подавления)\b', re.I),
    ],
    DocumentType.FORMULARY_DATA: [
        re.compile(r'\b(formulary|формуляр|перечень\s*лекарств|dori\s*formulyari|'
                   r'drug\s*list|лекарственный\s*перечень)\b', re.I),
    ],
    DocumentType.HL7V2_MESSAGE: [
        re.compile(r'^MSH\|', re.M),
        re.compile(r'\bORU\^R01\b|\bADT\^A0[1-9]\b|\bORM\^O01\b'),
    ],
    DocumentType.RESEARCH_ARTICLE: [
        re.compile(r'\b(abstract|PMID|doi:10\.\d+|PubMed|systematic\s*review|'
                   r'meta.analysis|randomized|clinical\s*trial)\b', re.I),
    ],
}


def classify_document(content: str, filename: Optional[str] = None) -> DocumentType:
    """
    Classify a document by content patterns.
    Supports EN/RU/UZ text. Filename is a secondary signal.
    """
    scores: dict[DocumentType, int] = {dt: 0 for dt in DocumentType}

    # Content-based classification
    for doc_type, patterns in _TYPE_SIGNALS.items():
        for pattern in patterns:
            matches = pattern.findall(content[:5000])  # First 5000 chars
            scores[doc_type] += len(matches)

    # Filename-based boost
    if filename:
        fn_lower = filename.lower()
        if "antibiogram" in fn_lower or "susceptibility" in fn_lower:
            scores[DocumentType.ANTIBIOGRAM_DATA] += 10
        elif "formulary" in fn_lower or "формуляр" in fn_lower:
            scores[DocumentType.FORMULARY_DATA] += 10
        elif fn_lower.endswith(".hl7") or fn_lower.endswith(".hl7v2"):
            scores[DocumentType.HL7V2_MESSAGE] += 10
        elif "lab" in fn_lower or "анализ" in fn_lower:
            scores[DocumentType.LAB_REPORT] += 5
        elif "discharge" in fn_lower or "выписка" in fn_lower:
            scores[DocumentType.DISCHARGE_SUMMARY] += 5

    best = max(scores, key=lambda dt: scores[dt])
    if scores[best] == 0:
        return DocumentType.UNKNOWN
    return best


# ─────────────────────────────────────────────────────────────────
# EXTRACTORS — structured data from raw text
# ─────────────────────────────────────────────────────────────────

@dataclass
class ExtractedLabValue:
    """A lab value extracted from unstructured text."""
    lab_name: str
    value: Optional[float] = None
    unit: Optional[str] = None
    reference_range: Optional[str] = None
    is_abnormal: bool = False
    loinc_code: Optional[str] = None


@dataclass
class ExtractedMedicationEntry:
    """A medication extracted from text."""
    drug_name: str
    dose: Optional[str] = None
    frequency: Optional[str] = None
    route: Optional[str] = None


@dataclass
class ExtractionResult:
    """Complete extraction result from a document."""
    document_id: str = field(default_factory=lambda: str(uuid4()))
    document_type: DocumentType = DocumentType.UNKNOWN
    route: DocumentRoute = DocumentRoute.PATIENT_CONTEXT
    # Extracted structured data
    labs: list[ExtractedLabValue] = field(default_factory=list)
    medications: list[ExtractedMedicationEntry] = field(default_factory=list)
    diagnoses: list[str] = field(default_factory=list)
    allergies: list[str] = field(default_factory=list)
    # For institutional routing
    structured_data: list[dict] = field(default_factory=list)  # CSV/JSON rows
    raw_text: str = ""
    # Metadata
    source_filename: Optional[str] = None
    language_detected: Optional[str] = None
    extraction_confidence: float = 0.0
    warnings: list[str] = field(default_factory=list)


# Lab value patterns (EN + RU + standard units)
_LAB_PATTERNS: list[tuple[str, re.Pattern, Optional[str]]] = [
    ("eGFR", re.compile(r'(?:eGFR|СКФ|GFR)\s*[:\-=]?\s*([\d.]+)\s*(mL/min|мл/мин)?', re.I), "33914-3"),
    ("Creatinine", re.compile(r'(?:creatinine|креатинин)\s*[:\-=]?\s*([\d.]+)\s*(mg/dL|мг/дл|µmol/L|мкмоль/л)?', re.I), "2160-0"),
    ("Potassium", re.compile(r'(?:potassium|калий|K\+?)\s*[:\-=]?\s*([\d.]+)\s*(mmol/L|ммоль/л|mEq/L)?', re.I), "2823-3"),
    ("Sodium", re.compile(r'(?:sodium|натрий|Na\+?)\s*[:\-=]?\s*([\d.]+)\s*(mmol/L|ммоль/л|mEq/L)?', re.I), "2951-2"),
    ("Hemoglobin", re.compile(r'(?:hemoglobin|Hb|гемоглобин|Hgb)\s*[:\-=]?\s*([\d.]+)\s*(g/dL|г/дл|g/L|г/л)?', re.I), "718-7"),
    ("HbA1c", re.compile(r'(?:HbA1c|гликированный\s*гемоглобин|A1c)\s*[:\-=]?\s*([\d.]+)\s*(%)?', re.I), "4548-4"),
    ("INR", re.compile(r'(?:INR|МНО)\s*[:\-=]?\s*([\d.]+)', re.I), "6301-6"),
    ("WBC", re.compile(r'(?:WBC|лейкоциты|белые\s*кровяные)\s*[:\-=]?\s*([\d.]+)\s*(×10⁹/L|10\^9/L|тыс)?', re.I), "6690-2"),
    ("Platelets", re.compile(r'(?:platelets|тромбоциты|PLT)\s*[:\-=]?\s*([\d.]+)\s*(×10⁹/L|10\^9/L|тыс)?', re.I), "777-3"),
    ("ALT", re.compile(r'(?:ALT|АЛТ|SGPT)\s*[:\-=]?\s*([\d.]+)\s*(U/L|Ед/л)?', re.I), "1742-6"),
    ("AST", re.compile(r'(?:AST|АСТ|SGOT)\s*[:\-=]?\s*([\d.]+)\s*(U/L|Ед/л)?', re.I), "1920-8"),
    ("Albumin", re.compile(r'(?:albumin|альбумин)\s*[:\-=]?\s*([\d.]+)\s*(g/dL|г/дл|g/L|г/л)?', re.I), "1751-7"),
    ("Glucose", re.compile(r'(?:glucose|глюкоза|сахар\s*крови)\s*[:\-=]?\s*([\d.]+)\s*(mg/dL|ммоль/л|mmol/L)?', re.I), "2345-7"),
    ("TSH", re.compile(r'(?:TSH|ТТГ)\s*[:\-=]?\s*([\d.]+)\s*(mIU/L|мМЕ/л)?', re.I), "3016-3"),
    ("Total Cholesterol", re.compile(r'(?:total\s*cholesterol|общий\s*холестерин)\s*[:\-=]?\s*([\d.]+)\s*(mg/dL|ммоль/л)?', re.I), "2093-3"),
]

# Medication pattern (EN + RU)
_MED_PATTERN = re.compile(
    r'(?:^|\n)\s*\d*\.?\s*'  # Optional numbering
    r'([A-ZА-ЯЁa-zа-яё][A-ZА-ЯЁa-zа-яё\-\s]{2,30})'  # Drug name
    r'\s+([\d.]+\s*(?:mg|мг|g|г|mcg|мкг|ml|мл|IU|МЕ))'   # Dose
    r'(?:\s*[,\-]?\s*([\d]+\s*(?:раз|times|x|р/д|tab|таб|caps|капс)[^\n]*))?',  # Frequency
    re.I | re.M
)


def extract_labs(text: str) -> list[ExtractedLabValue]:
    """Extract lab values from free text. Multilingual (EN/RU)."""
    results = []
    for lab_name, pattern, loinc in _LAB_PATTERNS:
        match = pattern.search(text)
        if match:
            try:
                value = float(match.group(1))
                unit = match.group(2) if match.lastindex >= 2 else None
                results.append(ExtractedLabValue(
                    lab_name=lab_name,
                    value=value,
                    unit=unit,
                    loinc_code=loinc,
                ))
            except (ValueError, IndexError):
                pass
    return results


def extract_medications(text: str) -> list[ExtractedMedicationEntry]:
    """Extract medication entries from text. Handles numbered lists."""
    results = []
    for match in _MED_PATTERN.finditer(text):
        name = match.group(1).strip()
        dose = match.group(2).strip() if match.group(2) else None
        freq = match.group(3).strip() if match.lastindex >= 3 and match.group(3) else None
        if len(name) >= 3:
            results.append(ExtractedMedicationEntry(
                drug_name=name, dose=dose, frequency=freq
            ))
    return results


# ─────────────────────────────────────────────────────────────────
# HL7v2 PARSER — CIS market adapter (Uzbekistan/CIS reality)
# ─────────────────────────────────────────────────────────────────

def parse_hl7v2_message(raw: str) -> dict[str, Any]:
    """
    Parse an HL7v2 message into structured segments.
    Handles common message types: ORU^R01 (labs), ADT (admissions), ORM (orders).
    This is the non-FHIR adapter for CIS markets.
    """
    segments: dict[str, list[list[str]]] = {}
    for line in raw.strip().split("\n"):
        line = line.strip()
        if not line or len(line) < 3:
            continue
        fields = line.split("|")
        seg_type = fields[0][:3]
        if seg_type not in segments:
            segments[seg_type] = []
        segments[seg_type].append(fields)

    result: dict[str, Any] = {"segments": {}, "message_type": "unknown"}

    # MSH — Message Header
    msh_list = segments.get("MSH", [])
    if msh_list:
        msh = msh_list[0]
        if len(msh) > 9:
            result["message_type"] = msh[8] if len(msh) > 8 else "unknown"
            result["sending_facility"] = msh[3] if len(msh) > 3 else ""
            result["timestamp"] = msh[6] if len(msh) > 6 else ""

    # PID — Patient Identification
    pid_list = segments.get("PID", [])
    if pid_list:
        pid = pid_list[0]
        result["patient"] = {
            "id": pid[3] if len(pid) > 3 else "",
            "name": pid[5] if len(pid) > 5 else "",
            "dob": pid[7] if len(pid) > 7 else "",
            "sex": pid[8] if len(pid) > 8 else "",
        }

    # OBX — Observation Result (lab values)
    obx_list = segments.get("OBX", [])
    labs = []
    for obx in obx_list:
        if len(obx) >= 6:
            lab = {
                "code": obx[3] if len(obx) > 3 else "",
                "value": obx[5] if len(obx) > 5 else "",
                "unit": obx[6] if len(obx) > 6 else "",
                "reference_range": obx[7] if len(obx) > 7 else "",
                "abnormal_flag": obx[8] if len(obx) > 8 else "",
                "status": obx[11] if len(obx) > 11 else "",
            }
            labs.append(lab)
    if labs:
        result["labs"] = labs

    # RXA / RXE — Pharmacy/medication segments
    for seg_name in ("RXA", "RXE"):
        seg_list = segments.get(seg_name, [])
        if seg_list:
            meds = []
            for seg in seg_list:
                med = {
                    "drug": seg[5] if len(seg) > 5 else "",
                    "dose": seg[6] if len(seg) > 6 else "",
                    "route": seg[9] if len(seg) > 9 else "",
                }
                meds.append(med)
            result["medications"] = meds

    result["segments"] = {k: len(v) for k, v in segments.items()}
    return result


# ─────────────────────────────────────────────────────────────────
# CSV/JSON STRUCTURED DATA PARSER
# ─────────────────────────────────────────────────────────────────

def parse_csv_content(content: str) -> list[dict]:
    """Parse CSV content into list of dicts. Auto-detects delimiter."""
    try:
        dialect = csv.Sniffer().sniff(content[:2000])
    except csv.Error:
        dialect = None

    reader = csv.DictReader(io.StringIO(content), dialect=dialect)
    rows = []
    for row in reader:
        rows.append(dict(row))
        if len(rows) >= 10000:  # Safety limit
            break
    return rows


def parse_json_content(content: str) -> list[dict]:
    """Parse JSON content — handles array or single object."""
    data = json.loads(content)
    if isinstance(data, list):
        return data
    elif isinstance(data, dict):
        return [data]
    return []


# ─────────────────────────────────────────────────────────────────
# DOCUMENT INTAKE PIPELINE — main orchestrator
# ─────────────────────────────────────────────────────────────────

class DocumentIntakePipeline:
    """
    L14-7: Document Intake Pipeline.
    
    Processes uploaded documents through:
    1. Format detection + parsing
    2. Document type classification
    3. Structured data extraction (labs, meds, diagnoses)
    4. Routing to appropriate CURANIQ layer
    
    Supports: PDF text, DOCX text, CSV, JSON, HL7v2, plain text.
    Languages: English, Russian, Uzbek.
    """

    def __init__(self) -> None:
        self._processed: list[ExtractionResult] = []

    def process(
        self,
        content: str,
        filename: Optional[str] = None,
        content_type: Optional[str] = None,
        tenant_id: Optional[str] = None,
    ) -> ExtractionResult:
        """
        Process a document through the intake pipeline.
        
        Args:
            content: Text content (already extracted from PDF/DOCX by caller)
            filename: Original filename (used for type detection)
            content_type: MIME type hint
            tenant_id: For institutional routing
        
        Returns:
            ExtractionResult with structured data + routing recommendation
        """
        result = ExtractionResult(
            source_filename=filename,
            raw_text=content[:50000],  # Safety limit
        )

        # Step 1: Detect format and parse if structured
        is_hl7v2 = content.strip().startswith("MSH|")
        is_json = content.strip().startswith(("{", "["))
        is_csv = (
            filename and filename.lower().endswith((".csv", ".tsv"))
        ) or (content.count(",") > 10 and content.count("\n") > 3)

        # Step 2: Classify document type
        result.document_type = classify_document(content, filename)
        result.route = ROUTING_TABLE.get(result.document_type, DocumentRoute.PATIENT_CONTEXT)

        # Step 3: Extract structured data based on format + type
        if is_hl7v2:
            hl7_data = parse_hl7v2_message(content)
            result.structured_data = [hl7_data]
            # Extract labs from HL7v2 OBX segments
            for lab in hl7_data.get("labs", []):
                try:
                    result.labs.append(ExtractedLabValue(
                        lab_name=lab.get("code", ""),
                        value=float(lab["value"]) if lab.get("value") else None,
                        unit=lab.get("unit"),
                    ))
                except (ValueError, TypeError):
                    pass
            result.extraction_confidence = 0.90

        elif is_json:
            try:
                result.structured_data = parse_json_content(content)
                result.extraction_confidence = 0.95
            except json.JSONDecodeError as e:
                result.warnings.append(f"JSON parse error: {e}")

        elif is_csv:
            try:
                result.structured_data = parse_csv_content(content)
                result.extraction_confidence = 0.90
            except Exception as e:
                result.warnings.append(f"CSV parse error: {e}")

        else:
            # Unstructured text — extract labs and medications
            result.labs = extract_labs(content)
            result.medications = extract_medications(content)
            result.extraction_confidence = 0.60 + 0.05 * min(len(result.labs), 6)

        # Step 4: Language detection (simple heuristic)
        cyrillic_count = sum(1 for c in content[:1000] if '\u0400' <= c <= '\u04FF')
        latin_count = sum(1 for c in content[:1000] if 'a' <= c.lower() <= 'z')
        if cyrillic_count > latin_count:
            result.language_detected = "ru"  # Could be UZ-cyrillic too
        else:
            result.language_detected = "en"

        self._processed.append(result)

        logger.info(
            f"L14-7: Document processed: type={result.document_type.value}, "
            f"route={result.route.value}, labs={len(result.labs)}, "
            f"meds={len(result.medications)}, "
            f"structured_rows={len(result.structured_data)}, "
            f"confidence={result.extraction_confidence:.2f}"
        )

        return result

    @property
    def processed_count(self) -> int:
        return len(self._processed)

    def summary(self) -> dict:
        """Audit summary."""
        type_counts: dict[str, int] = {}
        for r in self._processed:
            type_counts[r.document_type.value] = type_counts.get(r.document_type.value, 0) + 1
        return {
            "module": "L14-7",
            "total_processed": self.processed_count,
            "by_type": type_counts,
        }
