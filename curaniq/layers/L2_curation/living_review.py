"""
CURANIQ — Medical Evidence Operating System
Layer 2: Evidence Knowledge & Synthesis

L2-4  Living Review Engine
       Continuous PRISMA-LSR surveillance, update triggers, version tracking.
"""
from __future__ import annotations
import hashlib, logging, re, uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Optional
logger = logging.getLogger(__name__)


class ReviewUpdateTrigger(str, Enum):
    NEW_RCT              = "new_rct"
    GUIDELINE_UPDATE     = "guideline_update"
    NEW_SAFETY_SIGNAL    = "new_safety_signal"
    RETRACTION_OF_KEY    = "retraction_of_key_study"
    TIME_BASED           = "time_based"
    REGULATORY_ACTION    = "regulatory_action"
    META_ANALYSIS_UPDATE = "meta_analysis_update"

class ReviewStatus(str, Enum):
    CURRENT   = "current"
    DUE       = "due"
    OVERDUE   = "overdue"
    UPDATING  = "updating"
    SUSPENDED = "suspended"


@dataclass
class LivingReviewEntry:
    """A single topic tracked by the Living Review Engine."""
    topic_id:           str = field(default_factory=lambda: str(uuid.uuid4()))
    topic:              str = ""
    mesh_terms:         list[str] = field(default_factory=list)
    review_interval_days: int = 90
    last_review_date:   Optional[datetime] = None
    last_update_date:   Optional[datetime] = None
    status:             ReviewStatus = ReviewStatus.DUE
    version:            int = 1
    update_triggers:    list[ReviewUpdateTrigger] = field(default_factory=list)
    pending_trigger:    Optional[ReviewUpdateTrigger] = None
    key_study_pmids:    list[str] = field(default_factory=list)
    summary_hash:       Optional[str] = None   # SHA-256 of last summary — change detection
    last_summary:       Optional[str] = None
    created_at:         datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def is_due(self) -> bool:
        if self.pending_trigger:
            return True
        if not self.last_review_date:
            return True
        due_date = self.last_review_date + timedelta(days=self.review_interval_days)
        return datetime.now(timezone.utc) >= due_date

    def days_until_due(self) -> int:
        if not self.last_review_date:
            return 0
        due_date = self.last_review_date + timedelta(days=self.review_interval_days)
        delta = (due_date - datetime.now(timezone.utc)).days
        return max(0, delta)

    def mark_reviewed(self, new_summary: str) -> None:
        self.last_review_date = datetime.now(timezone.utc)
        self.last_update_date = datetime.now(timezone.utc)
        new_hash = hashlib.sha256(new_summary.encode()).hexdigest()
        if self.summary_hash and new_hash != self.summary_hash:
            self.version += 1
            logger.info(f"Living review topic '{self.topic}' updated to v{self.version}")
        self.summary_hash = new_hash
        self.last_summary = new_summary
        self.status = ReviewStatus.CURRENT
        self.pending_trigger = None


