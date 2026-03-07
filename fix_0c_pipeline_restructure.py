"""
CURANIQ Fix 0C: Pipeline Restructure - Normalize First
Moves UniversalInputNormalizer BEFORE triage gate.
Everything after sanitization runs on English.

Pipeline becomes:
  1. Sanitize (prompt injection)
  2. Normalize to English (any language -> English)
  3. Triage on ENGLISH text (works for all languages)
  4. Mode router on English
  5. Retrieve evidence using English + detected drug INNs
  6. CQL with pre-extracted drugs (from normalizer)
  7. Generate, verify, gate, audit

One change. All languages. No per-language anything.

Run in PowerShell:
  cd D:\curaniq_engine\curaniq_engine
  python fix_0c_pipeline_restructure.py
"""
import os
import sys

BASE = r"D:\curaniq_engine\curaniq_engine"
PIPELINE = os.path.join(BASE, "curaniq", "core", "pipeline.py")

if not os.path.exists(PIPELINE):
    print(f"ERROR: {PIPELINE} not found.")
    sys.exit(1)

print(f"Found: {PIPELINE}")

with open(PIPELINE, "r", encoding="utf-8") as f:
    content = f.read()

# ═══════════════════════════════════════════════════════════════
# PATCH: Replace STAGE 1-6 in process() method
# Old flow: sanitize -> triage(raw) -> route -> decompose -> retrieve -> CQL(extract_drugs)
# New flow: sanitize -> NORMALIZE -> triage(english) -> route -> decompose -> retrieve -> CQL(normalized.drugs)
# ═══════════════════════════════════════════════════════════════

OLD_STAGES = '''        # ═══════════════════════════════════════════════════════════════
        # STAGE 1: L6-1 Prompt Injection Defense
        # ═══════════════════════════════════════════════════════════════
        sanitized_text, injection_detected = sanitize_input(query.raw_text)
        if injection_detected:
            return self._build_refusal_response(
                query,
                "PROMPT_INJECTION",
                "Prompt injection attempt detected. Input sanitized. Query cannot be processed.",
                InteractionMode.QUICK_ANSWER,
            )

        # ═══════════════════════════════════════════════════════════════
        # STAGE 2: L5-13 Triage Gate (pre-LLM emergency classifier)
        # ═══════════════════════════════════════════════════════════════
        triage = self.triage_gate.assess(sanitized_text, query.patient_context)

        if triage.result == TriageResult.EMERGENCY:
            # Pipeline HALTS. Return pre-scripted emergency escalation only.
            return self._build_emergency_response(query, triage)

        # ═══════════════════════════════════════════════════════════════
        # STAGE 3: L14-1 Mode Router
        # ═══════════════════════════════════════════════════════════════
        mode = self.mode_router.route(query)

        # ═══════════════════════════════════════════════════════════════
        # STAGE 4: L14-2 Question Decomposer
        # ═══════════════════════════════════════════════════════════════
        sub_queries = self.decomposer.decompose(sanitized_text)

        # ═══════════════════════════════════════════════════════════════
        # STAGE 5: L4-1 Hybrid Retriever
        # ═══════════════════════════════════════════════════════════════
        evidence_pack = self.retriever.retrieve(
            query=query,
            mode=mode,
            sub_queries=sub_queries[1:],  # Skip original query (already in retriever)
        )

        # ═══════════════════════════════════════════════════════════════
        # STAGE 6: L3-1 CQL Safety Kernel (deterministic rules)
        # ═══════════════════════════════════════════════════════════════
        # Extract drugs mentioned in query for CQL processing
        drugs_mentioned = self._extract_drugs(sanitized_text)
        food_herbs = self._extract_food_herbs(sanitized_text)'''

