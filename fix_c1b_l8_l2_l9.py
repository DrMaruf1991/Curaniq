"""
CURANIQ Fix C1-B: Wire L8 + L2 + L9 Layer Files
Connects 6 self-contained layer files (2,343 lines) into pipeline:
  L8: interface_layer.py (596) — Evidence Cards, Role-Based UI, Multilingual
  L2: grade_engine.py (492) — GRADE certainty grading
  L2: living_review.py (267) — PRISMA-LSR tracking
  L2: retraction_jurisdiction.py (670) — Retraction Watch + Jurisdiction Gate
  L2: jurisdiction_gate.py (24) — Re-export
  L9: citation_payment.py (294) — Payment Gateway (Stripe + Payme/Click/Uzum)

Run in PowerShell:
  cd D:\curaniq_engine\curaniq_engine
  python fix_c1b_l8_l2_l9.py
"""
import os, sys

BASE = r"D:\curaniq_engine\curaniq_engine"
PIPELINE = os.path.join(BASE, "curaniq", "core", "pipeline.py")

if not os.path.exists(PIPELINE):
    print(f"ERROR: {PIPELINE} not found."); sys.exit(1)

# Verify all layer files exist
layers = {
    "L8/interface": "curaniq/layers/L8_interface/interface_layer.py",
    "L2/grade": "curaniq/layers/L2_curation/grade_engine.py",
    "L2/living_review": "curaniq/layers/L2_curation/living_review.py",
    "L2/retraction": "curaniq/layers/L2_curation/retraction_jurisdiction.py",
    "L9/payment": "curaniq/layers/L9_audit_payments/citation_payment.py",
}
for name, path in layers.items():
    full = os.path.join(BASE, path)
    if not os.path.exists(full):
        print(f"ERROR: {name} not found at {full}"); sys.exit(1)
print(f"All {len(layers)} layer files found.")

with open(PIPELINE, "r", encoding="utf-8") as f:
    content = f.read()

# ══════════════════════════════════════════════════════════════
# PATCH 1: Add imports
# ══════════════════════════════════════════════════════════════

IMPORT_MARKER = "from curaniq.layers.L6_security.prompt_defense import PromptDefenseSuite"

NEW_IMPORTS = """from curaniq.layers.L6_security.prompt_defense import PromptDefenseSuite

# L8: Interface layer — Evidence Cards, Role-Based UI, Multilingual
from curaniq.layers.L8_interface.interface_layer import (
    EvidenceCardsBuilder,
    RoleBasedUIAdapter,
    MultilingualEngine,
    MedicationBoundaryDisplay,
    LanguageAutoDetector,
    MedicalTranslationEngine,
)

# L2: Evidence curation engines
from curaniq.layers.L2_curation.grade_engine import GRADEGradingEngine
from curaniq.layers.L2_curation.living_review import LivingReviewEngine
from curaniq.layers.L2_curation.retraction_jurisdiction import (
    RetractionWatchSentinel,
    JurisdictionGuidanceGate,
)

# L9: Payment gateway
from curaniq.layers.L9_audit_payments.citation_payment import PaymentGateway"""

if "EvidenceCardsBuilder" in content:
    print("SKIP: L8/L2/L9 already imported")
else:
    content = content.replace(IMPORT_MARKER, NEW_IMPORTS)
    print("PATCHED: Added L8 + L2 + L9 imports")

# ══════════════════════════════════════════════════════════════
# PATCH 2: Add to __init__
# ══════════════════════════════════════════════════════════════

INIT_MARKER = "        self.prompt_defense    = PromptDefenseSuite()"
NEW_INITS = """        self.prompt_defense    = PromptDefenseSuite()

        # L8: Interface engines
        self.evidence_cards_builder = EvidenceCardsBuilder()
        self.role_adapter           = RoleBasedUIAdapter()
        self.multilingual           = MultilingualEngine()
        self.med_boundary           = MedicationBoundaryDisplay()
        self.translation_engine     = MedicalTranslationEngine()

        # L2: Curation engines
        self.grade_engine           = GRADEGradingEngine()
        self.living_review          = LivingReviewEngine()
        self.retraction_sentinel    = RetractionWatchSentinel()
        self.jurisdiction_gate      = JurisdictionGuidanceGate()

        # L9: Payment
        self.payment_gateway        = PaymentGateway()"""

if "self.evidence_cards_builder" in content:
    print("SKIP: L8/L2/L9 already in __init__")
else:
    content = content.replace(INIT_MARKER, NEW_INITS)
    print("PATCHED: Added L8 + L2 + L9 engines to __init__")

# ══════════════════════════════════════════════════════════════
# WRITE
# ══════════════════════════════════════════════════════════════

with open(PIPELINE, "w", encoding="utf-8") as f:
    f.write(content)
print(f"Saved: {PIPELINE}")

# ══════════════════════════════════════════════════════════════
# VERIFICATION
# ══════════════════════════════════════════════════════════════

print("\n== VERIFICATION ==")
with open(PIPELINE, "r", encoding="utf-8") as f:
    final = f.read()

checks = [
    # L8 Interface
    ("EvidenceCardsBuilder imported",     "EvidenceCardsBuilder" in final),
    ("RoleBasedUIAdapter imported",       "RoleBasedUIAdapter" in final),
    ("MultilingualEngine imported",       "MultilingualEngine" in final),
    ("MedicalTranslationEngine imported", "MedicalTranslationEngine" in final),
    ("MedicationBoundaryDisplay imported","MedicationBoundaryDisplay" in final),
    ("L8 engines in __init__",            "self.evidence_cards_builder" in final),
    # L2 Curation
    ("GRADEGradingEngine imported",       "GRADEGradingEngine" in final),
    ("LivingReviewEngine imported",       "LivingReviewEngine" in final),
    ("RetractionWatchSentinel imported",  "RetractionWatchSentinel" in final),
    ("JurisdictionGuidanceGate imported", "JurisdictionGuidanceGate" in final),
    ("L2 engines in __init__",            "self.grade_engine" in final),
    # L9 Payment
    ("PaymentGateway imported",           "PaymentGateway" in final),
    ("PaymentGateway in __init__",        "self.payment_gateway" in final),
]

ok = 0
for desc, passed in checks:
    ok += passed
    print(f"  {'PASS' if passed else 'FAIL'}: {desc}")

print(f"\n  {ok}/{len(checks)} checks passed")

if ok == len(checks):
    # Count remaining disconnected
    print("\n  L8 + L2 + L9 WIRED (2,343 lines activated)")
    print("  Remaining disconnected: 8 files")
    print("    L1: api_connectors, evidence_compiler, semantic_chunker, staleness_monitor")
    print("    L4: adversarial_jury, claim_contract_engine, constrained_generator, retrieval_pipeline")
    print("    L5: safety_gate_pipeline")
