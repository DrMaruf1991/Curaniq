"""
CURANIQ Fix 0B: Universal Input Layer
1. Copies universal_input.py to curaniq/layers/L8_interface/
2. Patches pipeline.py to use UniversalInputNormalizer

Requires: universal_input.py in same folder as this script.

Run in PowerShell:
  cd D:\curaniq_engine\curaniq_engine
  python fix_0b_patch_pipeline.py
"""
import os
import sys
import shutil

BASE = r"D:\curaniq_engine\curaniq_engine"
PIPELINE = os.path.join(BASE, "curaniq", "core", "pipeline.py")
TARGET = os.path.join(BASE, "curaniq", "layers", "L8_interface", "universal_input.py")
SOURCE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "universal_input.py")

if not os.path.exists(PIPELINE):
    print(f"ERROR: {PIPELINE} not found.")
    sys.exit(1)

# ── STEP 1: Copy universal_input.py ──
if not os.path.exists(SOURCE):
    print(f"ERROR: universal_input.py not found next to this script.")
    print(f"Expected: {SOURCE}")
    sys.exit(1)

os.makedirs(os.path.dirname(TARGET), exist_ok=True)
shutil.copy2(SOURCE, TARGET)
print(f"COPIED: universal_input.py -> {TARGET}")

# ── STEP 2: Patch pipeline.py ──
with open(PIPELINE, "r", encoding="utf-8") as f:
    content = f.read()

# Add import
IMPORT_AFTER = "from curaniq.layers.L2_curation.ontology import OntologyNormalizer"
NEW_IMPORT = "\nfrom curaniq.layers.L8_interface.universal_input import UniversalInputNormalizer"

if "UniversalInputNormalizer" in content:
    print("SKIP: UniversalInputNormalizer already imported")
else:
    content = content.replace(IMPORT_AFTER, IMPORT_AFTER + NEW_IMPORT)
    print("PATCHED: Added UniversalInputNormalizer import")

# Add to __init__
INIT_AFTER = "        self.ontology          = OntologyNormalizer()"
NEW_INIT = "\n        self.input_normalizer  = UniversalInputNormalizer()"

if "self.input_normalizer" in content:
    print("SKIP: input_normalizer already in __init__")
else:
    content = content.replace(INIT_AFTER, INIT_AFTER + NEW_INIT)
    print("PATCHED: Added input_normalizer to __init__")

# Replace _extract_food_herbs
OLD_START = '    def _extract_food_herbs(self, text: str) -> list[str]:'
OLD_END = '    def _extract_monitoring(self, cql_results: dict'

if OLD_START in content and OLD_END in content:
    si = content.index(OLD_START)
    ei = content.index(OLD_END)
    NEW = '''    def _extract_food_herbs(self, text: str) -> list[str]:
        """
        Extract food/herb mentions via UniversalInputNormalizer.
        Any language -> canonical English terms for L3-17 processing.
        """
        normalized = self.input_normalizer.normalize(text)
        return normalized.detected_foods

    '''
    content = content[:si] + NEW + content[ei:]
    print("PATCHED: _extract_food_herbs uses UniversalInputNormalizer")
else:
    print("WARNING: Could not find _extract_food_herbs")

with open(PIPELINE, "w", encoding="utf-8") as f:
    f.write(content)
print(f"Saved: {PIPELINE}")

# ── VERIFICATION ──
print("\n== VERIFICATION ==")
sys.path.insert(0, BASE)

from curaniq.layers.L8_interface.universal_input import (
    detect_script, UniversalInputNormalizer, is_english_script,
)

print("\n--- Script Detection (Any Language) ---")
tests = [
    ("Hello world",             "latin"),
    ("Привет мир",              "cyrillic"),
    ("مرحبا بالعالم",           "arabic"),
    ("你好世界",                 "cjk"),
    ("안녕하세요",               "hangul"),
    ("नमस्ते दुनिया",           "devanagari"),
    ("Ola mundo",               "latin"),
    ("Γεια σου κόσμε",          "greek"),
    ("გამარჯობა",               "georgian"),
    ("สวัสดีชาวโลก",            "thai"),
]

ok = 0
for text, expected in tests:
    got = detect_script(text)
    p = (got == expected)
    ok += p
    s = "PASS" if p else "FAIL"
    print(f"  {s}: '{text[:25]}' -> {got}")
print(f"  {ok}/{len(tests)}")

print("\n--- Drug Extraction (Any Language) ---")
normalizer = UniversalInputNormalizer()

drug_tests = [
    ("metformin dose?",              ["metformin"]),
    ("Метформин дозировка",          ["metformin"]),
    ("глюкофаж при диабете",         ["metformin"]),
    ("aspirin and warfarin",         ["warfarin"]),
    ("аспирин с варфарином",         ["warfarin"]),
    ("Tylenol overdose",             ["paracetamol"]),
    ("аугментин для ребенка",        ["amoxicillin/clavulanic acid"]),
]

ok2 = 0
for text, expected in drug_tests:
    nq = normalizer.normalize(text)
    found = all(d in nq.detected_drugs for d in expected)
    ok2 += found
    s = "PASS" if found else "FAIL"
    print(f"  {s}: '{text[:30]}' -> {nq.detected_drugs}")
print(f"  {ok2}/{len(drug_tests)}")

print(f"\n  TOTAL: {ok + ok2}/{len(tests) + len(drug_tests)}")
