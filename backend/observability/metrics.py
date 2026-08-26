"""
Prometheus Metrics Exporter Subsystem.

Exposes low-cardinality, production-safe application metrics in Prometheus text format.
Guarantees zero high-cardinality label explosion (no user/workflow/request IDs).
"""

from __future__ import annotations

import threading
from typing import Any


class MetricsRegistry:
    """
    Thread-safe in-memory Prometheus metrics registry.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._counters: dict[str, dict[tuple[tuple[str, str], ...], float]] = {}
        self._histograms: dict[str, dict[tuple[tuple[str, str], ...], list[float]]] = {}

    def inc_counter(self, name: str, value: float = 1.0, labels: dict[str, str] | None = None) -> None:
        """Increment a counter metric."""
        label_key = tuple(sorted((labels or {}).items()))
        with self._lock:
            if name not in self._counters:
                self._counters[name] = {}
            self._counters[name][label_key] = self._counters[name].get(label_key, 0.0) + value

    def observe_histogram(self, name: str, value: float, labels: dict[str, str] | None = None) -> None:
        """Record an observation in a histogram."""
        label_key = tuple(sorted((labels or {}).items()))
        with self._lock:
            if name not in self._histograms:
                self._histograms[name] = {}
            if label_key not in self._histograms[name]:
                self._histograms[name][label_key] = []
            self._histograms[name][label_key].append(value)

    def get_counter_value(self, name: str, labels: dict[str, str] | None = None) -> float:
        """Retrieve current counter value."""
        label_key = tuple(sorted((labels or {}).items()))
        with self._lock:
            return self._counters.get(name, {}).get(label_key, 0.0)

    def generate_prometheus_text(self) -> str:
        """
        Generate Prometheus text exposition format string.
        """
        lines: list[str] = []
        with self._lock:
            # Format counters
            for metric_name, label_map in sorted(self._counters.items()):
                lines.append(f"# TYPE {metric_name} counter")
                for label_tuples, val in sorted(label_map.items()):
                    if label_tuples:
                        lbl_str = ",".join(f'{k}="{v}"' for k, v in label_tuples)
                        lines.append(f"{metric_name}{{{lbl_str}}} {val}")
                    else:
                        lines.append(f"{metric_name} {val}")

            # Format histograms
            for metric_name, label_map in sorted(self._histograms.items()):
                lines.append(f"# TYPE {metric_name} histogram")
                for label_tuples, values in sorted(label_map.items()):
                    count = len(values)
                    total = sum(values)
                    lbl_base = ",".join(f'{k}="{v}"' for k, v in label_tuples)
                    lbl_prefix = f"{{{lbl_base}," if lbl_base else "{"
                    lbl_close = "}"

                    lines.append(f"{metric_name}_count{lbl_prefix[:-1] if not lbl_base else lbl_prefix[:-1] + lbl_close} {count}")
                    lines.append(f"{metric_name}_sum{lbl_prefix[:-1] if not lbl_base else lbl_prefix[:-1] + lbl_close} {total:.4f}")

        return "\n".join(lines) + "\n"

    def clear(self) -> None:
        """Clear all metric values (for unit test isolation)."""
        with self._lock:
            self._counters.clear()
            self._histograms.clear()


# Global metrics registry singleton
metrics = MetricsRegistry()


def record_workflow_dispatched(scenario: str = "unknown", tenant_id: str = "tenant-default") -> None:
    """Record a workflow dispatch event."""
    metrics.inc_counter("recoveryos_workflows_dispatched_total", labels={"scenario": scenario, "tenant_id": tenant_id})


def record_publish_failure(backend: str = "pubsub") -> None:
    """Record a failed event publish attempt."""
    metrics.inc_counter("recoveryos_publish_failures_total", labels={"backend": backend})


def record_worker_execution(status: str, failure_type: str = "none") -> None:
    """Record worker execution delivery outcome."""
    metrics.inc_counter("recoveryos_worker_executions_total", labels={"status": status.lower(), "failure_type": failure_type.lower()})


def record_occ_mismatch() -> None:
    """Record an optimistic concurrency version conflict."""
    metrics.inc_counter("recoveryos_occ_mismatches_total")


def record_duplicate_claim() -> None:
    """Record a deduplicated duplicate message claim."""
    metrics.inc_counter("recoveryos_duplicate_claims_total")


def record_workflow_recovery(status: str = "dispatched") -> None:
    """Record an operator workflow recovery action."""
    metrics.inc_counter("recoveryos_recoveries_total", labels={"status": status.lower()})
