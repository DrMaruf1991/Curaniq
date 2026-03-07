"""
CURANIQ Fix 0A: Wire L2-1 OntologyNormalizer into Pipeline
Replaces hardcoded 50-drug English list with universal ontology resolution.
Any language in -> canonical INN out -> CQL processes by INN.

Run in PowerShell:
  cd D:\curaniq_engine\curaniq_engine
  python fix_0a_ontology_wiring.py
"""
import os
import sys

BASE = r"D:\curaniq_engine\curaniq_engine"
PIPELINE = os.path.join(BASE, "curaniq", "core", "pipeline.py")

if not os.path.exists(PIPELINE):
    print(f"ERROR: {PIPELINE} not found.")
    print(f"Check path: {BASE}\\curaniq\\core\\pipeline.py")
    sys.exit(1)

print(f"Found: {PIPELINE}")

with open(PIPELINE, "r", encoding="utf-8") as f:
    content = f.read()

# ── PATCH 1: Add OntologyNormalizer import ──

OLD_IMPORT = "from curaniq.safety.triage_gate import TriageGate"
NEW_IMPORT = """from curaniq.safety.triage_gate import TriageGate
from curaniq.layers.L2_curation.ontology import OntologyNormalizer, resolve_drug_name, get_search_synonyms"""

if "OntologyNormalizer" in content:
    print("SKIP: OntologyNormalizer already imported")
else:
    content = content.replace(OLD_IMPORT, NEW_IMPORT)
    print("PATCHED: Added OntologyNormalizer import")

# ── PATCH 2: Add OntologyNormalizer to __init__ ──

OLD_INIT = "        self.audit_ledger      = AuditLedger()"
NEW_INIT = """        self.audit_ledger      = AuditLedger()
        self.ontology          = OntologyNormalizer()"""

if "self.ontology" in content:
    print("SKIP: self.ontology already in __init__")
else:
    content = content.replace(OLD_INIT, NEW_INIT)
    print("PATCHED: Added self.ontology to __init__")

# ── PATCH 3: Replace _extract_drugs ──

OLD_START = '    def _extract_drugs(self, text: str) -> list[str]:'
OLD_END = '    def _extract_food_herbs(self, text: str) -> list[str]:'

if OLD_START in content and OLD_END in content:
    si = content.index(OLD_START)
    ei = content.index(OLD_END)

    NEW_METHOD = '''    def _extract_drugs(self, text: str) -> list[str]:
        """
        Extract drug names using L2-1 OntologyNormalizer.
        Universal: any language -> canonical INN.
        """
        import re as _re
        from curaniq.layers.L2_curation.ontology import _REVERSE_DRUG_LOOKUP

        found_inns: list[str] = []
        seen: set[str] = set()
        text_lower = text.lower()

        # Match known drug names from ontology (any language)
        for variant, inn in _REVERSE_DRUG_LOOKUP.items():
            if len(variant) >= 3 and _re.search(
                r'\\b' + _re.escape(variant) + r'\\b', text_lower
            ):
                if inn not in seen:
                    found_inns.append(inn)
                    seen.add(inn)

        # Tokenize and resolve unknown words
        tokens = _re.findall(r'\\b[a-zA-Z\\u0400-\\u04FF]{4,}\\b', text)
        for token in tokens:
            canonical, resolved = resolve_drug_name(token)
            if resolved and canonical not in seen:
                found_inns.append(canonical)
                seen.add(canonical)

        return found_inns

'''
    content = content[:si] + NEW_METHOD + content[ei:]
    print("PATCHED: Replaced _extract_drugs with ontology resolution")
else:
    print("WARNING: Could not find _extract_drugs boundaries")

# ── WRITE ──
with open(PIPELINE, "w", encoding="utf-8") as f:
    f.write(content)
print(f"\nSaved: {PIPELINE}")

# ── VERIFY ──
print("\n== VERIFICATION ==")
sys.path.insert(0, BASE)

from curaniq.layers.L2_curation.ontology import (
    resolve_drug_name, _REVERSE_DRUG_LOOKUP
)
import re

def test(text):
    found, seen = [], set()
    tl = text.lower()
    for v, inn in _REVERSE_DRUG_LOOKUP.items():
        if len(v) >= 3 and re.search(r'\b' + re.escape(v) + r'\b', tl):
            if inn not in seen:
                found.append(inn); seen.add(inn)
    for tok in re.findall(r'\b[a-zA-Z\u0400-\u04FF]{4,}\b', text):
        c, r = resolve_drug_name(tok)
        if r and c not in seen:
            found.append(c); seen.add(c)
    return found

tests = [
    ("metformin dose?",                  "metformin",     True),
    ("Какая доза метформина?",            "metformin",     True),
    ("глюкофаж при диабете",              "metformin",     True),
    ("escitalopram for anxiety",          "citalopram",    False),
    ("aspirin safe with warfarin?",       "warfarin",      True),
    ("аспирин с варфарином?",             "warfarin",      True),
    ("Augmentin dose",                    "amoxicillin/clavulanic acid", True),
    ("аугментин дозировка",               "amoxicillin/clavulanic acid", True),
    ("Tylenol and Advil",                 "paracetamol",   True),
    ("albuterol vs ventolin",             "salbutamol",    True),
]

ok = 0
for q, drug, want in tests:
    r = test(q)
    got = drug in r
    p = (got == want)
    ok += p
    print(f"  {'PASS' if p else 'FAIL'}: '{q[:35]}' -> {r}")

print(f"\n  {ok}/{len(tests)} passed")
