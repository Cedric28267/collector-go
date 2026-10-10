# collector-go

`collector-go` maintains a fixed-bucket latency histogram and computes approximate percentiles like p95 and p99 from a stream of samples using t-digest-style centroid merging. It keeps memory bounded regardless of how many samples you throw at it.

## Usage

```python
from collector_go import HistogramCollector

h = HistogramCollector([10, 50, 100, 250, 500, 1000, 2500, 5000], max_centroids=1000)
for sample in (12.0, 45.0, 120.0, 320.0, 900.0, 1100.0, 4800.0):
    h.record(sample)

print(h.percentile(50))   # approximate median
print(h.percentile(95))   # approximate p95
print(h.percentile(99))   # approximate p99
print(h.bucket_counts())  # [0, 2, 1, 1, 1, 1, 0, 0, 1]
print(h.total)            # 7
```

## Why this exists

Latency monitoring needs percentiles, not means, because tails are where operational problems live. Keeping every sample is fine for a few thousand requests but does not scale; pre-aggregating into a histogram with a handful of buckets is cheap but too coarse to read p99 off accurately. This library sits between those two options: it keeps a fixed bucket grid for fast O(buckets) counts, plus a centroid store with a hard ceiling (`max_centroids`) that preserves tail shape by merging the closest adjacent centroids when the budget is exceeded, exactly as a t-digest does during compaction.

The trade-off: the percentile values are approximations. A p95 returned here is within a few percent of the true value for typical latency distributions, but it is not exact. If you need exact percentiles, keep the raw samples somewhere else.

## Edge cases you will hit

- **Empty collector.** Calling `percentile`, `min`, or `max` before any sample is recorded raises `ValueError`. Returning a fake zero would hide missing-data bugs.
- **Percentile 0 and 100.** These are defined as the observed minimum and maximum respectively — not interpolated. This is a deliberate choice so that boundary queries always return real samples.
- **NaN samples.** Rejected on `record` with `ValueError`. A single NaN would poison every aggregate.
- **Boundaries.** Must be strictly increasing and non-negative. The constructor validates this rather than silently sorting, because an unsorted input is almost always a caller bug.
- **Merge pressure.** With a small `max_centroids` and many distinct values, the centroid store compacts aggressively. The returned percentiles stay self-consistent (p50 ≤ p95 ≤ p99) but individual values drift from the exact answer. If you need tighter accuracy at the tail, raise `max_centroids`.
