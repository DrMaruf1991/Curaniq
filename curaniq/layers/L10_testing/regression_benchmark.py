"""
CURANIQ — L10-2 Synthetic Patient Regression + L10-4 Benchmark Dashboard
Re-exports from L9 citation_provenance module where implementation lives.
"""
from curaniq.layers.L9_audit_payments.citation_provenance import (
    SyntheticPatientRegression,
    SyntheticPatientCase,
    RegressionResult,
    BenchmarkDashboard,
    BenchmarkMetric,
)

__all__ = [
    "SyntheticPatientRegression",
    "SyntheticPatientCase",
    "RegressionResult",
    "BenchmarkDashboard",
    "BenchmarkMetric",
]
