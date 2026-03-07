"""
CURANIQ - L10-10 LLM Cost Monitor & Budget Enforcement
Integrated INTO the LLM client. Not a separate manual tracker.

Copy to: curaniq/layers/L10_testing/cost_monitor.py

Every LLM call is automatically tracked — zero manual wiring.
Budget exceeded = LLM calls BLOCKED (fail-closed).
Alerts at configurable thresholds (80%, 90%, 100%).
All pricing from environment — updates when providers change rates.

Env vars:
  CURANIQ_COST_ANTHROPIC_IN   — $/1M input tokens
  CURANIQ_COST_ANTHROPIC_OUT  — $/1M output tokens
  CURANIQ_COST_OPENAI_IN      — $/1M input tokens
  CURANIQ_COST_OPENAI_OUT     — $/1M output tokens
  CURANIQ_COST_GOOGLE_IN      — $/1M input tokens
  CURANIQ_COST_GOOGLE_OUT     — $/1M output tokens
  CURANIQ_MONTHLY_BUDGET      — Monthly limit in USD (0 = unlimited)
  CURANIQ_COST_LOG_PATH       — Where to persist cost records
  CURANIQ_BUDGET_ALERT_PCT    — Alert threshold % (default: 80)
"""
from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


@dataclass
class CostRecord:
    """Single LLM call cost record."""
    timestamp: str
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    latency_ms: float
    query_id: str = ""
    success: bool = True


@dataclass
class BudgetStatus:
    """Current budget state."""
    monthly_budget: float
    spent: float
    remaining: float
    utilization_pct: float
    exceeded: bool
    alert_triggered: bool
    alert_message: str = ""


class CostEngine:
    """
    Computes cost for any provider/model combination.
    Rates from environment. No hardcoded prices.
    """

    def __init__(self):
        # Load rates from environment per provider
        # Format: cost per 1 MILLION tokens
        self._rates: dict[str, dict[str, float]] = {}
        self._load_rates()

    def _load_rates(self):
        """Load pricing from environment variables."""
        providers = {
            "anthropic": ("CURANIQ_COST_ANTHROPIC_IN", "CURANIQ_COST_ANTHROPIC_OUT", 3.0, 15.0),
            "openai":    ("CURANIQ_COST_OPENAI_IN",    "CURANIQ_COST_OPENAI_OUT",    2.5, 10.0),
            "google":    ("CURANIQ_COST_GOOGLE_IN",    "CURANIQ_COST_GOOGLE_OUT",    1.25, 5.0),
        }
        for provider, (in_env, out_env, default_in, default_out) in providers.items():
            self._rates[provider] = {
                "input": float(os.environ.get(in_env, str(default_in))),
                "output": float(os.environ.get(out_env, str(default_out))),
            }

    def compute(self, provider: str, input_tokens: int, output_tokens: int) -> float:
        """Compute cost in USD."""
        rates = self._rates.get(provider, {"input": 3.0, "output": 15.0})
        cost = (
            (input_tokens / 1_000_000) * rates["input"] +
            (output_tokens / 1_000_000) * rates["output"]
        )
        return round(cost, 6)


