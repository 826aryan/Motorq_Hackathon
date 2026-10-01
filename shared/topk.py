"""Top-K with a min-heap of size K: one pass, O(n log K) time, O(K) memory (SPEC §6 Top-K history)."""
import heapq
from collections.abc import Iterable


def top_k(items: Iterable[tuple[str, float]], k: int) -> list[tuple[str, float]]:
    """Highest-scoring (key, score) pairs, best first. The heap's root is the weakest of the current top K,
    so each new item is compared with it once and only replaces it when larger."""
    heap: list[tuple[float, str]] = []
    for key, score in items:
        if len(heap) < k:
            heapq.heappush(heap, (score, key))
        elif score > heap[0][0]:
            heapq.heapreplace(heap, (score, key))
    return [(key, score) for score, key in sorted(heap, reverse=True)]
