"""
CURANIQ Fix 0D: Wire L5-17 NumericGate into Safety Suite
Defense-in-depth: even if claim_contract misses something,
the safety gate catches any unverified numbers.

Every number in clinical output must be either:
  (a) DETERMINISTIC — computed by CQL (dose calc, eGFR, Tisdale score)
  (b) VERBATIM — character-identical from governed evidence snippet
  (c) BLOCKED — neither, and the claim gets suppressed

This gate reads the verification results already computed by
claim_contract.py. No new logic. No hardcoding. Just enforcement.

This is THE feature that beats GPT/Gemini. They hallucinate numbers.
CURANIQ blocks them.

Run in PowerShell:
  cd D:\curaniq_engine\curaniq_engine
  python fix_0d_numeric_gate.py
"""
import os
import sys

BASE = r"D:\curaniq_engine\curaniq_engine"
SAFETY_GATES = os.path.join(BASE, "curaniq", "safety", "safety_gates.py")

if not os.path.exists(SAFETY_GATES):
    print(f"ERROR: {SAFETY_GATES} not found.")
    sys.exit(1)

print(f"Found: {SAFETY_GATES}")

with open(SAFETY_GATES, "r", encoding="utf-8") as f:
    content = f.read()

# ═══════════════════════════════════════════════════════════════
# PATCH 1: Add gate_numeric_verification function
# ═══════════════════════════════════════════════════════════════

# Insert before the SafetyGateSuiteRunner class
GATE_CODE = '''

# ─────────────────────────────────────────────────────────────────────────────
# L5-17: NUMERIC DETERMINISTIC-OR-QUOTED GATE (defense-in-depth)
# Architecture: "Every number must be deterministic (CQL) OR verbatim-quoted
# (hash-bound). Even one unverifiable numeric value = BLOCK."
# This is THE differentiator vs GPT/Gemini. They hallucinate numbers.
# ─────────────────────────────────────────────────────────────────────────────

def gate_numeric_verification(
    claim_contract: ClaimContract,
) -> SafetyGateResult:
    """
    L5-17 defense-in-depth check.
    Reads numeric token verification status from claim_contract
    (already computed by ClaimContractEngine).
    If ANY numeric token is BLOCKED, entire response is flagged.

    No regex. No hardcoded patterns. Just reads the verification
    results that the claim contract already computed.
    """
    from curaniq.models.schemas import NumericTokenStatus

    total_numeric = 0
    blocked_numeric = 0
    blocked_details: list[str] = []

    for claim in claim_contract.atomic_claims:
        for nt in claim.numeric_tokens:
            total_numeric += 1
            if nt.status == NumericTokenStatus.BLOCKED:
                blocked_numeric += 1
                blocked_details.append(
                    f"'{nt.value_str}' in claim: '{claim.claim_text[:60]}...'"
                )

    if total_numeric == 0:
        return SafetyGateResult(
            gate_id="L5-17",
            gate_name="Numeric Deterministic-or-Quoted Gate",
            passed=True,
            message="No numeric values in output — gate not applicable.",
            severity="INFO",
        )

    if blocked_numeric > 0:
        return SafetyGateResult(
            gate_id="L5-17",
            gate_name="Numeric Deterministic-or-Quoted Gate",
            passed=False,
            message=(
                f"NUMERIC SAFETY BLOCK: {blocked_numeric}/{total_numeric} "
                f"numeric value(s) could not be verified as deterministic (CQL) "
                f"or verbatim from evidence. Unverified: "
                + "; ".join(blocked_details[:3])
                + (f" (+{blocked_numeric - 3} more)" if blocked_numeric > 3 else "")
            ),
            severity="BLOCK",
        )

    # All numeric tokens verified
    verified_det = sum(
        1 for c in claim_contract.atomic_claims
        for nt in c.numeric_tokens
        if nt.status == NumericTokenStatus.DETERMINISTIC
    )
    verified_verb = total_numeric - verified_det

    return SafetyGateResult(
        gate_id="L5-17",
        gate_name="Numeric Deterministic-or-Quoted Gate",
        passed=True,
        message=(
            f"All {total_numeric} numeric value(s) verified: "
            f"{verified_det} deterministic (CQL), "
            f"{verified_verb} verbatim from evidence."
        ),
        severity="INFO",
    )

'''

