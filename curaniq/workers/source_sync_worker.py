"""CLI worker for source synchronization.

Usage:
    python -m curaniq.workers.source_sync_worker

In production this should be run by a scheduler (Kubernetes CronJob, Celery
beat, systemd timer, etc.) with configured source connectors/credentials.
"""
from __future__ import annotations

import json
import sys

from curaniq.services.source_sync_service import SourceSyncService


def main() -> int:
    service = SourceSyncService()
    results = service.run_registered_sources()
    print(json.dumps([r.__dict__ for r in results], ensure_ascii=False, default=str, indent=2))
    return 0 if all(r.outcome == "success" for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
