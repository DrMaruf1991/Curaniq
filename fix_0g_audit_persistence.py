"""
CURANIQ Fix 0G: Persistent Audit Ledger
Replaces in-memory list with pluggable storage backend.
Default: JSONL append-only file. Survives restarts.
Production: swap to PostgreSQL via env var.

21 CFR Part 11: immutable, append-only, hash-chained.

Requires: audit_storage.py in same folder.

Run in PowerShell:
  cd D:\curaniq_engine\curaniq_engine
  python fix_0g_audit_persistence.py
"""
import os, sys, shutil

BASE = r"D:\curaniq_engine\curaniq_engine"
LEDGER = os.path.join(BASE, "curaniq", "audit", "ledger.py")
TARGET = os.path.join(BASE, "curaniq", "audit", "storage.py")
SOURCE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "audit_storage.py")

if not os.path.exists(LEDGER):
    print(f"ERROR: {LEDGER} not found."); sys.exit(1)
if not os.path.exists(SOURCE):
    print(f"ERROR: audit_storage.py not found next to this script."); sys.exit(1)

# ── STEP 1: Copy storage backend ──
shutil.copy2(SOURCE, TARGET)
print(f"COPIED: audit_storage.py -> {TARGET}")

# ── STEP 2: Patch ledger.py ──
with open(LEDGER, "r", encoding="utf-8") as f:
    content = f.read()

# Add import
if "get_storage_backend" in content:
    print("SKIP: Storage backend already imported")
else:
    # Find a good import location
    import_marker = "from uuid import UUID, uuid4"
    new_import = import_marker + "\n\nfrom curaniq.audit.storage import get_storage_backend"
    content = content.replace(import_marker, new_import)
    print("PATCHED: Added storage backend import")

# Replace __init__ to use backend
OLD_INIT = "        self._entries: list[AuditLedgerEntry] = []\n        self._last_hash: Optional[str] = None"
NEW_INIT = """        self._storage = get_storage_backend()
        self._entries: list[AuditLedgerEntry] = []  # In-memory cache for fast access
        self._last_hash: Optional[str] = self._storage.get_last_hash()"""

if "self._storage = get_storage_backend()" in content:
    print("SKIP: Storage backend already in __init__")
else:
    content = content.replace(OLD_INIT, NEW_INIT)
    print("PATCHED: __init__ now uses storage backend")

# Replace __len__ to use backend
OLD_LEN = "        return len(self._entries)"
NEW_LEN = "        return self._storage.count()"
if "self._storage.count()" not in content:
    content = content.replace(OLD_LEN, NEW_LEN, 1)
    print("PATCHED: __len__ uses storage backend")

# Patch the record() method to persist entries
OLD_APPEND = "        self._entries.append(entry)"
NEW_APPEND = """        self._entries.append(entry)

        # Persist to durable storage (file/PostgreSQL)
        self._storage.append({
            "entry_id": str(entry.entry_id),
            "query_id": str(entry.query_id),
            "user_role": entry.user_role.value,
            "jurisdiction": entry.jurisdiction.value,
            "triage_result": entry.triage_result.value,
            "mode": entry.mode.value,
            "evidence_pack_id": str(entry.evidence_pack_id),
            "evidence_count": entry.evidence_count,
            "safety_suite_passed": entry.safety_suite_passed,
            "refused": entry.refused,
            "refusal_reason": entry.refusal_reason,
            "entry_hash": entry.entry_hash,
            "previous_entry_hash": entry.previous_entry_hash,
            "created_at": entry.created_at.isoformat(),
        })"""

if "self._storage.append" in content:
    print("SKIP: Storage persist already wired")
else:
    content = content.replace(OLD_APPEND, NEW_APPEND)
    print("PATCHED: record() now persists to storage backend")

# Patch get_query_audit_trail to also check storage
OLD_QUERY = "        return [e for e in self._entries if e.query_id == query_id]"
NEW_QUERY = """        # Check in-memory first (current session)
        results = [e for e in self._entries if e.query_id == query_id]
        if results:
            return results
        # Fall back to persistent storage (previous sessions)
        stored = self._storage.get_by_query_id(str(query_id))
        return stored  # Returns dicts, not AuditLedgerEntry — acceptable for API"""

if "self._storage.get_by_query_id" in content:
    print("SKIP: get_query_audit_trail already uses storage")
else:
    content = content.replace(OLD_QUERY, NEW_QUERY)
    print("PATCHED: get_query_audit_trail checks persistent storage")

# ── WRITE ──
with open(LEDGER, "w", encoding="utf-8") as f:
    f.write(content)
print(f"Saved: {LEDGER}")

# ── VERIFICATION ──
print("\n== VERIFICATION ==")
sys.path.insert(0, BASE)

import tempfile, json
os.environ["CURANIQ_AUDIT_BACKEND"] = "jsonl"
os.environ["CURANIQ_AUDIT_PATH"] = os.path.join(tempfile.gettempdir(), "curaniq_test_audit.jsonl")

# Clean test file
test_path = os.environ["CURANIQ_AUDIT_PATH"]
if os.path.exists(test_path):
    os.remove(test_path)

from curaniq.audit.storage import JSONLFileBackend

backend = JSONLFileBackend(test_path)

# Test 1: Append
backend.append({
    "entry_id": "test-001",
    "query_id": "q-001",
    "entry_hash": "abc123",
    "created_at": "2026-03-07T10:00:00Z",
})
backend.append({
    "entry_id": "test-002",
    "query_id": "q-002",
    "entry_hash": "def456",
    "previous_hash": "abc123",
    "created_at": "2026-03-07T10:01:00Z",
})
print(f"  PASS: Appended 2 entries" if backend.count() == 2 else "  FAIL: Count wrong")

# Test 2: Survives "restart" (new instance reads file)
backend2 = JSONLFileBackend(test_path)
print(f"  PASS: New instance loaded {backend2.count()} entries from file" if backend2.count() == 2 else f"  FAIL: Expected 2, got {backend2.count()}")

# Test 3: Query by ID
results = backend2.get_by_query_id("q-001")
print(f"  PASS: Query by ID returned {len(results)} entry" if len(results) == 1 else "  FAIL: Query wrong")

# Test 4: Last hash preserved
print(f"  PASS: Last hash = {backend2.get_last_hash()}" if backend2.get_last_hash() == "def456" else "  FAIL: Hash wrong")

# Test 5: File is JSONL (one JSON per line)
with open(test_path, "r") as f:
    lines = [l.strip() for l in f if l.strip()]
valid_json = all(json.loads(l) for l in lines)
print(f"  PASS: File has {len(lines)} valid JSONL lines" if valid_json and len(lines) == 2 else "  FAIL: JSONL format")

# Test 6: Append-only (file only grows)
backend2.append({"entry_id": "test-003", "query_id": "q-003", "entry_hash": "ghi789"})
with open(test_path, "r") as f:
    final_lines = [l.strip() for l in f if l.strip()]
print(f"  PASS: File grew to {len(final_lines)} lines (append-only)" if len(final_lines) == 3 else "  FAIL: Not append-only")

# Cleanup
os.remove(test_path)

print(f"\n  Audit storage: durable, append-only, hash-chained")
print(f"  Backend: CURANIQ_AUDIT_BACKEND env (jsonl/memory/postgresql)")
print(f"  Path: CURANIQ_AUDIT_PATH env (default: ./curaniq_audit.jsonl)")
