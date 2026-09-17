from collections import defaultdict
from threading import RLock


class RequestMetrics:
    """仅记录聚合运行指标，不保存问题或文档正文。"""

    def __init__(self) -> None:
        self._lock = RLock()
        self._counts: dict[tuple[str, str, int], int] = defaultdict(int)
        self._duration_ms: dict[tuple[str, str], list[float]] = defaultdict(list)

    def record(self, method: str, path: str, status: int, duration_ms: float) -> None:
        with self._lock:
            self._counts[(method, path, status)] += 1
            samples = self._duration_ms[(method, path)]
            samples.append(duration_ms)
            if len(samples) > 1000:
                del samples[:-1000]

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            requests = [
                {"method": method, "path": path, "status": status, "count": count}
                for (method, path, status), count in sorted(self._counts.items())
            ]
            latency = []
            for (method, path), values in sorted(self._duration_ms.items()):
                ordered = sorted(values)
                latency.append(
                    {
                        "method": method,
                        "path": path,
                        "samples": len(values),
                        "p50_ms": round(_percentile(ordered, 0.50), 3),
                        "p95_ms": round(_percentile(ordered, 0.95), 3),
                    }
                )
        return {"requests": requests, "latency": latency}


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    index = min(round((len(values) - 1) * percentile), len(values) - 1)
    return values[index]
