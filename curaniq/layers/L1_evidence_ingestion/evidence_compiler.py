"""
CURANIQ — Medical Evidence Operating System
Layer 1: Evidence Ingestion

L1-2  Evidence Compiler (FHIR/PICO extraction, HL7 EBMonFHIR)
L1-3  Negative Evidence Registry
L1-12 Cochrane Library API Integration
"""
from __future__ import annotations
import asyncio, hashlib, logging, re, uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
import aiohttp
from curaniq.models.evidence import (
    EvidenceChunk, EvidenceProvenanceChain, EvidenceTier,
    Jurisdiction, RetractionStatus, SourceAPI, StalenessStatus, STALENESS_TTL_HOURS,
)
logger = logging.getLogger(__name__)
PIPELINE_VERSION = "1.0.0"

# ─────────────────────────────────────────────────────────────────────────────
# L1-2: EVIDENCE COMPILER — FHIR/PICO EXTRACTION
# Architecture: 'Extracts FHIR Evidence/EvidenceReport resources.
# PICO extraction. HL7 EBMonFHIR alignment.'
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class PICOFrame:
    """Population-Intervention-Comparator-Outcome structured extraction."""
    population:    Optional[str] = None
    intervention:  Optional[str] = None
    comparator:    Optional[str] = None
    outcome:       Optional[str] = None
    timeframe:     Optional[str] = None
    setting:       Optional[str] = None
    study_design:  Optional[str] = None
    effect_size:   Optional[str] = None
    confidence_interval: Optional[str] = None
    p_value:       Optional[str] = None
    sample_size:   Optional[int] = None
    nnT:           Optional[float] = None   # Number Needed to Treat
    nnH:           Optional[float] = None   # Number Needed to Harm
    arr:           Optional[float] = None   # Absolute Risk Reduction
    rr:            Optional[float] = None   # Relative Risk
    or_:           Optional[float] = None   # Odds Ratio

    def is_complete(self) -> bool:
        return bool(self.population and self.intervention and self.outcome)

    def to_summary(self) -> str:
        parts = []
        if self.population:    parts.append(f"P: {self.population}")
        if self.intervention:  parts.append(f"I: {self.intervention}")
        if self.comparator:    parts.append(f"C: {self.comparator}")
        if self.outcome:       parts.append(f"O: {self.outcome}")
        if self.effect_size:   parts.append(f"Effect: {self.effect_size}")
        if self.nnT:           parts.append(f"NNT: {self.nnT:.1f}")
        if self.arr:           parts.append(f"ARR: {self.arr:.1%}")
        return " | ".join(parts)


# PICO extraction patterns
_POPULATION_PATTERNS = [
    re.compile(r'(?:adults?|patients?|participants?|subjects?|individuals?)\s+(?:with|who have|aged?)\s+([^,.;\n]{5,80})', re.I),
    re.compile(r'(?:in|among)\s+(\d[\d,\s]*(?:adults?|patients?|men|women|children))\s+with\s+([^,.;\n]{5,60})', re.I),
    re.compile(r'(?:n\s*=\s*|enrolled\s+)(\d[\d,\s]*)\s+(?:patients?|participants?)', re.I),
]
_INTERVENTION_PATTERNS = [
    re.compile(r'(?:treated?\s+with|received?|assigned?\s+to|randomized?\s+to)\s+([^,.;\n]{3,60})', re.I),
    re.compile(r'(?:treatment|therapy|intervention|drug|medication)[:\s]+([^,.;\n]{3,60})', re.I),
]
_OUTCOME_PATTERNS = [
    re.compile(r'(?:primary\s+(?:endpoint|outcome|efficacy))[:\s]+([^,.;\n]{5,100})', re.I),
    re.compile(r'(?:significant(?:ly)?|reduced?|decreased?|improved?|associated\s+with)\s+([^,.;\n]{5,80})', re.I),
    re.compile(r'(?:mortality|morbidity|survival|hospitali[sz]ation|mi|stroke|death)\s+(?:rate|risk|was)', re.I),
]
_EFFECT_PATTERNS = [
    re.compile(r'(?:HR|RR|OR|ARR|NNT|NNH|RD)\s*[=:]\s*([\d.]+\s*(?:\([\d.,\s%-]+\))?)', re.I),
    re.compile(r'(?:hazard ratio|relative risk|odds ratio)[:\s]+([\d.]+)', re.I),
    re.compile(r'p\s*[<>=]\s*([\d.]+(?:e-\d+)?)', re.I),
]
_NNT_PATTERN = re.compile(r'NNT\s*[=:]\s*([\d.]+)', re.I)
_NNH_PATTERN = re.compile(r'NNH\s*[=:]\s*([\d.]+)', re.I)
_ARR_PATTERN = re.compile(r'ARR\s*[=:]\s*([\d.]+)\s*%?', re.I)
_SAMPLE_PATTERN = re.compile(r'(?:n\s*=\s*|enrolled\s+|included\s+)([\d,]+)\s+(?:patients?|participants?)', re.I)


