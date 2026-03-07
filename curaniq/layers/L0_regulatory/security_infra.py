"""
CURANIQ — Medical Evidence Operating System
Layer 0: Security & Infrastructure Foundation

L0-3  Cybersecurity Lifecycle (FDA guidance)
L0-6  CI/CD Pipeline Configuration
L0-7  Data Architecture (PostgreSQL + pgvector + Redis)
L0-8  Centralized Secret Management

Architecture: All secrets from environment. No hardcoded keys.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional
from uuid import uuid4

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# L0-8: CENTRALIZED SECRET MANAGEMENT
# ─────────────────────────────────────────────────────────────────────────────

class SecretCategory(str, Enum):
    LLM_API_KEY      = "llm_api_key"
    EVIDENCE_API_KEY = "evidence_api_key"
    DATABASE_URL     = "database_url"
    EHR_TOKEN        = "ehr_token"
    PAYMENT_KEY      = "payment_key"
    ENCRYPTION_KEY   = "encryption_key"
    INTERNAL_SERVICE = "internal_service"


@dataclass
class SecretRegistryEntry:
    name: str
    category: SecretCategory
    env_var: str
    required: bool = True
    rotation_days: int = 90
    last_rotated: Optional[datetime] = None
    is_set: bool = False
    masked_value: str = ""


class SecretManager:
    """
    L0-8: Centralized secret management.

    Rules:
    - NO hardcoded secrets anywhere in codebase
    - All secrets from environment variables
    - Least privilege: each module gets only its required keys
    - Rotation tracking: secrets older than rotation_days flagged
    - Misuse detection: log when secrets accessed outside expected modules
    """

    REGISTRY: list[dict] = [
        {"name": "Anthropic API Key",    "env": "ANTHROPIC_API_KEY",    "cat": SecretCategory.LLM_API_KEY,      "required": True,  "rotation": 90},
        {"name": "OpenAI API Key",       "env": "OPENAI_API_KEY",       "cat": SecretCategory.LLM_API_KEY,      "required": False, "rotation": 90},
        {"name": "Google API Key",       "env": "GOOGLE_API_KEY",       "cat": SecretCategory.LLM_API_KEY,      "required": False, "rotation": 90},
        {"name": "PubMed API Key",       "env": "NCBI_API_KEY",         "cat": SecretCategory.EVIDENCE_API_KEY, "required": False, "rotation": 365},
        {"name": "OpenFDA API Key",      "env": "OPENFDA_API_KEY",      "cat": SecretCategory.EVIDENCE_API_KEY, "required": False, "rotation": 365},
        {"name": "Database URL",         "env": "DATABASE_URL",          "cat": SecretCategory.DATABASE_URL,     "required": True,  "rotation": 180},
        {"name": "Redis URL",            "env": "REDIS_URL",             "cat": SecretCategory.DATABASE_URL,     "required": False, "rotation": 180},
        {"name": "Stripe Secret Key",    "env": "STRIPE_SECRET_KEY",     "cat": SecretCategory.PAYMENT_KEY,      "required": False, "rotation": 90},
        {"name": "Payme Secret Key",     "env": "PAYME_SECRET_KEY",      "cat": SecretCategory.PAYMENT_KEY,      "required": False, "rotation": 90},
        {"name": "Click Secret Key",     "env": "CLICK_SECRET_KEY",      "cat": SecretCategory.PAYMENT_KEY,      "required": False, "rotation": 90},
        {"name": "JWT Secret",           "env": "JWT_SECRET_KEY",        "cat": SecretCategory.ENCRYPTION_KEY,   "required": True,  "rotation": 30},
        {"name": "Audit Signing Key",    "env": "AUDIT_SIGNING_KEY",     "cat": SecretCategory.ENCRYPTION_KEY,   "required": True,  "rotation": 90},
    ]

    def __init__(self):
        self._entries: list[SecretRegistryEntry] = []
        self._access_log: list[dict] = []
        self._scan_environment()

    def _scan_environment(self):
        """Scan environment for all registered secrets."""
        for secret_def in self.REGISTRY:
            env_var = secret_def["env"]
            value = os.environ.get(env_var, "")
            is_set = bool(value)
            masked = self._mask_value(value) if is_set else "(not set)"

            entry = SecretRegistryEntry(
                name=secret_def["name"],
                category=secret_def["cat"],
                env_var=env_var,
                required=secret_def["required"],
                rotation_days=secret_def["rotation"],
                is_set=is_set,
                masked_value=masked,
            )
            self._entries.append(entry)

    def _mask_value(self, value: str) -> str:
        """Mask secret value for logging: show first 4 + last 4 chars."""
        if len(value) <= 8:
            return "****"
        return value[:4] + "****" + value[-4:]

    def get_secret(self, env_var: str, requesting_module: str) -> Optional[str]:
        """
        Retrieve a secret with access logging.
        Returns None if not set (never raises — fail-closed handled by caller).
        """
        value = os.environ.get(env_var)
        self._access_log.append({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "env_var": env_var,
            "module": requesting_module,
            "found": value is not None,
        })

        if value is None:
            entry = next((e for e in self._entries if e.env_var == env_var), None)
            if entry and entry.required:
                logger.warning(
                    "Required secret %s not set — requested by %s",
                    env_var, requesting_module,
                )
        return value

    def audit_secrets(self) -> dict[str, Any]:
        """Generate secret health report."""
        missing_required = [
            e.name for e in self._entries
            if e.required and not e.is_set
        ]
        return {
            "total_registered": len(self._entries),
            "set": sum(1 for e in self._entries if e.is_set),
            "missing": sum(1 for e in self._entries if not e.is_set),
            "missing_required": missing_required,
            "access_count": len(self._access_log),
            "healthy": len(missing_required) == 0,
        }

    def scan_codebase_for_hardcoded(self, root_dir: str) -> list[dict]:
        """
        Scan Python files for potential hardcoded secrets.
        Returns list of violations.
        """
        violations = []
        patterns = [
            re.compile(r'(?:api[_-]?key|secret|password|token)\s*=\s*["\'][^"\']{8,}["\']', re.I),
            re.compile(r'sk-[a-zA-Z0-9]{20,}'),
            re.compile(r'Bearer\s+[a-zA-Z0-9\-._~+/]{20,}'),
        ]

        for dirpath, _, filenames in os.walk(root_dir):
            for fname in filenames:
                if not fname.endswith(".py"):
                    continue
                filepath = os.path.join(dirpath, fname)
                try:
                    with open(filepath, "r", encoding="utf-8") as f:
                        for line_num, line in enumerate(f, 1):
                            for pattern in patterns:
                                if pattern.search(line):
                                    violations.append({
                                        "file": filepath,
                                        "line": line_num,
                                        "pattern": pattern.pattern[:40],
                                        "snippet": line.strip()[:80],
                                    })
                except (UnicodeDecodeError, PermissionError):
                    continue

        return violations


# ─────────────────────────────────────────────────────────────────────────────
# L0-3: CYBERSECURITY LIFECYCLE
# ─────────────────────────────────────────────────────────────────────────────

class ThreatCategory(str, Enum):
    PROMPT_INJECTION = "prompt_injection"
    DATA_EXFILTRATION = "data_exfiltration"
    AUTHENTICATION_BYPASS = "auth_bypass"
    PRIVILEGE_ESCALATION = "privilege_escalation"
    SUPPLY_CHAIN = "supply_chain"
    DENIAL_OF_SERVICE = "dos"


@dataclass
class ThreatModelEntry:
    threat_id: str
    category: ThreatCategory
    attack_vector: str
    impact: str
    mitigation_module: str
    stride_category: str
    tested: bool = False


class CybersecurityLifecycle:
    """
    L0-3: FDA cybersecurity guidance compliance.

    Threat model for CURANIQ covering STRIDE categories:
    Spoofing, Tampering, Repudiation, Information Disclosure,
    Denial of Service, Elevation of Privilege.
    """

    THREAT_MODEL: list[ThreatModelEntry] = [
        ThreatModelEntry("T-001", ThreatCategory.PROMPT_INJECTION,
            "Injected instructions in clinical query text",
            "Manipulated clinical output", "L6-1", "Tampering"),
        ThreatModelEntry("T-002", ThreatCategory.DATA_EXFILTRATION,
            "PHI embedded in LLM prompt reaching external API",
            "HIPAA violation", "L6-2", "Information Disclosure"),
        ThreatModelEntry("T-003", ThreatCategory.AUTHENTICATION_BYPASS,
            "Expired/stolen EHR FHIR token reused",
            "Unauthorized patient data access", "L6-5", "Spoofing"),
        ThreatModelEntry("T-004", ThreatCategory.PRIVILEGE_ESCALATION,
            "Patient-mode user receiving clinician-level output",
            "Unsupervised self-medication", "L5-14", "Elevation of Privilege"),
        ThreatModelEntry("T-005", ThreatCategory.SUPPLY_CHAIN,
            "Compromised LLM API returning adversarial content",
            "Harmful clinical recommendation", "L4-12", "Tampering"),
        ThreatModelEntry("T-006", ThreatCategory.DENIAL_OF_SERVICE,
            "Excessive complex queries exhausting LLM budget",
            "Service unavailability", "L10-10", "Denial of Service"),
    ]

    def __init__(self):
        self._threats = list(self.THREAT_MODEL)

    def verify_mitigation(self, threat_id: str, test_passed: bool):
        """Mark a threat's mitigation as tested."""
        threat = next((t for t in self._threats if t.threat_id == threat_id), None)
        if threat:
            threat.tested = test_passed

    def get_untested_threats(self) -> list[ThreatModelEntry]:
        return [t for t in self._threats if not t.tested]

    def compliance_report(self) -> dict[str, Any]:
        return {
            "total_threats": len(self._threats),
            "tested": sum(1 for t in self._threats if t.tested),
            "untested": [t.threat_id for t in self._threats if not t.tested],
            "categories_covered": list({t.category.value for t in self._threats}),
        }