# Pre-configured high-priority topics from architecture (clinical wedge areas)
DEFAULT_LIVING_REVIEW_TOPICS: list[LivingReviewEntry] = [
    LivingReviewEntry(topic="metformin renal dosing CKD", mesh_terms=["metformin","chronic kidney disease","eGFR","lactic acidosis"], review_interval_days=180, key_study_pmids=["28526737"]),
    LivingReviewEntry(topic="direct oral anticoagulants atrial fibrillation", mesh_terms=["DOAC","NOAC","rivaroxaban","apixaban","dabigatran","atrial fibrillation","stroke prevention"], review_interval_days=180),
    LivingReviewEntry(topic="SGLT2 inhibitors heart failure CKD outcomes", mesh_terms=["dapagliflozin","empagliflozin","canagliflozin","heart failure","CKD"], review_interval_days=90),
    LivingReviewEntry(topic="GLP-1 receptor agonists obesity cardiovascular risk", mesh_terms=["semaglutide","liraglutide","tirzepatide","obesity","cardiovascular"], review_interval_days=60),
    LivingReviewEntry(topic="antibiotic resistance empirical therapy UTI", mesh_terms=["UTI","urinary tract infection","trimethoprim","nitrofurantoin","resistance","empirical"], review_interval_days=90),
    LivingReviewEntry(topic="opioid analgesia chronic non-cancer pain", mesh_terms=["opioid","chronic pain","morphine","oxycodone","dependence"], review_interval_days=180),
    LivingReviewEntry(topic="COVID-19 antiviral treatment immunocompromised", mesh_terms=["COVID-19","nirmatrelvir","molnupiravir","remdesivir","immunocompromised"], review_interval_days=30),
    LivingReviewEntry(topic="statin therapy primary prevention low cardiovascular risk", mesh_terms=["statins","primary prevention","cardiovascular","cholesterol","NNT"], review_interval_days=365),
    LivingReviewEntry(topic="valproate pregnancy teratogenicity risk", mesh_terms=["valproate","pregnancy","teratogenicity","neural tube defects","epilepsy"], review_interval_days=180),
    LivingReviewEntry(topic="QT prolongation drug combinations torsades", mesh_terms=["QT prolongation","torsades de pointes","drug combination","CredibleMeds"], review_interval_days=90),
]