def extract_pico(content: str) -> PICOFrame:
    """Extract PICO frame from clinical text. Production: use BioBERT NER."""
    frame = PICOFrame()
    content_clean = content[:3000]  # Limit to first 3000 chars

    for pat in _POPULATION_PATTERNS:
        m = pat.search(content_clean)
        if m:
            frame.population = m.group(1).strip()[:120]
            break

    for pat in _INTERVENTION_PATTERNS:
        m = pat.search(content_clean)
        if m:
            frame.intervention = m.group(1).strip()[:120]
            break

    for pat in _OUTCOME_PATTERNS:
        m = pat.search(content_clean)
        if m:
            frame.outcome = (m.group(1) if m.lastindex and m.lastindex >= 1 else m.group(0)).strip()[:200]
            break

    for pat in _EFFECT_PATTERNS:
        m = pat.search(content_clean)
        if m:
            frame.effect_size = m.group(1).strip()[:80]
            break

    m = _NNT_PATTERN.search(content_clean)
    if m:
        try: frame.nnT = float(m.group(1))
        except ValueError: pass

    m = _NNH_PATTERN.search(content_clean)
    if m:
        try: frame.nnH = float(m.group(1))
        except ValueError: pass

    m = _ARR_PATTERN.search(content_clean)
    if m:
        try: frame.arr = float(m.group(1)) / 100.0
        except ValueError: pass

    m = _SAMPLE_PATTERN.search(content_clean)
    if m:
        try: frame.sample_size = int(m.group(1).replace(',', ''))
        except ValueError: pass

    # Study design detection
    design_map = [
        ('systematic review', EvidenceTier.SYSTEMATIC_REVIEW),
        ('meta-analysis', EvidenceTier.SYSTEMATIC_REVIEW),
        ('randomised controlled', EvidenceTier.RCT),
        ('randomized controlled', EvidenceTier.RCT),
        ('cohort study', EvidenceTier.COHORT),
        ('case-control', EvidenceTier.COHORT),
        ('guideline', EvidenceTier.GUIDELINE),
    ]
    cl = content_clean.lower()
    for term, _ in design_map:
        if term in cl:
            frame.study_design = term
            break

    return frame


@dataclass
class FHIREvidence:
    """HL7 FHIR R4 Evidence resource (EBMonFHIR alignment)."""
    resource_id:        str = field(default_factory=lambda: str(uuid.uuid4()))
    resource_type:      str = "Evidence"
    title:              Optional[str] = None
    status:             str = "active"
    pico:               Optional[PICOFrame] = None
    certainty_rating:   Optional[str] = None   # GRADE: high/moderate/low/very-low
    evidence_tier:      Optional[EvidenceTier] = None
    doi:                Optional[str] = None
    pmid:               Optional[str] = None
    publication_date:   Optional[datetime] = None
    jurisdiction:       Jurisdiction = Jurisdiction.INTL
    raw_chunk_id:       Optional[str] = None

    def to_fhir_json(self) -> dict:
        """Serialize to FHIR R4 Evidence resource JSON."""
        resource: dict[str, Any] = {
            "resourceType": "Evidence",
            "id": self.resource_id,
            "status": self.status,
        }
        if self.title:
            resource["title"] = self.title
        if self.doi:
            resource["identifier"] = [{"system": "https://doi.org", "value": self.doi}]
        if self.pmid:
            resource.setdefault("identifier", []).append(
                {"system": "https://pubmed.ncbi.nlm.nih.gov", "value": self.pmid}
            )
        if self.pico:
            resource["variableDefinition"] = []
            if self.pico.population:
                resource["variableDefinition"].append({
                    "variableRole": "population",
                    "description": self.pico.population
                })
            if self.pico.intervention:
                resource["variableDefinition"].append({
                    "variableRole": "exposure",
                    "description": self.pico.intervention
                })
            if self.pico.outcome:
                resource["variableDefinition"].append({
                    "variableRole": "measuredVariable",
                    "description": self.pico.outcome
                })
        if self.certainty_rating:
            resource["certainty"] = [{"rating": [{"text": self.certainty_rating}]}]
        return resource


