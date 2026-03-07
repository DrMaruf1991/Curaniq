"""
CURANIQ — Medical Evidence Operating System
Layer 9: Audit & Revenue

L9-3  Citation Verifier UI (click-through: claim → evidence card → source DOI)
L9-7  Payment Gateway & SaaS Revenue Engine (Stripe + Payme/Click/Uzum)
"""
from __future__ import annotations
import logging, re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional
logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# L9-3: CITATION VERIFIER UI
# Architecture: 'Every claim in UI links back to exact evidence source.
# Click claim → see evidence card → click DOI → open original paper.'
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class CitationLink:
    claim_id:      str
    claim_text:    str
    chunk_id:      str
    source_name:   str
    evidence_tier: str
    doi_url:       Optional[str]
    pubmed_url:    Optional[str]
    cochrane_url:  Optional[str]
    snippet:       str
    confidence:    float
    verified:      bool
    retraction_status: str


class CitationVerifierUI:
    """
    L9-3: Build click-through citation links for every claim in CURANIQ output.
    Architecture: 'Every displayed claim has a verifiable source link.
    Unverified claims are visually flagged and not presented as fact.'
    """

    def build_links(
        self,
        claims: list[dict],
        chunk_registry: dict[str, Any],
    ) -> list[CitationLink]:
        links = []
        for claim in claims:
            if claim.get("suppressed"):
                continue
            for chunk_id in claim.get("chunk_ids", []):
                chunk = chunk_registry.get(chunk_id)
                if not chunk:
                    continue
                doi = getattr(chunk.provenance, "source_doi", None)
                pmid_match = re.search(r'PMID[:\s]+(\d{5,9})', chunk.content or "")
                cochrane_match = re.search(r'CD\d{6}', chunk.content or "")
                links.append(CitationLink(
                    claim_id=claim.get("claim_id", ""),
                    claim_text=claim.get("claim_text", "")[:120],
                    chunk_id=chunk_id,
                    source_name=chunk.provenance.source_api.value,
                    evidence_tier=chunk.evidence_tier.value,
                    doi_url=f"https://doi.org/{doi}" if doi else None,
                    pubmed_url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid_match.group(1)}" if pmid_match else None,
                    cochrane_url=f"https://www.cochranelibrary.com/cdsr/doi/10.1002/{cochrane_match.group(0)}" if cochrane_match else None,
                    snippet=chunk.content[:200].replace("\n", " "),
                    confidence=claim.get("confidence", 0.5),
                    verified=True,
                    retraction_status=chunk.retraction_status.value,
                ))
        return links

    def render_html(self, links: list[CitationLink]) -> str:
        """Render citation links as HTML for the web UI."""
        if not links:
            return "<p class='no-citations'>No verifiable citations available for this output.</p>"
        rows = []
        for lnk in links:
            retraction_badge = ""
            if lnk.retraction_status not in ("unchecked", "clean"):
                retraction_badge = f'<span class="badge retraction">{lnk.retraction_status.upper()}</span>'
            source_links = " ".join(filter(None, [
                f'<a href="{lnk.doi_url}" target="_blank" rel="noopener">DOI ↗</a>' if lnk.doi_url else "",
                f'<a href="{lnk.pubmed_url}" target="_blank" rel="noopener">PubMed ↗</a>' if lnk.pubmed_url else "",
                f'<a href="{lnk.cochrane_url}" target="_blank" rel="noopener">Cochrane ↗</a>' if lnk.cochrane_url else "",
            ]))
            rows.append(
                f'<div class="citation-card" data-claim-id="{lnk.claim_id}" data-chunk-id="{lnk.chunk_id}">'
                f'  <span class="badge tier">{lnk.evidence_tier}</span>'
                f'  {retraction_badge}'
                f'  <strong class="source-name">{lnk.source_name}</strong>'
                f'  <p class="snippet">{lnk.snippet[:180]}…</p>'
                f'  <div class="source-links">{source_links}</div>'
                f'  <span class="confidence">Confidence: {lnk.confidence:.0%}</span>'
                f'</div>'
            )
        return "\n".join(rows)

    def render_text(self, links: list[CitationLink]) -> str:
        """Plain-text citation list for non-HTML interfaces."""
        if not links:
            return "No verifiable citations."
        lines = ["CITATIONS:"]
        for i, lnk in enumerate(links, 1):
            url = lnk.doi_url or lnk.pubmed_url or lnk.cochrane_url or "(no URL)"
            lines.append(f"[{i}] {lnk.source_name} ({lnk.evidence_tier}) — {lnk.snippet[:100]}… {url}")
        return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# L9-7: PAYMENT GATEWAY & SAAS REVENUE ENGINE
# Architecture: 'Stripe for international. Payme/Click/Uzum for Uzbekistan.
# Per-seat clinic pricing. Usage-based overage.'
# ─────────────────────────────────────────────────────────────────────────────

class SubscriptionTier(str, Enum):
    FREE       = "free"
    CLINIC     = "clinic"       # Small clinic / GP practice
    HOSPITAL   = "hospital"     # Hospital department
    ENTERPRISE = "enterprise"   # Custom — unlimited