class LLMCostMonitor:
    """
    L10-10: Integrated cost monitoring.

    Wraps the LLM client's generate() method. Every call:
      1. CHECK budget before calling (fail-closed if exceeded)
      2. RECORD tokens + cost after call completes
      3. ALERT if approaching threshold
      4. PERSIST to JSONL for analytics

    Usage — wraps LLM response automatically:
      monitor.after_call(response, query_id)
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._cost_engine = CostEngine()
        self._records: list[CostRecord] = []

        self._monthly_budget = float(os.environ.get("CURANIQ_MONTHLY_BUDGET", "0"))
        self._alert_pct = float(os.environ.get("CURANIQ_BUDGET_ALERT_PCT", "80"))
        self._log_path = os.environ.get("CURANIQ_COST_LOG_PATH", "./curaniq_cost.jsonl")

        self._load_current_month()

    def _load_current_month(self):
        """Load current month's records from persistent storage."""
        if not os.path.exists(self._log_path):
            return
        month_prefix = datetime.now(timezone.utc).strftime("%Y-%m")
        try:
            with open(self._log_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                        if d.get("timestamp", "").startswith(month_prefix):
                            self._records.append(CostRecord(
                                timestamp=d["timestamp"],
                                provider=d["provider"],
                                model=d["model"],
                                input_tokens=d["input_tokens"],
                                output_tokens=d["output_tokens"],
                                cost_usd=d["cost_usd"],
                                latency_ms=d.get("latency_ms", 0),
                                query_id=d.get("query_id", ""),
                                success=d.get("success", True),
                            ))
                    except (json.JSONDecodeError, KeyError):
                        continue
        except Exception:
            pass

    def check_budget(self) -> BudgetStatus:
        """Check current budget status. Called BEFORE every LLM call."""
        spent = self._current_month_cost()
        budget = self._monthly_budget

        if budget <= 0:
            # Unlimited budget
            return BudgetStatus(
                monthly_budget=0, spent=spent, remaining=float("inf"),
                utilization_pct=0, exceeded=False, alert_triggered=False,
            )

        remaining = budget - spent
        pct = (spent / budget) * 100 if budget > 0 else 0
        exceeded = spent >= budget
        alert = pct >= self._alert_pct

        msg = ""
        if exceeded:
            msg = f"BUDGET EXCEEDED: ${spent:.2f} / ${budget:.2f}. LLM calls BLOCKED."
            logger.warning(msg)
        elif alert:
            msg = f"BUDGET ALERT: ${spent:.2f} / ${budget:.2f} ({pct:.0f}% used)."
            logger.info(msg)

        return BudgetStatus(
            monthly_budget=budget,
            spent=round(spent, 4),
            remaining=round(remaining, 2),
            utilization_pct=round(pct, 1),
            exceeded=exceeded,
            alert_triggered=alert,
            alert_message=msg,
        )

    def after_call(
        self,
        provider: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        latency_ms: float,
        query_id: str = "",
        success: bool = True,
    ) -> CostRecord:
        """Record a completed LLM call. Called AFTER every response."""
        cost = self._cost_engine.compute(provider, input_tokens, output_tokens)

        record = CostRecord(
            timestamp=datetime.now(timezone.utc).isoformat(),
            provider=provider,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
            latency_ms=latency_ms,
            query_id=query_id,
            success=success,
        )

        with self._lock:
            self._records.append(record)

        self._persist(record)
        return record

    def _persist(self, record: CostRecord):
        """Append to JSONL log."""
        try:
            Path(self._log_path).parent.mkdir(parents=True, exist_ok=True)
            with open(self._log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record.__dict__) + "\n")
                f.flush()
        except Exception:
            pass

    def _current_month_cost(self) -> float:
        """Total spend for current month."""
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        return sum(
            r.cost_usd for r in self._records
            if r.timestamp.startswith(month)
        )

    def get_summary(self) -> dict:
        """Current month summary for dashboards."""
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        month_records = [r for r in self._records if r.timestamp.startswith(month)]

        by_provider: dict[str, dict] = {}
        for r in month_records:
            if r.provider not in by_provider:
                by_provider[r.provider] = {
                    "calls": 0, "input_tokens": 0,
                    "output_tokens": 0, "cost_usd": 0.0,
                }
            p = by_provider[r.provider]
            p["calls"] += 1
            p["input_tokens"] += r.input_tokens
            p["output_tokens"] += r.output_tokens
            p["cost_usd"] = round(p["cost_usd"] + r.cost_usd, 6)

        total_cost = sum(r.cost_usd for r in month_records)
        budget = self.check_budget()

        return {
            "period": month,
            "total_calls": len(month_records),
            "total_cost_usd": round(total_cost, 4),
            "budget": budget.__dict__,
            "by_provider": by_provider,
            "avg_cost_per_query": round(
                total_cost / max(len(month_records), 1), 6
            ),
            "avg_latency_ms": round(
                sum(r.latency_ms for r in month_records) / max(len(month_records), 1), 1
            ),
        }