class EvidenceCompiler:
    """
    L1-2: Evidence Compiler.
    Extracts FHIR Evidence/EvidenceReport resources and PICO frames
    from raw evidence chunks. HL7 EBMonFHIR alignment.
    """

    def compile(self, chunk: EvidenceChunk) -> FHIREvidence:
        """Compile an EvidenceChunk into a FHIR Evidence resource with PICO."""
        pico = extract_pico(chunk.content)

        # Extract PMID/DOI
        pmid = self._extract_pmid(chunk.content)
        doi = chunk.provenance.source_doi or self._extract_doi(chunk.content)

        # Extract title
        title = self._extract_title(chunk.content)

        return FHIREvidence(
            title=title,
            pico=pico,
            evidence_tier=chunk.evidence_tier,
            doi=doi,
            pmid=pmid,
            publication_date=chunk.provenance.publication_date,
            jurisdiction=chunk.provenance.jurisdiction,
            raw_chunk_id=chunk.chunk_id,
        )

    def compile_batch(self, chunks: list[EvidenceChunk]) -> list[FHIREvidence]:
        return [self.compile(c) for c in chunks]

    def _extract_pmid(self, content: str) -> Optional[str]:
        m = re.search(r'PMID[:\s]+(\d{6,9})', content, re.I)
        return m.group(1) if m else None

    def _extract_doi(self, content: str) -> Optional[str]:
        m = re.search(r'(?:doi:|10\.)(\S+)', content, re.I)
        if m:
            doi = m.group(0).lstrip('doi:').rstrip('.,;)')
            return doi
        return None

    def _extract_title(self, content: str) -> Optional[str]:
        lines = content.split('\n')
        for line in lines[:5]:
            line = line.strip()
            if 20 <= len(line) <= 250 and not line.startswith('http'):
                return line
        return None


# ─────────────────────────────────────────────────────────────────────────────
# L1-3: NEGATIVE EVIDENCE REGISTRY
# Architecture: 'Indexes failed trials, null results, negative outcomes.
# Prevents publication bias. Choosing Wisely "do not do" recommendations.'
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class NegativeEvidenceRecord:
    """A registered negative finding — null result, failed trial, 'do not do'."""
    record_id:         str = field(default_factory=lambda: str(uuid.uuid4()))
    study_type:        str = ""       # "null_rct", "negative_cohort", "do_not_do", "harm_signal"
    intervention:      str = ""
    population:        str = ""
    outcome:           str = ""
    finding:           str = ""       # What was NOT shown / found harmful
    doi:               Optional[str] = None
    pmid:              Optional[str] = None
    source:            str = ""
    registered_at:     datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    choosing_wisely:   bool = False   # From Choosing Wisely "do not do" list
    harm_signal:       bool = False   # Evidence of harm (not just null result)
    harm_description:  Optional[str] = None