SUITE_MARKER = "class SafetyGateSuiteRunner:"

if "gate_numeric_verification" in content:
    print("SKIP: gate_numeric_verification already exists")
else:
    content = content.replace(SUITE_MARKER, GATE_CODE + SUITE_MARKER)
    print("PATCHED: Added gate_numeric_verification function")

# ═══════════════════════════════════════════════════════════════
# PATCH 2: Wire as Gate 12 in SafetyGateSuiteRunner.run_all
# Insert after Gate 11 (Black Box / REMS), before suite creation
# ═══════════════════════════════════════════════════════════════

OLD_SUITE_BUILD = """        # Gate 11: L5-11 Black Box / REMS
        all_gates.append(gate_black_box_rems(claims))

        suite = SafetyGateSuite("""

NEW_SUITE_BUILD = """        # Gate 11: L5-11 Black Box / REMS
        all_gates.append(gate_black_box_rems(claims))

        # Gate 12: L5-17 Numeric Deterministic-or-Quoted (defense-in-depth)
        # Every number must be CQL-computed or verbatim from evidence.
        # This is CURANIQ's #1 differentiator vs GPT/Gemini.
        all_gates.append(gate_numeric_verification(claim_contract))

        suite = SafetyGateSuite("""

if "gate_numeric_verification(claim_contract)" in content:
    print("SKIP: Gate 12 already wired in run_all")
else:
    content = content.replace(OLD_SUITE_BUILD, NEW_SUITE_BUILD)
    print("PATCHED: Wired gate_numeric_verification as Gate 12")

# ═══════════════════════════════════════════════════════════════
# WRITE
# ═══════════════════════════════════════════════════════════════

with open(SAFETY_GATES, "w", encoding="utf-8") as f:
    f.write(content)
print(f"Saved: {SAFETY_GATES}")

# ═══════════════════════════════════════════════════════════════
# VERIFICATION
# ═══════════════════════════════════════════════════════════════

print("\n== VERIFICATION ==")

with open(SAFETY_GATES, "r", encoding="utf-8") as f:
    final = f.read()

checks = [
    ("gate_numeric_verification function exists",
     "def gate_numeric_verification(" in final),
    ("Reads NumericTokenStatus.BLOCKED",
     "NumericTokenStatus.BLOCKED" in final),
    ("Reads NumericTokenStatus.DETERMINISTIC",
     "NumericTokenStatus.DETERMINISTIC" in final),
    ("Gate 12 wired in run_all",
     "gate_numeric_verification(claim_contract)" in final),
    ("Gate 12 comes after Gate 11",
     final.index("Gate 11") < final.index("Gate 12")),
    ("Gate 12 comes before suite build",
     final.index("gate_numeric_verification(claim_contract)") < final.index("suite = SafetyGateSuite(")),
    ("No hardcoded dose patterns",
     "mg" not in final.split("gate_numeric_verification")[1].split("class ")[0]
     if "gate_numeric_verification" in final else False),
    ("No regex in numeric gate",
     "re.compile" not in final.split("gate_numeric_verification")[1].split("class ")[0]
     if "gate_numeric_verification" in final else False),
    ("Reports verified counts (deterministic + verbatim)",
     "deterministic (CQL)" in final and "verbatim from evidence" in final),
    ("BLOCK severity for failures",
     '"BLOCK"' in final),
    ("Total gates now 12",
     final.count("all_gates.append(") == 12),
]

ok = 0
for desc, passed in checks:
    ok += passed
    print(f"  {'PASS' if passed else 'FAIL'}: {desc}")

print(f"\n  {ok}/{len(checks)} checks passed")

if ok == len(checks):
    print("\n  L5-17 NUMERIC GATE WIRED")
    print("  12 safety gates now active:")
    print("  1.  Retraction Blocking")
    print("  2.  Patient Mode Boundary")
    print("  3.  Task Gating by Role")
    print("  4.  No Evidence Refusal")
    print("  5.  Completeness")
    print("  6.  Dose Plausibility")
    print("  7.  Safety Language")
    print("  8.  Edge-Case Detection")
    print("  9.  Semantic Entropy")
    print("  10. Output Completeness")
    print("  11. Black Box / REMS")
    print("  12. NUMERIC DETERMINISTIC-OR-QUOTED (NEW)")