class LivingReviewEngine:
    """
    L2-4: PRISMA-LSR Continuous Surveillance Engine.

    Architecture: 'Continuous surveillance with defined update triggers.
    PRISMA-LSR protocol. Automatic re-query when new RCT published in domain.'

    Monitors high-priority clinical topics for:
    - New RCTs that meet inclusion criteria
    - Guideline updates from NICE/AHA/MOH
    - New safety signals from FDA/MHRA/EMA
    - Retractions of key studies that may invalidate current summary
    - Time-based review cycles (30-365 days depending on domain volatility)
    """

    def __init__(self) -> None:
        self._topics: dict[str, LivingReviewEntry] = {
            t.topic_id: t for t in DEFAULT_LIVING_REVIEW_TOPICS
        }

    # ── Topic Management ──────────────────────────────────────────────────

    def register_topic(self, entry: LivingReviewEntry) -> str:
        self._topics[entry.topic_id] = entry
        logger.info(f"Living review registered: '{entry.topic}' (interval: {entry.review_interval_days}d)")
        return entry.topic_id

    def get_topic(self, topic_id: str) -> Optional[LivingReviewEntry]:
        return self._topics.get(topic_id)

    def list_due(self) -> list[LivingReviewEntry]:
        """Return all topics currently due for review."""
        due = [t for t in self._topics.values() if t.is_due()]
        due.sort(key=lambda t: (t.pending_trigger is None, t.days_until_due()))
        return due

    def list_all(self) -> list[LivingReviewEntry]:
        return sorted(self._topics.values(), key=lambda t: t.topic)

    # ── Update Trigger Handling ───────────────────────────────────────────

    def fire_trigger(
        self,
        topic_id: str,
        trigger: ReviewUpdateTrigger,
        trigger_detail: Optional[str] = None,
    ) -> None:
        """
        Signal that an update trigger has fired for a topic.
        PRISMA-LSR: certain triggers (new RCT, safety signal, retraction)
        require immediate review regardless of schedule.
        """
        topic = self._topics.get(topic_id)
        if not topic:
            logger.warning(f"Trigger fired for unknown topic: {topic_id}")
            return
        topic.pending_trigger = trigger
        topic.status = ReviewStatus.UPDATING
        topic.update_triggers.append(trigger)
        logger.info(
            f"Living review trigger '{trigger.value}' fired for '{topic.topic}'"
            + (f": {trigger_detail}" if trigger_detail else "")
        )

    def detect_trigger_from_new_evidence(
        self,
        new_content: str,
        new_source: str,
    ) -> list[tuple[str, ReviewUpdateTrigger]]:
        """
        Scan new evidence content for signals that should trigger reviews.
        Returns list of (topic_id, trigger) pairs to fire.
        """
        fires: list[tuple[str, ReviewUpdateTrigger]] = []
        content_lower = new_content.lower()

        # Safety signal patterns
        safety_patterns = [
            re.compile(r'\b(black box|boxed warning|recall|market withdrawal|safety alert)\b', re.I),
            re.compile(r'\b(contraindicated|fatal|death|serious adverse|life.threatening)\b', re.I),
        ]
        is_safety_signal = any(p.search(content_lower) for p in safety_patterns)

        # Retraction patterns
        is_retraction = bool(re.search(r'\b(retracted|retraction|withdrawn)\b', content_lower))

        # RCT patterns
        is_new_rct = bool(re.search(r'\b(randomized|randomised|rct|clinical trial)\b', content_lower))

        # Guideline patterns
        is_guideline = bool(re.search(r'\b(guideline|nice|aha|acc|who|moh|recommendation)\b', content_lower))

        for topic in self._topics.values():
            # Check if new evidence is relevant to this topic
            relevant = any(
                term.lower() in content_lower
                for term in topic.mesh_terms
            )
            if not relevant:
                continue

            if is_retraction:
                fires.append((topic.topic_id, ReviewUpdateTrigger.RETRACTION_OF_KEY))
            elif is_safety_signal:
                fires.append((topic.topic_id, ReviewUpdateTrigger.NEW_SAFETY_SIGNAL))
            elif is_new_rct:
                fires.append((topic.topic_id, ReviewUpdateTrigger.NEW_RCT))
            elif is_guideline:
                fires.append((topic.topic_id, ReviewUpdateTrigger.GUIDELINE_UPDATE))

        return fires

    def acknowledge_review(
        self,
        topic_id: str,
        new_summary: str,
    ) -> None:
        """Mark a topic as reviewed with updated summary."""
        topic = self._topics.get(topic_id)
        if topic:
            topic.mark_reviewed(new_summary)

    # ── PRISMA-LSR Protocol ───────────────────────────────────────────────

    def get_prisma_search_protocol(self, topic: LivingReviewEntry) -> dict:
        """
        Generate PRISMA-LSR search protocol for a topic.
        Returns search strategy for PubMed/Cochrane/NICE.
        """
        return {
            "topic": topic.topic,
            "mesh_terms": topic.mesh_terms,
            "search_string": " AND ".join(f'"{t}"[MeSH]' for t in topic.mesh_terms[:4]),
            "filters": {
                "publication_types": ["Randomized Controlled Trial", "Systematic Review", "Meta-Analysis", "Practice Guideline"],
                "date_range": f"{(datetime.now(timezone.utc) - timedelta(days=topic.review_interval_days)).strftime('%Y/%m/%d')}:3000/01/01",
                "languages": ["English", "Russian"],
            },
            "inclusion_criteria": [
                "RCTs with ≥100 participants",
                "Systematic reviews with ≥3 RCTs",
                "NICE/AHA/ACC/WHO guidelines",
                "FDA/MHRA safety communications",
            ],
            "exclusion_criteria": [
                "Preprints (unless safety signal)",
                "Case reports/series (n<10)",
                "Animal studies",
                "Non-peer reviewed",
            ],
            "protocol_version": "PRISMA-LSR 2020",
        }

    def dashboard(self) -> dict:
        """Return dashboard data for monitoring interface."""
        all_topics = list(self._topics.values())
        due = self.list_due()
        return {
            "total_topics": len(all_topics),
            "due_for_review": len(due),
            "with_pending_trigger": sum(1 for t in all_topics if t.pending_trigger),
            "current": sum(1 for t in all_topics if t.status == ReviewStatus.CURRENT),
            "topics_due": [
                {
                    "topic": t.topic,
                    "trigger": t.pending_trigger.value if t.pending_trigger else "time_based",
                    "version": t.version,
                    "last_reviewed": t.last_review_date.isoformat() if t.last_review_date else "never",
                    "days_until_due": t.days_until_due(),
                }
                for t in due[:10]
            ],
        }