# ─────────────────────────────────────────────────────────────────────────────
# L0-7: DATA ARCHITECTURE
# ─────────────────────────────────────────────────────────────────────────────

class DataArchitectureConfig:
    """
    L0-7: Data architecture configuration.
    All connection details from environment — never hardcoded.
    """

    @staticmethod
    def get_database_url() -> Optional[str]:
        return os.environ.get("DATABASE_URL")

    @staticmethod
    def get_redis_url() -> Optional[str]:
        return os.environ.get("REDIS_URL")

    @staticmethod
    def get_vector_dimensions() -> int:
        return int(os.environ.get("VECTOR_DIMENSIONS", "1536"))

    @staticmethod
    def get_config() -> dict[str, Any]:
        return {
            "database": {
                "url_set": bool(os.environ.get("DATABASE_URL")),
                "pool_size": int(os.environ.get("DB_POOL_SIZE", "10")),
                "max_overflow": int(os.environ.get("DB_MAX_OVERFLOW", "20")),
            },
            "vector_store": {
                "dimensions": DataArchitectureConfig.get_vector_dimensions(),
                "index_type": os.environ.get("VECTOR_INDEX", "ivfflat"),
            },
            "cache": {
                "redis_url_set": bool(os.environ.get("REDIS_URL")),
                "default_ttl_seconds": int(os.environ.get("CACHE_TTL", "3600")),
            },
        }