# Choosing Wisely "do not do" — seed data (production: full API sync)
CHOOSING_WISELY_DO_NOT_DO: list[NegativeEvidenceRecord] = [
    NegativeEvidenceRecord(
        study_type="do_not_do",
        intervention="antibiotics for viral upper respiratory infections",
        population="adults with uncomplicated URI/common cold",
        outcome="symptom resolution",
        finding="Antibiotics do not reduce duration or severity of viral URI. Harms: adverse effects, resistance.",
        source="Choosing Wisely / IDSA",
        choosing_wisely=True,
    ),
    NegativeEvidenceRecord(
        study_type="do_not_do",
        intervention="PSA screening in men >75 years",
        population="men aged over 75 without prostate cancer symptoms",
        outcome="mortality reduction",
        finding="Routine PSA screening in men >75 does not reduce mortality and causes significant harm from over-diagnosis.",
        source="Choosing Wisely / USPSTF",
        choosing_wisely=True,
    ),
    NegativeEvidenceRecord(
        study_type="do_not_do",
        intervention="routine preoperative tests (CBC, ECG) for low-risk surgery",
        population="patients undergoing low-risk elective surgery",
        outcome="surgical outcome improvement",
        finding="Routine preoperative testing does not improve outcomes in low-risk surgery patients.",
        source="Choosing Wisely / ASA",
        choosing_wisely=True,
    ),
    NegativeEvidenceRecord(
        study_type="do_not_do",
        intervention="imaging for uncomplicated low back pain within first 6 weeks",
        population="adults with acute uncomplicated low back pain <6 weeks",
        outcome="clinical outcome improvement",
        finding="Early imaging does not improve outcomes and may lead to unnecessary interventions.",
        source="Choosing Wisely / ACR",
        choosing_wisely=True,
    ),
    NegativeEvidenceRecord(
        study_type="do_not_do",
        intervention="benzodiazepines as first-line for insomnia in elderly",
        population="adults ≥65 with insomnia",
        outcome="safe sleep improvement",
        finding="Benzodiazepines in elderly: risk of falls, fractures, cognitive impairment outweighs sleep benefit.",
        source="AGS Beers Criteria / Choosing Wisely",
        choosing_wisely=True,
        harm_signal=True,
        harm_description="Falls, hip fractures, delirium, cognitive impairment",
    ),
    NegativeEvidenceRecord(
        study_type="do_not_do",
        intervention="COX-2 inhibitors or NSAIDs post-MI or with heart failure",
        population="patients with recent MI or heart failure",
        outcome="pain relief without cardiac harm",
        finding="NSAIDs/COX-2 inhibitors post-MI increase risk of recurrent MI and worsen heart failure.",
        source="ESC / FDA / NICE",
        choosing_wisely=True,
        harm_signal=True,
        harm_description="Increased MI risk, fluid retention, worsening HF, hypertension",
    ),
    NegativeEvidenceRecord(
        study_type="null_rct",
        intervention="hormone replacement therapy for cardiovascular prevention",
        population="postmenopausal women for primary CV prevention",
        outcome="cardiovascular event reduction",
        finding="WHI trial: HRT does not prevent CVD and increases risk of breast cancer, VTE, stroke.",
        source="WHI Trial / Lancet",
        choosing_wisely=False,
        harm_signal=True,
        harm_description="Breast cancer, VTE, stroke increased",
        doi="10.1001/jama.288.3.321",
    ),
    NegativeEvidenceRecord(
        study_type="do_not_do",
        intervention="routine vitamin D supplementation without deficiency",
        population="adults without vitamin D deficiency",
        outcome="fracture prevention, CV benefit",
        finding="VITAL trial: vitamin D supplementation did not reduce fractures or major CV events in those without deficiency.",
        source="VITAL Trial / NEJM",
        choosing_wisely=False,
        harm_signal=False,
    ),
    NegativeEvidenceRecord(
        study_type="do_not_do",
        intervention="metformin in eGFR <30 mL/min/1.73m²",
        population="patients with CKD stage 4-5 (eGFR <30)",
        outcome="glycaemic control without lactic acidosis",
        finding="Metformin contraindicated in eGFR <30: risk of lactic acidosis. Stop if eGFR falls below 30.",
        source="FDA / MHRA / NICE",
        choosing_wisely=False,
        harm_signal=True,
        harm_description="Lactic acidosis — potentially fatal",
    ),
    NegativeEvidenceRecord(
        study_type="harm_signal",
        intervention="fluoroquinolones for uncomplicated UTI in elderly",
        population="elderly patients with uncomplicated UTI",
        outcome="infection treatment without serious adverse effects",
        finding="FDA Black Box: fluoroquinolones cause tendinopathy, peripheral neuropathy, aortic aneurysm — avoid for uncomplicated infections.",
        source="FDA Black Box Warning",
        choosing_wisely=True,
        harm_signal=True,
        harm_description="Tendon rupture, peripheral neuropathy, aortic dissection",
    ),
]