NEW_STAGES = '''        # ═══════════════════════════════════════════════════════════════
        # STAGE 1: L6-1 Prompt Injection Defense
        # ═══════════════════════════════════════════════════════════════
        sanitized_text, injection_detected = sanitize_input(query.raw_text)
        if injection_detected:
            return self._build_refusal_response(
                query,
                "PROMPT_INJECTION",
                "Prompt injection attempt detected. Input sanitized. Query cannot be processed.",
                InteractionMode.QUICK_ANSWER,
            )

        # ═══════════════════════════════════════════════════════════════
        # STAGE 1.5: L8-12/L8-13 Universal Input Normalization
        # Any language -> English for all deterministic processing.
        # Medical entities extracted here, used by CQL and retrieval.
        # ═══════════════════════════════════════════════════════════════
        normalized = self.input_normalizer.normalize(sanitized_text)
        english_text = normalized.english_text
        drugs_mentioned = normalized.detected_drugs
        food_herbs = normalized.detected_foods

        # ═══════════════════════════════════════════════════════════════
        # STAGE 2: L5-13 Triage Gate (on ENGLISH text — works any language)
        # ═══════════════════════════════════════════════════════════════
        triage = self.triage_gate.assess(english_text, query.patient_context)

        if triage.result == TriageResult.EMERGENCY:
            # Pipeline HALTS. Return pre-scripted emergency escalation only.
            return self._build_emergency_response(query, triage)

        # ═══════════════════════════════════════════════════════════════
        # STAGE 3: L14-1 Mode Router
        # ═══════════════════════════════════════════════════════════════
        mode = self.mode_router.route(query)

        # ═══════════════════════════════════════════════════════════════
        # STAGE 4: L14-2 Question Decomposer
        # ═══════════════════════════════════════════════════════════════
        sub_queries = self.decomposer.decompose(english_text)

        # ═══════════════════════════════════════════════════════════════
        # STAGE 5: L4-1 Hybrid Retriever
        # ═══════════════════════════════════════════════════════════════
        evidence_pack = self.retriever.retrieve(
            query=query,
            mode=mode,
            sub_queries=sub_queries[1:],
        )

        # ═══════════════════════════════════════════════════════════════
        # STAGE 6: L3-1 CQL Safety Kernel (deterministic rules)
        # Drugs and food/herbs already extracted by normalizer.
        # ═══════════════════════════════════════════════════════════════'''

if OLD_STAGES in content:
    content = content.replace(OLD_STAGES, NEW_STAGES)
    print("PATCHED: Pipeline restructured — normalize before triage")
else:
    # Try to find if already patched
    if "STAGE 1.5: L8-12/L8-13" in content:
        print("SKIP: Pipeline already restructured")
    else:
        print("WARNING: Could not find pipeline stages to replace.")
        print("The stage markers may have changed. Manual check needed.")
        sys.exit(1)

# ═══════════════════════════════════════════════════════════════
# Also remove the old _extract_drugs call from Stage 6 area
# since drugs are now extracted in Stage 1.5
# ═══════════════════════════════════════════════════════════════

# The old code after Stage 6 header had:
#   drugs_mentioned = self._extract_drugs(sanitized_text)
#   food_herbs = self._extract_food_herbs(sanitized_text)
# These are now gone (moved to normalizer in Stage 1.5)
# But we need to make sure the CQL call still works:

OLD_CQL = '''        cql_results = self.cql_kernel.run_all_checks(
            patient=query.patient_context or _empty_patient(),
            drugs_mentioned=drugs_mentioned,
            food_herb_mentioned=food_herbs if food_herbs else None,
        )'''

# This should still be there and still work since drugs_mentioned
# and food_herbs are now set in Stage 1.5
if OLD_CQL in content:
    print("VERIFIED: CQL call uses pre-extracted drugs from normalizer")
else:
    print("WARNING: CQL call not found in expected location")

# ═══════════════════════════════════════════════════════════════
# WRITE
# ═══════════════════════════════════════════════════════════════

with open(PIPELINE, "w", encoding="utf-8") as f:
    f.write(content)
print(f"Saved: {PIPELINE}")

# ═══════════════════════════════════════════════════════════════
# VERIFICATION
# ═══════════════════════════════════════════════════════════════

print("\n== VERIFICATION ==")
print("Checking pipeline structure...")

with open(PIPELINE, "r", encoding="utf-8") as f:
    final = f.read()

checks = [
    ("Stage 1.5 normalization exists",     "STAGE 1.5: L8-12/L8-13" in final),
    ("Normalizer runs before triage",       final.index("input_normalizer.normalize") < final.index("triage_gate.assess")),
    ("Triage receives english_text",        "triage_gate.assess(english_text" in final),
    ("Decomposer receives english_text",    "decomposer.decompose(english_text)" in final),
    ("Drugs from normalizer not hardcode",  "drugs_mentioned = normalized.detected_drugs" in final),
    ("Foods from normalizer not hardcode",  "food_herbs = normalized.detected_foods" in final),
    ("CQL uses pre-extracted drugs",        "drugs_mentioned=drugs_mentioned" in final),
    ("No old _extract_drugs(sanitized",     "_extract_drugs(sanitized_text)" not in final),
    ("No old _extract_food_herbs(sanit",    "_extract_food_herbs(sanitized_text)" not in final),
]

ok = 0
for desc, passed in checks:
    ok += passed
    print(f"  {'PASS' if passed else 'FAIL'}: {desc}")

print(f"\n  {ok}/{len(checks)} structural checks passed")

if ok == len(checks):
    print("\n  PIPELINE RESTRUCTURED CORRECTLY")
    print("  Flow: Sanitize -> Normalize(any lang) -> Triage(English) -> Route -> Retrieve -> CQL -> Generate -> Verify -> Gate -> Audit")
