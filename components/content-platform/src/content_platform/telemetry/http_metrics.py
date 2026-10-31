"""Thread-safe, bounded HTTP request measurements."""

from collections import Counter, deque
import math
import threading
import time


class RequestMetrics:
    def __init__(self, *, max_samples: int = 5_000) -> None:
        if max_samples < 1:
            raise ValueError("max_samples must be positive")
        self._lock = threading.Lock()
        self._started_at = time.monotonic()
        self._durations_ms: deque[float] = deque(maxlen=max_samples)
        self._status_counts: Counter[str] = Counter()
        self._counters: Counter[str] = Counter()
        self._requests = 0

    def record(self, status: int, duration_ms: float) -> None:
        with self._lock:
            self._requests += 1
            self._status_counts[str(status)] += 1
            self._durations_ms.append(max(0.0, duration_ms))

    def increment(self, name: str, amount: int = 1) -> None:
        with self._lock:
            self._counters[name] += amount

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            uptime = max(time.monotonic() - self._started_at, 1e-9)
            samples = sorted(self._durations_ms)
            requests = self._requests
            statuses = dict(self._status_counts)
            counters = dict(self._counters)
        return {
            "uptime_seconds": round(uptime, 6),
            "requests_total": requests,
            "average_requests_per_second": round(requests / uptime, 3),
            "http_errors_total": sum(
                count for status, count in statuses.items() if int(status) >= 400
            ),
            "status_counts": statuses,
            "counters": counters,
            "latency_sample_count": len(samples),
            "latency_sample_capacity": self._durations_ms.maxlen,
            "latency_ms": {
                "p50": self._percentile(samples, 0.50),
                "p95": self._percentile(samples, 0.95),
                "p99": self._percentile(samples, 0.99),
            },
        }

    @staticmethod
    def _percentile(samples: list[float], percentile: float) -> float | None:
        if not samples:
            return None
        rank = max(1, math.ceil(percentile * len(samples)))
        return round(samples[rank - 1], 3)
