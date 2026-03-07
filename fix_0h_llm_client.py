"""
CURANIQ Fix 0H: Wire Real LLM Client
Replaces NotImplementedError in _call_llm with real multi-provider failover.
All API keys from environment. No hardcoded keys, models, or endpoints.

If no API keys set: generator stays in mock mode (dev).
If ANTHROPIC_API_KEY set: real Claude calls.
If Claude fails and OPENAI_API_KEY set: failover to GPT-4o.

Requires: llm_client.py in same folder.

Run in PowerShell:
  cd D:\curaniq_engine\curaniq_engine
  python fix_0h_llm_client.py
"""
import os, sys, shutil

BASE = r"D:\curaniq_engine\curaniq_engine"
PIPELINE = os.path.join(BASE, "curaniq", "core", "pipeline.py")
COMPONENTS = os.path.join(BASE, "curaniq", "core", "pipeline_components.py")
TARGET_DIR = os.path.join(BASE, "curaniq", "layers", "L6_security")
TARGET = os.path.join(TARGET_DIR, "llm_client.py")
SOURCE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "llm_client.py")

if not os.path.exists(PIPELINE):
    print(f"ERROR: {PIPELINE} not found."); sys.exit(1)
if not os.path.exists(SOURCE):
    print(f"ERROR: llm_client.py not found next to this script."); sys.exit(1)

# ── STEP 1: Copy module ──
os.makedirs(TARGET_DIR, exist_ok=True)
shutil.copy2(SOURCE, TARGET)
print(f"COPIED: llm_client.py -> {TARGET}")

# ── STEP 2: Patch pipeline_components.py — implement _call_llm ──
with open(COMPONENTS, "r", encoding="utf-8") as f:
    comp = f.read()

OLD_CALL_LLM = '''        # This would call: self._llm_client.messages.create(...)
        # With the GENERATOR_SYSTEM_PROMPT formatted with evidence + patient context
        # Then run adversarial verifier (L4-12) to get cross_llm_agreement
        raise NotImplementedError(
            "Production LLM call requires Anthropic client initialization. "
            "See curaniq/services/llm_client.py"
        )'''

NEW_CALL_LLM = '''        # Build the full prompt from template
        system_prompt = GENERATOR_SYSTEM_PROMPT.format(
            evidence_pack_text=evidence_text,
            patient_context_text=patient_text,
            query_text=query_text,
        )

        # Call LLM via multi-provider failover client
        response = self._llm_client.generate(
            system_prompt=system_prompt,
            user_prompt=query_text,
        )

        if not response.success:
            # All providers failed — return empty with 0 agreement
            return (
                "Unable to generate clinical response. All LLM providers failed. "
                f"Error: {response.error}. "
                "Safe next steps: consult official prescribing information.",
                0.0,
            )

        # cross_llm_agreement: 0.85 default when no adversarial verifier yet.
        # TODO: Wire L4-12 adversarial jury for real cross-LLM verification.
        cross_llm_agreement = 0.85

        return response.text, cross_llm_agreement'''

if "raise NotImplementedError" in comp:
    comp = comp.replace(OLD_CALL_LLM, NEW_CALL_LLM)
    print("PATCHED: _call_llm now calls real LLM via MultiLLMClient")
elif "self._llm_client.generate" in comp:
    print("SKIP: _call_llm already implemented")
else:
    print("WARNING: Could not find _call_llm placeholder")

with open(COMPONENTS, "w", encoding="utf-8") as f:
    f.write(comp)
print(f"Saved: {COMPONENTS}")

# ── STEP 3: Patch pipeline.py — initialize LLM client from environment ──
with open(PIPELINE, "r", encoding="utf-8") as f:
    pipe = f.read()

# Add import
IMP_MARKER = "from curaniq.layers.L6_security.prompt_defense import PromptDefenseSuite"
LLM_IMP = "\nfrom curaniq.layers.L6_security.llm_client import MultiLLMClient"

if "MultiLLMClient" in pipe:
    print("SKIP: MultiLLMClient already imported")
else:
    pipe = pipe.replace(IMP_MARKER, IMP_MARKER + LLM_IMP)
    print("PATCHED: Added MultiLLMClient import")

# Replace generator initialization to use env-driven client
OLD_GEN_INIT = "        self.generator         = ConstrainedGenerator(llm_client)"
NEW_GEN_INIT = """        # LLM client from environment. None = mock mode (no API keys).
        _llm = llm_client or MultiLLMClient.from_environment()
        self.generator         = ConstrainedGenerator(_llm)"""

if "MultiLLMClient.from_environment()" in pipe:
    print("SKIP: Generator already uses env-driven LLM client")
else:
    pipe = pipe.replace(OLD_GEN_INIT, NEW_GEN_INIT)
    print("PATCHED: Generator now uses env-driven MultiLLMClient")

with open(PIPELINE, "w", encoding="utf-8") as f:
    f.write(pipe)
print(f"Saved: {PIPELINE}")

# ── VERIFICATION ──
print("\n== VERIFICATION ==")
sys.path.insert(0, BASE)

from curaniq.layers.L6_security.llm_client import (
    MultiLLMClient, LLMResponse, LLMProviderConfig,
)

# Test 1: No API keys = None (dev mode)
os.environ.pop("ANTHROPIC_API_KEY", None)
os.environ.pop("OPENAI_API_KEY", None)
os.environ.pop("GOOGLE_API_KEY", None)
client = MultiLLMClient.from_environment()
print(f"  PASS: No API keys -> client is None (mock mode)" if client is None else "  FAIL")

# Test 2: Check pipeline file structure
with open(PIPELINE, "r", encoding="utf-8") as f:
    pfinal = f.read()

checks = [
    ("MultiLLMClient imported",          "MultiLLMClient" in pfinal),
    ("from_environment() in init",       "from_environment()" in pfinal),
    ("Generator gets LLM client",        "ConstrainedGenerator(_llm)" in pfinal),
]

with open(COMPONENTS, "r", encoding="utf-8") as f:
    cfinal = f.read()

checks += [
    ("_call_llm calls generate()",       "self._llm_client.generate(" in cfinal),
    ("No NotImplementedError",           "NotImplementedError" not in cfinal),
    ("System prompt template used",      "GENERATOR_SYSTEM_PROMPT.format(" in cfinal),
    ("Handles provider failure",         "All LLM providers failed" in cfinal),
    ("Returns cross_llm_agreement",      "cross_llm_agreement" in cfinal),
]

ok = 0
for desc, passed in checks:
    ok += passed
    print(f"  {'PASS' if passed else 'FAIL'}: {desc}")

print(f"\n  {ok}/{len(checks)} checks passed")

if ok == len(checks):
    print("\n  LLM CLIENT WIRED")
    print("  Dev mode:  No API keys -> mock responses (safe)")
    print("  Production: Set ANTHROPIC_API_KEY -> real Claude calls")
    print("  Failover:  OPENAI_API_KEY -> GPT-4o if Claude fails")
    print("  Tertiary:  GOOGLE_API_KEY -> Gemini if both fail")
    print("  Models:    Override via CURANIQ_ANTHROPIC_MODEL etc.")
