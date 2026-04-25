"""
CURANIQ Clinical Knowledge — L1 connector implementations.

Each connector wraps one governed evidence source with proper
rate-limiting, retry/backoff, and error handling. Connectors are
injected into `LiveEvidenceProvider` to fulfill the
`ClinicalKnowledgeProvider` contract.

See docs/MIGRATION_PLAYBOOK.md for the per-session source map.
"""
from curaniq.knowledge.connectors.rxnorm import RxNormConnector

__all__ = ["RxNormConnector"]