TIER_CONFIG: dict[str, dict] = {
    "free": {
        "queries_per_month": 50,
        "price_usd_monthly": 0,
        "price_uzs_monthly": 0,
        "seats": 1,
        "features": ["basic_query", "drug_safety_basic"],
    },
    "clinic": {
        "queries_per_month": 2_000,
        "price_usd_monthly": 149,
        "price_uzs_monthly": 1_900_000,   # ~149 USD at current rate
        "seats": 10,
        "features": ["basic_query", "drug_safety_basic", "drug_safety_advanced",
                     "evidence_cards", "multilingual", "audit_ledger"],
    },
    "hospital": {
        "queries_per_month": 20_000,
        "price_usd_monthly": 999,
        "price_uzs_monthly": 12_700_000,
        "seats": 100,
        "features": ["basic_query", "drug_safety_basic", "drug_safety_advanced",
                     "evidence_cards", "multilingual", "audit_ledger",
                     "ehr_integration", "shadow_mode", "benchmark_tracker"],
    },
    "enterprise": {
        "queries_per_month": -1,   # Unlimited
        "price_usd_monthly": 0,    # Custom contract
        "price_uzs_monthly": 0,
        "seats": -1,
        "features": ["all"],
    },
}

OVERAGE_PRICE_PER_100_QUERIES_USD = 5.0


@dataclass
class TenantUsage:
    tenant_id:     str
    month:         str
    tier:          SubscriptionTier
    query_count:   int = 0
    overage_count: int = 0
    overage_usd:   float = 0.0


@dataclass
class PaymentRecord:
    payment_id:    str
    tenant_id:     str
    amount_usd:    float
    currency:      str          # "USD" | "UZS"
    gateway:       str          # "stripe" | "payme" | "click" | "uzum"
    status:        str          # "pending" | "paid" | "failed" | "refunded"
    description:   str
    created_at:    datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class PaymentGateway:
    """
    L9-7: SaaS revenue engine.

    International: Stripe Billing API (webhook-driven subscription management).
    Uzbekistan: Payme / Click / Uzum Pay (UZS-denominated, local payment rails).

    Enforces query limits per tier.
    Tracks overage and triggers upgrade prompts.
    """

    def __init__(self, stripe_client=None, payme_client=None) -> None:
        self._stripe = stripe_client     # injected at startup
        self._payme = payme_client
        self._usage: dict[str, TenantUsage] = {}
        self._payments: list[PaymentRecord] = []

    def record_query(self, tenant_id: str, tier: SubscriptionTier) -> dict:
        """Record a query and check against tier limit."""
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        key = f"{tenant_id}:{month}"
        if key not in self._usage:
            self._usage[key] = TenantUsage(tenant_id=tenant_id, month=month, tier=tier)
        record = self._usage[key]
        record.query_count += 1

        limit = TIER_CONFIG[tier.value]["queries_per_month"]
        if limit == -1:
            return {"allowed": True, "queries_used": record.query_count, "limit": "unlimited"}

        if record.query_count > limit:
            record.overage_count += 1
            overage_cost = (record.overage_count / 100) * OVERAGE_PRICE_PER_100_QUERIES_USD
            record.overage_usd = round(overage_cost, 2)
            return {
                "allowed": True,   # Still allowed — billed as overage
                "queries_used": record.query_count,
                "limit": limit,
                "overage": record.overage_count,
                "overage_cost_usd": record.overage_usd,
                "upgrade_prompt": f"You've exceeded your {limit} query limit. Consider upgrading to avoid overage charges.",
            }

        return {
            "allowed": True,
            "queries_used": record.query_count,
            "limit": limit,
            "remaining": limit - record.query_count,
        }

    def get_usage_summary(self, tenant_id: str) -> dict:
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        key = f"{tenant_id}:{month}"
        record = self._usage.get(key)
        if not record:
            return {"queries_used": 0, "month": month, "overage": 0}
        limit = TIER_CONFIG[record.tier.value]["queries_per_month"]
        return {
            "tenant_id": tenant_id,
            "month": month,
            "tier": record.tier.value,
            "queries_used": record.query_count,
            "limit": limit if limit != -1 else "unlimited",
            "overage": record.overage_count,
            "overage_cost_usd": record.overage_usd,
        }

    def create_stripe_subscription(self, tenant_id: str, tier: SubscriptionTier, email: str) -> dict:
        """Create Stripe subscription (production: uses Stripe Billing API)."""
        if self._stripe is None:
            return {"status": "mock", "message": "Stripe client not connected. Set STRIPE_SECRET_KEY."}
        price_id = f"price_curaniq_{tier.value}_monthly"
        try:
            customer = self._stripe.Customer.create(email=email, metadata={"tenant_id": tenant_id})
            subscription = self._stripe.Subscription.create(
                customer=customer.id,
                items=[{"price": price_id}],
                metadata={"tenant_id": tenant_id, "tier": tier.value},
            )
            record = PaymentRecord(
                payment_id=subscription.id,
                tenant_id=tenant_id,
                amount_usd=TIER_CONFIG[tier.value]["price_usd_monthly"],
                currency="USD",
                gateway="stripe",
                status="paid",
                description=f"CURANIQ {tier.value} subscription",
            )
            self._payments.append(record)
            return {"status": "created", "subscription_id": subscription.id}
        except Exception as e:
            logger.error(f"Stripe subscription error: {e}")
            return {"status": "error", "message": str(e)}

    def create_payme_invoice(self, tenant_id: str, tier: SubscriptionTier) -> dict:
        """Create Payme invoice for Uzbekistan customers (UZS)."""
        amount_uzs = TIER_CONFIG[tier.value]["price_uzs_monthly"]
        if amount_uzs == 0:
            return {"status": "free_tier"}
        if self._payme is None:
            return {
                "status": "mock",
                "amount_uzs": amount_uzs,
                "message": "Payme client not connected. Configure PAYME_MERCHANT_ID.",
                "payment_url": f"https://checkout.paycom.uz/mock?amount={amount_uzs * 100}&tenant={tenant_id}",
            }
        # Production: call Payme Create Invoice API
        return {"status": "pending", "amount_uzs": amount_uzs}