class NegativeEvidenceRegistry:
    """
    L1-3: Registry of negative findings, failed trials, null results.
    Prevents publication bias by ensuring CURANIQ is aware of what DOESN'T work.
    Critical for 'do not do' recommendations and harm signals.
    """

    def __init__(self) -> None:
        self._records: list[NegativeEvidenceRecord] = list(CHOOSING_WISELY_DO_NOT_DO)
        self._index: dict[str, list[NegativeEvidenceRecord]] = {}
        self._rebuild_index()

    def _rebuild_index(self) -> None:
        self._index = {}
        for r in self._records:
            for term in self._extract_terms(r.intervention + " " + r.population):
                self._index.setdefault(term, []).append(r)

    def _extract_terms(self, text: str) -> list[str]:
        words = re.findall(r'\b[a-z]{4,}\b', text.lower())
        return list(set(words))[:20]

    def register(self, record: NegativeEvidenceRecord) -> None:
        self._records.append(record)
        self._rebuild_index()

    def search(self, query: str, include_harm_only: bool = False) -> list[NegativeEvidenceRecord]:
        """Search negative evidence registry for a clinical query."""
        query_terms = set(re.findall(r'\b[a-z]{4,}\b', query.lower()))
        scored: dict[str, tuple[NegativeEvidenceRecord, int]] = {}

        for term in query_terms:
            for record in self._index.get(term, []):
                rid = record.record_id
                if rid not in scored:
                    scored[rid] = (record, 0)
                scored[rid] = (record, scored[rid][1] + 1)

        results = [r for r, score in sorted(scored.values(), key=lambda x: -x[1])]
        if include_harm_only:
            results = [r for r in results if r.harm_signal]
        return results[:10]

    def get_do_not_do(self, intervention: str) -> list[NegativeEvidenceRecord]:
        """Get Choosing Wisely 'do not do' records matching an intervention."""
        results = self.search(intervention)
        return [r for r in results if r.choosing_wisely]

    def get_harm_signals(self, drug_name: str) -> list[NegativeEvidenceRecord]:
        """Get known harm signals for a drug."""
        results = self.search(drug_name)
        return [r for r in results if r.harm_signal]

    def format_for_output(self, records: list[NegativeEvidenceRecord]) -> str:
        """Format negative evidence for clinical output."""
        if not records:
            return ""
        lines = ["⚠️ NEGATIVE EVIDENCE / DO NOT DO:"]
        for r in records[:5]:
            prefix = "🚫 DO NOT DO" if r.choosing_wisely else ("⚠️ HARM SIGNAL" if r.harm_signal else "❌ NULL RESULT")
            lines.append(f"{prefix}: {r.intervention}")
            lines.append(f"   Finding: {r.finding}")
            if r.harm_description:
                lines.append(f"   Harms: {r.harm_description}")
            lines.append(f"   Source: {r.source}")
        return "\n".join(lines)

    @property
    def total_records(self) -> int:
        return len(self._records)


# ─────────────────────────────────────────────────────────────────────────────
# L1-12: COCHRANE LIBRARY API INTEGRATION
# Architecture: 'Gold-standard systematic reviews. Structured PICO extraction.
# Plain language summaries for patient-facing outputs.
# Quality assured — highest evidence tier after guidelines.'
# ─────────────────────────────────────────────────────────────────────────────

