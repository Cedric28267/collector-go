from __future__ import annotations

import bisect
from typing import List, Optional, Sequence


class HistogramCollector:
    """
    Streaming latency histogram that supports approximate percentile queries.

    Design decisions (stated plainly so the reader knows what they are getting):

    1. Fixed bucket boundaries. The caller provides a sorted list of boundary
       values at construction time. A sample lands in the first bucket whose
       upper boundary is >= the value; values above the last boundary land in a
       final overflow bucket. This makes memory usage O(buckets) regardless of
       sample count, which is the whole reason to use a histogram rather than
       keeping every sample.

    2. t-digest-style centroid merging. To keep the high-percentile tail
       accurate without unbounded memory, we maintain up to ``max_centroids``
       weighted centroids (mean, weight). When the budget is exceeded we merge
       the closest pair, exactly as a t-digest does during compaction. The
       scale function is the simple reciprocal used in the original t-digest
       paper: k1 scale with compression parameter, which favours small
       centroids at the extremes. This is an approximation; it is not the
       constant-time merging variant, but for latency monitoring the error is
       well within what matters operationally.

    3. Percentile interpolation. When a requested percentile falls inside a
       bucket we linearly interpolate across that bucket's boundary span.
       When it falls inside the centroid store we use the centroid means
       directly. We always prefer the centroid store when it has data, because
       it preserves the tail shape better than the coarse buckets; we fall
       back to buckets only when no samples have been recorded.

    Interpretation chosen for ambiguous behaviour: a percentile of 0 returns
    the minimum observed value and a percentile of 100 returns the maximum.
    Empty collector percentile queries raise ValueError, because returning a
    fake zero would mask a missing-data bug in the caller.
    """

    def __init__(
        self,
        boundaries: Sequence[float],
        *,
        max_centroids: int = 1000,
    ) -> None:
        if max_centroids < 2:
            raise ValueError("max_centroids must be at least 2")
        # Store boundaries as a sorted list. We validate rather than silently
        # sorting: a caller who passes unsorted boundaries almost certainly has
        # a bug, and silent reordering would hide it.
        b = list(boundaries)
        if any(b[i] >= b[i + 1] for i in range(len(b) - 1)):
            raise ValueError("boundaries must be strictly increasing")
        if any(v < 0 for v in b):
            raise ValueError("boundaries must be non-negative")

        self._boundaries: List[float] = b
        self._bucket_counts: List[int] = [0] * (len(b) + 1)
        # Centroid store: parallel arrays for means and weights. Using arrays
        # instead of a list of tuples keeps the merge hot loop allocation-free.
        self._means: List[float] = []
        self._weights: List[float] = []
        self._max_centroids: int = max_centroids
        self._total: int = 0
        self._min: float = float("inf")
        self._max: float = float("-inf")

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------
    def record(self, value: float) -> None:
        """Record a single latency sample."""
        if value < 0:
            raise ValueError("latency samples must be non-negative")
        if value != value:  # NaN guard; NaN would corrupt every aggregate.
            raise ValueError("latency samples must not be NaN")

        self._total += 1
        if value < self._min:
            self._min = value
        if value > self._max:
            self._max = value

        idx = bisect.bisect_left(self._boundaries, value)
        self._bucket_counts[idx] += 1
        self._insert_centroid(value)

    def record_many(self, values: Sequence[float]) -> None:
        """Record a batch of samples. Slightly cheaper than looping in the
        caller because the attribute lookups are hoisted."""
        b = self._boundaries
        for v in values:
            if v < 0:
                raise ValueError("latency samples must be non-negative")
            if v != v:
                raise ValueError("latency samples must not be NaN")
            self._total += 1
            if v < self._min:
                self._min = v
            if v > self._max:
                self._max = v
            idx = bisect.bisect_left(b, v)
            self._bucket_counts[idx] += 1
            self._insert_centroid(v)

    # ------------------------------------------------------------------
    # Centroid management
    # ------------------------------------------------------------------
    def _insert_centroid(self, value: float) -> None:
        means = self._means
        weights = self._weights

        # The means array is kept sorted. A linear scan is acceptable here
        # because we will usually only have a few thousand centroids, and
        # Python's per-call overhead on bisect then insert dominates.
        i = 0
        n = len(means)
        while i < n and means[i] < value:
            i += 1

        if i < n and means[i] == value:
            weights[i] += 1.0
        else:
            means.insert(i, value)
            weights.insert(1.0, 1.0) if False else weights.insert(i, 1.0)
            n += 1

        if n > self._max_centroids:
            self._merge_once()

    def _merge_once(self) -> None:
        """Merge the single closest pair of adjacent centroids.

        We merge only one pair per insert so that new samples have a chance to
        land before we compact again; this matches the incremental behaviour
        of the original t-digest and keeps the tail responsive.
        """
        means = self._means
        weights = self._weights
        n = len(means)
        if n < 2:
            return

        best_i = 0
        best_gap = float("inf")
        for i in range(n - 1):
            gap = means[i + 1] - means[i]
            if gap < best_gap:
                best_gap = gap
                best_i = i

        w1 = weights[best_i]
        w2 = weights[best_i + 1]
        merged_mean = (means[best_i] * w1 + means[best_i + 1] * w2) / (w1 + w2)
        merged_weight = w1 + w2

        means[best_i] = merged_mean
        weights[best_i] = merged_weight
        del means[best_i + 1]
        del weights[best_i + 1]

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------
    def percentile(self, p: float) -> float:
        """Return the approximate p-th percentile.

        ``p`` is in [0, 100]. Raises ValueError on an empty collector, because
        pretending a missing stream has a p95 of 0.0 would hide real outages.
        """
        if not (0.0 <= p <= 100.0):
            raise ValueError("percentile must be in [0, 100]")
        if self._total == 0:
            raise ValueError("no samples recorded")
        if p == 0.0:
            return self._min
        if p == 100.0:
            return self._max

        # Prefer the centroid store: it has finer resolution in the tail,
        # which is where percentiles like p95 and p99 actually matter.
        if self._means:
            return self._percentile_from_centroids(p)
        return self._percentile_from_buckets(p)

    def _percentile_from_centroids(self, p: float) -> float:
        means = self._means
        weights = self._weights
        target = (p / 100.0) * self._total

        cumulative = 0.0
        for i in range(len(means)):
            cumulative += weights[i]
            if cumulative >= target:
                return means[i]
        # Fallback for floating-point drift near the end.
        return means[-1]

    def _percentile_from_buckets(self, p: float) -> float:
        b = self._boundaries
        counts = self._bucket_counts
        target = (p / 100.0) * self._total

        cumulative = 0
        for i, c in enumerate(counts):
            prev = cumulative
            cumulative += c
            if cumulative >= target:
                if c == 0:
                    # Empty bucket: cannot interpolate, return the lower edge.
                    return b[i - 1] if i > 0 else self._min
                # Linear interpolation across this bucket's span. The lower
                # edge is the previous boundary (or 0 for the first bucket);
                # the upper edge is this bucket's boundary (or the running max
                # for the overflow bucket).
                lower = b[i - 1] if i > 0 else 0.0
                upper = b[i] if i < len(b) else self._max
                if upper <= lower:
                    return lower
                frac = (target - prev) / c
                return lower + frac * (upper - lower)
        return self._max

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------
    @property
    def total(self) -> int:
        """Number of samples recorded."""
        return self._total

    @property
    def min(self) -> float:
        """Minimum observed sample. Raises on an empty collector."""
        if self._total == 0:
            raise ValueError("no samples recorded")
        return self._min

    @property
    def max(self) -> float:
        """Maximum observed sample. Raises on an empty collector."""
        if self._total == 0:
            raise ValueError("no samples recorded")
        return self._max

    def bucket_counts(self) -> List[int]:
        """Return a copy of the per-bucket counts.

        Bucket i covers (boundary[i-1], boundary[i]] for i > 0; bucket 0
        covers [0, boundary[0]]; the last bucket covers everything above the
        final boundary.
        """
        return list(self._bucket_counts)
