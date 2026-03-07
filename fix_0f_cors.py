"""
CURANIQ Fix 0F: Environment-Driven CORS
No hardcoded origins. Reads from CURANIQ_CORS_ORIGINS env var.
Defaults to locked-down (localhost only) if not set.

Run in PowerShell:
  cd D:\curaniq_engine\curaniq_engine
  python fix_0f_cors.py
"""
import os, sys

BASE = r"D:\curaniq_engine\curaniq_engine"
MAIN = os.path.join(BASE, "curaniq", "api", "main.py")

if not os.path.exists(MAIN):
    print(f"ERROR: {MAIN} not found."); sys.exit(1)

with open(MAIN, "r", encoding="utf-8") as f:
    content = f.read()

OLD_CORS = '''app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)'''

NEW_CORS = '''# CORS: Environment-driven. No hardcoded origins.
# Set CURANIQ_CORS_ORIGINS="https://curaniq.com,https://app.curaniq.com"
# Default: localhost only (secure by default)
_cors_env = os.environ.get("CURANIQ_CORS_ORIGINS", "")
_cors_origins = (
    [o.strip() for o in _cors_env.split(",") if o.strip()]
    if _cors_env
    else ["http://localhost:3000", "http://localhost:8080"]
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "Authorization"],
)'''

if 'allow_origins=["*"]' in content:
    content = content.replace(OLD_CORS, NEW_CORS)
    # Add os import if missing at top
    if "import os" not in content.split("from fastapi")[0]:
        content = content.replace("from __future__ import annotations", 
                                  "from __future__ import annotations\nimport os")
    with open(MAIN, "w", encoding="utf-8") as f:
        f.write(content)
    print("PATCHED: CORS now environment-driven")
    print("  Default: localhost only (secure)")
    print("  Production: set CURANIQ_CORS_ORIGINS env var")
    print("  Methods restricted to GET, POST")
    print("  Headers restricted to Content-Type, Authorization")
elif "CURANIQ_CORS_ORIGINS" in content:
    print("SKIP: Already environment-driven")
else:
    print("WARNING: Could not find CORS block")

print("\n== VERIFICATION ==")
with open(MAIN, "r", encoding="utf-8") as f:
    final = f.read()

checks = [
    ('No wildcard origins',     '"*"' not in final.split("allow_origins")[1].split(")")[0] if "allow_origins" in final else False),
    ('Reads CURANIQ_CORS_ORIGINS env', 'CURANIQ_CORS_ORIGINS' in final),
    ('Secure default (localhost)',      'localhost' in final),
    ('Methods restricted',              '"GET", "POST"' in final),
    ('Headers restricted',              '"Content-Type", "Authorization"' in final),
]
ok = 0
for desc, passed in checks:
    ok += passed
    print(f"  {'PASS' if passed else 'FAIL'}: {desc}")
print(f"\n  {ok}/{len(checks)} passed")