class CochraneConnector:
    """
    L1-12: Cochrane Library API connector.
    Cochrane systematic reviews = highest evidence tier (after clinical guidelines).
    Every Cochrane review automatically receives GRADE HIGH starting point.
    PICO extracted from structured review data.
    """
    BASE_URL = "https://www.cochranelibrary.com/api"
    SOURCE = SourceAPI.COCHRANE

    def __init__(self, api_key: Optional[str] = None) -> None:
        self.api_key = api_key
        self._last_fetch: Optional[datetime] = None

    async def search(self, query: str, max_results: int = 10) -> list[EvidenceChunk]:
        """Search Cochrane Library for systematic reviews."""
        chunks = []
        try:
            async with aiohttp.ClientSession() as session:
                params = {
                    "q": query,
                    "rows": max_results,
                    "start": 0,
                    "type": "review",
                }
                if self.api_key:
                    params["api_key"] = self.api_key
                headers = {"Accept": "application/json", "User-Agent": "CURANIQ/1.0 (safety@curaniq.com)"}

                # Cochrane REST API search
                async with session.get(
                    f"{self.BASE_URL}/search/results",
                    params=params, headers=headers,
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status != 200:
                        logger.warning(f"Cochrane API returned {resp.status} for: {query}")
                        return self._fallback_pubmed_cochrane(query)
                    data = await resp.json()

                self._last_fetch = datetime.now(timezone.utc)
                results = data.get("results", data.get("rows", []))
                for item in results[:max_results]:
                    chunk = self._parse_review(item)
                    if chunk:
                        chunks.append(chunk)
        except Exception as e:
            logger.warning(f"Cochrane API error: {e}. Using PubMed fallback.")
            return self._fallback_pubmed_cochrane(query)

        logger.info(f"Cochrane: {len(chunks)} systematic reviews for '{query}'")
        return chunks

    def _parse_review(self, item: dict) -> Optional[EvidenceChunk]:
        try:
            title = item.get("title", "")
            abstract = item.get("abstract", item.get("plainLanguageSummary", ""))
            doi = item.get("doi", "")
            pub_date_str = item.get("publishedDate", "")
            cochrane_id = item.get("id", item.get("cdNumber", str(uuid.uuid4())))

            if not abstract:
                return None

            # Cochrane reviews have plain language summaries — extract both
            pls = item.get("plainLanguageSummary", "")
            content = f"COCHRANE SYSTEMATIC REVIEW: {title}\n\n"
            if abstract:
                content += f"ABSTRACT:\n{abstract}\n\n"
            if pls and pls != abstract:
                content += f"PLAIN LANGUAGE SUMMARY:\n{pls}"

            content_bytes = content.encode("utf-8")
            snippet_hash = hashlib.sha256(content_bytes).hexdigest()

            pub_date = None
            if pub_date_str:
                try:
                    pub_date = datetime.fromisoformat(pub_date_str.replace("Z", "+00:00"))
                except ValueError:
                    pass

            # Extract PICO
            pico = extract_pico(content)

            provenance = EvidenceProvenanceChain(
                source_api=self.SOURCE,
                retrieval_timestamp=datetime.now(timezone.utc),
                document_version=f"Cochrane:{cochrane_id}",
                snippet_hash=snippet_hash,
                ingestion_pipeline_version=PIPELINE_VERSION,
                source_doi=doi or None,
                publication_date=pub_date,
                jurisdiction=Jurisdiction.INTL,
                evidence_tier=EvidenceTier.SYSTEMATIC_REVIEW,
                chunk_position=0,
                parent_document_id=f"cochrane:{cochrane_id}",
            )
            return EvidenceChunk(
                content=content,
                content_bytes=content_bytes,
                provenance=provenance,
                retraction_status=RetractionStatus.UNCHECKED,
                evidence_tier=EvidenceTier.SYSTEMATIC_REVIEW,
                staleness_status=StalenessStatus.FRESH,
                staleness_ttl_hours=STALENESS_TTL_HOURS[self.SOURCE],
                last_verified=datetime.now(timezone.utc),
                pico_population=pico.population,
                pico_intervention=pico.intervention,
                pico_comparator=pico.comparator,
                pico_outcome=pico.outcome,
            )
        except Exception as e:
            logger.error(f"Cochrane parse error: {e}")
            return None

    def _fallback_pubmed_cochrane(self, query: str) -> list[EvidenceChunk]:
        """
        Fallback: return empty list with warning.
        Production: cache last successful Cochrane results and serve from cache.
        """
        logger.warning(f"Cochrane API unavailable — no systematic review data for: {query}")
        return []

    def check_staleness(self) -> StalenessStatus:
        if not self._last_fetch:
            return StalenessStatus.UNKNOWN
        age_hours = (datetime.now(timezone.utc) - self._last_fetch).total_seconds() / 3600
        ttl = STALENESS_TTL_HOURS[self.SOURCE]
        if age_hours <= ttl:
            return StalenessStatus.FRESH
        return StalenessStatus.STALE
