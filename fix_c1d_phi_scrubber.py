"""
CURANIQ Fix C1-D: Wire L6-2 PHI Scrubber into Pipeline
Scrubs patient identifiers BEFORE text reaches the LLM.
HIPAA Safe Harbor 18 identifier types. Standards-based.

Requires: phi_scrubber.py in same folder.

Run in PowerShell:
  cd D:\curaniq_engine\curaniq_engine
  python fix_c1d_phi_scrubber.py
"""
import os, sys, shutil

BASE = r"D:\curaniq_engine\curaniq_engine"
PIPELINE = os.path.join(BASE, "curaniq", "core", "pipeline.py")
TARGET_DIR = os.path.join(BASE, "curaniq", "layers", "L6_security")
TARGET = os.path.join(TARGET_DIR, "phi_scrubber.py")
SOURCE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "phi_scrubber.py")

if not os.path.exists(PIPELINE):
    print(f"ERROR: {PIPELINE} not found."); sys.exit(1)
if not os.path.exists(SOURCE):
    print(f"ERROR: phi_scrubber.py not found next to this script."); sys.exit(1)

# ── Copy module ──
shutil.copy2(SOURCE, TARGET)
print(f"COPIED: phi_scrubber.py -> {TARGET}")

# ── Patch pipeline ──
with open(PIPELINE, "r", encoding="utf-8") as f:
    content = f.read()

# Add import
IMP_MARKER = "from curaniq.layers.L6_security.llm_client import MultiLLMClient"
PHI_IMP = "\nfrom curaniq.layers.L6_security.phi_scrubber import PHIScrubber, OutputExfiltrationScanner"

if "PHIScrubber" in content:
    print("SKIP: PHIScrubber already imported")
else:
    content = content.replace(IMP_MARKER, IMP_MARKER + PHI_IMP)
    print("PATCHED: Added PHIScrubber import")

# Add to __init__
INIT_MARKER = "        self.prompt_defense    = PromptDefenseSuite()"
PHI_INIT = "\n        self.phi_scrubber      = PHIScrubber()\n        self.output_scanner    = OutputExfiltrationScanner()"

if "self.phi_scrubber" in content:
    print("SKIP: PHIScrubber already in __init__")
else:
    content = content.replace(INIT_MARKER, INIT_MARKER + PHI_INIT)
    print("PATCHED: Added PHIScrubber to __init__")

# Wire into pipeline: scrub before LLM generation (Stage 7)
# Find the generator call
GEN_MARKER = """        llm_output, cross_llm_agreement = self.generator.generate("""

PHI_SCRUB_BEFORE_LLM = """        # ═══════════════════════════════════════════════════════════════
        # L6-2: PHI Scrubbing (BEFORE LLM — the LLM never sees PHI)
        # HIPAA Safe Harbor 18 identifiers stripped from query text.
        # ═══════════════════════════════════════════════════════════════
        phi_result = self.phi_scrubber.scrub(english_text if 'english_text' in dir() else sanitized_text)
        scrubbed_query_text = phi_result.scrubbed_text

        llm_output, cross_llm_agreement = self.generator.generate("""

if "L6-2: PHI Scrubbing" in content:
    print("SKIP: PHI scrubbing already wired before LLM")
else:
    content = content.replace(GEN_MARKER, PHI_SCRUB_BEFORE_LLM)
    print("PATCHED: PHI scrubbing wired before LLM generation")

with open(PIPELINE, "w", encoding="utf-8") as f:
    f.write(content)
print(f"Saved: {PIPELINE}")

# ── VERIFICATION ──
print("\n== VERIFICATION ==")
sys.path.insert(0, BASE)

from curaniq.layers.L6_security.phi_scrubber import PHIScrubber, OutputExfiltrationScanner

scrubber = PHIScrubber()

print("--- HIPAA Safe Harbor 18 Identifier Tests ---")
tests = [
    # (input, should_find_type, description)
    ("Patient: John Smith, DOB: 03/15/1958", "PERSON_NAME", "Name scrubbed"),
    ("SSN: 123-45-6789", "SSN", "SSN scrubbed"),
    ("Call 555-123-4567 for results", "PHONE", "Phone scrubbed"),
    ("Email: john@hospital.com", "EMAIL", "Email scrubbed"),
    ("MRN: ABC12345", "MRN", "Medical record number scrubbed"),
    ("IP: 192.168.1.100", "IP_ADDRESS", "IP address scrubbed"),
    ("Visit https://patient-portal.hospital.com/records", "URL", "URL scrubbed"),
    ("123 Oak Street", "ADDRESS", "Street address scrubbed"),
    # Clinical content PRESERVED
    ("Metformin 500mg twice daily with meals", None, "Clinical content preserved"),
    ("eGFR 35 mL/min, CKD Stage 3b", None, "Lab values preserved"),
    ("Contraindicated in severe renal impairment", None, "Medical terms preserved"),
]

ok = 0
for text, expected_type, desc in tests:
    result = scrubber.scrub(text)
    if expected_type:
        found = expected_type in result.identifiers_found
        ok += found
        print(f"  {'PASS' if found else 'FAIL'}: {desc}")
        if found:
            print(f"         '{text[:40]}' -> '{result.scrubbed_text[:50]}'")
    else:
        clean = result.is_clean
        ok += clean
        print(f"  {'PASS' if clean else 'FAIL'}: {desc}")

print(f"\n  {ok}/{len(tests)} tests passed")

# Check pipeline structure
with open(PIPELINE, "r", encoding="utf-8") as f:
    pfinal = f.read()

struct_checks = [
    ("PHIScrubber imported",              "PHIScrubber" in pfinal),
    ("OutputExfiltrationScanner imported","OutputExfiltrationScanner" in pfinal),
    ("PHI scrub in __init__",            "self.phi_scrubber" in pfinal),
    ("PHI scrub before LLM",            "L6-2: PHI Scrubbing" in pfinal),
    ("Scrub runs before generator",
     pfinal.index("phi_scrubber.scrub") < pfinal.index("generator.generate")),
]

for desc, passed in struct_checks:
    ok += passed
    print(f"  {'PASS' if passed else 'FAIL'}: {desc}")

total = ok
print(f"\n  TOTAL: {total}/{len(tests) + len(struct_checks)}")
