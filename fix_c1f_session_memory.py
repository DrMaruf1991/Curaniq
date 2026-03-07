"""
CURANIQ Fix C1-F: Wire L14-8 Session Memory + L14-3 Assumption Ledger
Multi-turn clinical state + explicit assumption tracking.

Requires: session_memory.py in same folder.

Run in PowerShell:
  cd D:\curaniq_engine\curaniq_engine
  python fix_c1f_session_memory.py
"""
import os, sys, shutil

BASE = r"D:\curaniq_engine\curaniq_engine"
PIPELINE = os.path.join(BASE, "curaniq", "core", "pipeline.py")
SOURCE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "session_memory.py")

if not os.path.exists(PIPELINE):
    print(f"ERROR: {PIPELINE} not found."); sys.exit(1)

# ── Copy module ──
L14_DIR = os.path.join(BASE, "curaniq", "layers", "L14_interaction")
os.makedirs(L14_DIR, exist_ok=True)
init_path = os.path.join(L14_DIR, "__init__.py")
if not os.path.exists(init_path):
    with open(init_path, "w") as f:
        f.write("")

TARGET = os.path.join(L14_DIR, "session_memory.py")
shutil.copy2(SOURCE, TARGET)
print(f"COPIED: session_memory.py -> {TARGET}")

# ── Patch pipeline ──
with open(PIPELINE, "r", encoding="utf-8") as f:
    content = f.read()

IMP_MARKER = "from curaniq.layers.L11_local_reality.drug_availability import LocalDrugAvailabilityFilter"
NEW_IMP = IMP_MARKER + """
from curaniq.layers.L14_interaction.session_memory import ClinicalSessionMemory, AssumptionLedger"""

if "ClinicalSessionMemory" in content:
    print("SKIP: Session memory already imported")
else:
    content = content.replace(IMP_MARKER, NEW_IMP)
    print("PATCHED: Added session memory imports")

INIT_MARKER = "        self.drug_availability = LocalDrugAvailabilityFilter()"
NEW_INIT = INIT_MARKER + """
        self.session_memory    = ClinicalSessionMemory()
        self.assumption_ledger = AssumptionLedger()"""

if "self.session_memory" in content:
    print("SKIP: Session memory already in __init__")
else:
    content = content.replace(INIT_MARKER, NEW_INIT)
    print("PATCHED: Added session memory to __init__")

with open(PIPELINE, "w", encoding="utf-8") as f:
    f.write(content)
print(f"Saved: {PIPELINE}")

# ── VERIFICATION ──
print("\n== VERIFICATION ==")
sys.path.insert(0, BASE)

from curaniq.layers.L14_interaction.session_memory import (
    ClinicalSessionMemory, AssumptionLedger,
)

# Test Session Memory
print("--- L14-8: Session Memory ---")
mem = ClinicalSessionMemory()

# Turn 1: Doctor asks about metformin
sid = mem.get_or_create()
mem.record_turn(sid, "What is the dose of metformin for CKD?",
                drugs=["metformin"], foods=[], evidence_count=5)
mem.update_patient_context(sid, {"age_years": 68, "egfr": 35})

# Turn 2: Doctor asks about adding insulin
mem.record_turn(sid, "Can I add insulin if metformin is insufficient?",
                drugs=["insulin"], foods=[], evidence_count=3)

# Turn 3: Doctor asks about diet
mem.record_turn(sid, "Any food interactions with these drugs?",
                drugs=[], foods=["grapefruit", "alcohol"], evidence_count=2)

# Check accumulation
acc_drugs = mem.get_accumulated_drugs(sid)
ctx = mem.get_patient_context(sid)
turns = mem.get_turn_count(sid)
summary = mem.get_session_summary(sid)
llm_ctx = mem.build_session_context_for_llm(sid)

checks = [
    ("Session created",               sid is not None),
    ("3 turns recorded",              turns == 3),
    ("Drugs accumulated across turns", "metformin" in acc_drugs and "insulin" in acc_drugs),
    ("Patient context persists",       ctx.get("egfr") == 35),
    ("Summary has all drugs",          len(summary["accumulated_drugs"]) == 2),
    ("LLM context includes drugs",     "metformin" in llm_ctx),
    ("LLM context includes queries",   "metformin" in llm_ctx),
]

ok1 = 0
for desc, passed in checks:
    ok1 += passed
    print(f"  {'PASS' if passed else 'FAIL'}: {desc}")

# Test Assumption Ledger
print("\n--- L14-3: Assumption Ledger ---")
ledger = AssumptionLedger()

# Scenario: query about metformin dose, minimal patient context
assumptions = ledger.assess_missing_context(
    query_text="What dose of metformin for diabetes?",
    drugs=["metformin"],
    patient_context={"age_years": 45},  # Only age provided
)

checks2 = [
    ("Renal assumption made",       any(a.category == "renal" for a in assumptions)),
    ("Allergy assumption made",     any(a.category == "allergies" for a in assumptions)),
    ("Medications assumption made", any(a.category == "medications" for a in assumptions)),
    ("Jurisdiction assumption made",any(a.category == "jurisdiction" for a in assumptions)),
    ("Age NOT assumed (provided)",  not any(a.category == "age" for a in assumptions)),
    ("Clinician format works",      "ASSUMPTIONS MADE" in ledger.format_for_clinician()),
    ("LLM format works",            "MISSING PATIENT DATA" in ledger.format_for_llm_context()),
]

ok2 = 0
for desc, passed in checks2:
    ok2 += passed
    print(f"  {'PASS' if passed else 'FAIL'}: {desc}")

# Full context scenario — no assumptions needed
ledger2 = AssumptionLedger()
full_ctx = {
    "age_years": 45, "weight_kg": 70, "sex_at_birth": "M",
    "is_pregnant": False, "renal": {"egfr": 90},
    "allergies": ["penicillin"], "active_medications": ["lisinopril"],
    "jurisdiction": "UZ",
}
assumptions2 = ledger2.assess_missing_context("metformin dose?", ["metformin"], full_ctx)
ok2 += (len(assumptions2) == 0)
print(f"  {'PASS' if len(assumptions2) == 0 else 'FAIL'}: Full context = zero assumptions")

total = ok1 + ok2
total_max = len(checks) + len(checks2) + 1
print(f"\n  TOTAL: {total}/{total_max}")
