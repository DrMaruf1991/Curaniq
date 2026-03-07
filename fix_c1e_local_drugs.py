"""
CURANIQ Fix C1-E: L11-1 Local Drug Availability + PHI Fix
1. Updates phi_scrubber.py (fixes name/address overlap)
2. Creates L11 layer directory
3. Wires drug availability filter into pipeline

Requires: drug_availability.py + phi_scrubber.py in same folder.

Run in PowerShell:
  cd D:\curaniq_engine\curaniq_engine
  python fix_c1e_local_drugs.py
"""
import os, sys, shutil

BASE = r"D:\curaniq_engine\curaniq_engine"
PIPELINE = os.path.join(BASE, "curaniq", "core", "pipeline.py")

# Source files
DRUG_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "drug_availability.py")
PHI_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "phi_scrubber.py")

if not os.path.exists(PIPELINE):
    print(f"ERROR: {PIPELINE} not found."); sys.exit(1)

# ── STEP 1: Update PHI scrubber (fixes overlap) ──
PHI_TARGET = os.path.join(BASE, "curaniq", "layers", "L6_security", "phi_scrubber.py")
if os.path.exists(PHI_SRC):
    shutil.copy2(PHI_SRC, PHI_TARGET)
    print(f"UPDATED: phi_scrubber.py (name/address overlap fixed)")
else:
    print("SKIP: phi_scrubber.py not in folder (already applied)")

# ── STEP 2: Copy drug availability module ──
L11_DIR = os.path.join(BASE, "curaniq", "layers", "L11_local_reality")
os.makedirs(L11_DIR, exist_ok=True)
init_path = os.path.join(L11_DIR, "__init__.py")
if not os.path.exists(init_path):
    with open(init_path, "w") as f:
        f.write("")

DRUG_TARGET = os.path.join(L11_DIR, "drug_availability.py")
if os.path.exists(DRUG_SRC):
    shutil.copy2(DRUG_SRC, DRUG_TARGET)
    print(f"COPIED: drug_availability.py -> {DRUG_TARGET}")
else:
    print(f"ERROR: drug_availability.py not found."); sys.exit(1)

# ── STEP 3: Patch pipeline ──
with open(PIPELINE, "r", encoding="utf-8") as f:
    content = f.read()

# Add import
IMP_MARKER = "from curaniq.layers.L6_security.phi_scrubber import PHIScrubber"
L11_IMP = "\nfrom curaniq.layers.L11_local_reality.drug_availability import LocalDrugAvailabilityFilter"

if "LocalDrugAvailabilityFilter" in content:
    print("SKIP: L11-1 already imported")
else:
    content = content.replace(IMP_MARKER, IMP_MARKER + L11_IMP)
    print("PATCHED: Added L11-1 import")

# Add to __init__
INIT_MARKER = "        self.phi_scrubber      = PHIScrubber()"
L11_INIT = "\n        self.drug_availability = LocalDrugAvailabilityFilter()"

if "self.drug_availability" in content:
    print("SKIP: L11-1 already in __init__")
else:
    content = content.replace(INIT_MARKER, INIT_MARKER + L11_INIT)
    print("PATCHED: Added drug_availability to __init__")

with open(PIPELINE, "w", encoding="utf-8") as f:
    f.write(content)
print(f"Saved: {PIPELINE}")

# ── VERIFICATION ──
print("\n== VERIFICATION ==")
sys.path.insert(0, BASE)

from curaniq.layers.L11_local_reality.drug_availability import LocalDrugAvailabilityFilter

filt = LocalDrugAvailabilityFilter()

print("--- Drug Availability (Uzbekistan) ---")
tests = [
    ("metformin", "UZ", True, "available"),
    ("amoxicillin", "UZ", True, "available"),
    ("warfarin", "UZ", True, "available"),
    ("apixaban", "UZ", False, "unavailable"),
    ("rivaroxaban", "UZ", False, "unavailable"),
    ("morphine", "UZ", True, "restricted"),
    ("tramadol", "UZ", True, "restricted"),
    ("sacubitril/valsartan", "UZ", False, "shortage"),
]

ok = 0
for drug, jur, exp_avail, exp_status in tests:
    r = filt.check(drug, jur)
    p = (r.is_available == exp_avail) and (r.status == exp_status)
    ok += p
    print(f"  {'PASS' if p else 'FAIL'}: {drug} -> {r.status} (available={r.is_available})")
    if r.alternatives:
        print(f"         Alternatives: {r.alternatives}")
    if r.restrictions:
        print(f"         Restriction: {r.restrictions}")

print(f"\n  {ok}/{len(tests)} availability checks passed")

# Alerts test
alerts = filt.get_unavailable_alerts(
    ["metformin", "apixaban", "morphine"], "UZ"
)
print(f"\n--- Alerts for [metformin, apixaban, morphine] ---")
for a in alerts:
    print(f"  {a}")
print(f"  {len(alerts)} alerts generated (expect 2: apixaban unavailable + morphine restricted)")

# Pipeline structure
with open(PIPELINE, "r", encoding="utf-8") as f:
    pfinal = f.read()
print(f"\n  Pipeline has LocalDrugAvailabilityFilter: {'YES' if 'drug_availability' in pfinal else 'NO'}")
print(f"  Formulary: CURANIQ_FORMULARY_PATH env (or seed data)")
print(f"  Seed: {len(filt._formularies.get('UZ', {}))} drugs for Uzbekistan")
