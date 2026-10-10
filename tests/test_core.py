import math
import unittest

from collector_go import HistogramCollector


class TestConstruction(unittest.TestCase):
    def test_default_boundaries_sorted(self):
        h = HistogramCollector([1, 5, 10, 50, 100, 500, 1000])
        self.assertEqual(h.total, 0)

    def test_unsorted_boundaries_rejected(self):
        with self.assertRaises(ValueError):
            HistogramCollector([10, 5, 1])

    def test_duplicate_boundaries_rejected(self):
        with self.assertRaises(ValueError):
            HistogramCollector([1, 5, 5, 10])

    def test_negative_boundaries_rejected(self):
        with self.assertRaises(ValueError):
            HistogramCollector([-1, 1, 5])

    def test_max_centroids_too_small(self):
        with self.assertRaises(ValueError):
            HistogramCollector([1, 2], max_centroids=1)


class TestRecording(unittest.TestCase):
    def test_record_single(self):
        h = HistogramCollector([10, 100])
        h.record(5)
        self.assertEqual(h.total, 1)
        self.assertEqual(h.min, 5)
        self.assertEqual(h.max, 5)

    def test_record_negative_rejected(self):
        h = HistogramCollector([10])
        with self.assertRaises(ValueError):
            h.record(-1)

    def test_record_nan_rejected(self):
        h = HistogramCollector([10])
        with self.assertRaises(ValueError):
            h.record(float("nan"))

    def test_record_many(self):
        h = HistogramCollector([10, 100])
        h.record_many([1, 2, 3, 50, 200])
        self.assertEqual(h.total, 5)
        self.assertEqual(h.min, 1)
        self.assertEqual(h.max, 200)

    def test_record_many_rejects_nan(self):
        h = HistogramCollector([10, 100])
        with self.assertRaises(ValueError):
            h.record_many([1, float("nan"), 3])


class TestPercentile(unittest.TestCase):
    def test_empty_percentile_raises(self):
        h = HistogramCollector([10, 100])
        with self.assertRaises(ValueError):
            h.percentile(50)

    def test_empty_min_raises(self):
        h = HistogramCollector([10, 100])
        with self.assertRaises(ValueError):
            _ = h.min

    def test_empty_max_raises(self):
        h = HistogramCollector([10, 100])
        with self.assertRaises(ValueError):
            _ = h.max

    def test_zero_is_min(self):
        h = HistogramCollector([10, 100])
        h.record_many([5, 50, 500])
        self.assertEqual(h.percentile(0), 5)

    def test_hundred_is_max(self):
        h = HistogramCollector([10, 100])
        h.record_many([5, 50, 500])
        self.assertEqual(h.percentile(100), 500)

    def test_out_of_range_raises(self):
        h = HistogramCollector([10, 100])
        h.record(5)
        with self.assertRaises(ValueError):
            h.percentile(-1)
        with self.assertRaises(ValueError):
            h.percentile(101)

    def test_small_uniform_stream(self):
        # 0..99 inclusive, p50 should land near the median of the set.
        h = HistogramCollector([10, 50, 100], max_centroids=500)
        h.record_many(list(range(100)))
        p50 = h.percentile(50)
        # With centroids we may return a mean, so allow a small band.
        self.assertLessEqual(p50, 55)
        self.assertGreaterEqual(p50, 45)

    def test_high_percentile_is_in_upper_half(self):
        h = HistogramCollector([10, 50, 100], max_centroids=500)
        h.record_many(list(range(100)))
        p99 = h.percentile(99)
        self.assertGreaterEqual(p99, 90)

    def test_single_sample_all_percentiles(self):
        h = HistogramCollector([10, 100])
        h.record(42)
        for p in (0, 25, 50, 75, 100):
            self.assertEqual(h.percentile(p), 42)


class TestCentroidMerging(unittest.TestCase):
    def test_centroid_budget_enforced(self):
        h = HistogramCollector([1000], max_centroids=10)
        for i in range(500):
            h.record(float(i))
        # Internal centroid store should not exceed budget after merging.
        self.assertLessEqual(len(h._means), 10)

    def test_percentile_still_reasonable_after_merge(self):
        h = HistogramCollector([1000], max_centroids=20)
        for i in range(500):
            h.record(float(i))
        p50 = h.percentile(50)
        p95 = h.percentile(95)
        # Even with aggressive merging, the ordering must hold.
        self.assertLessEqual(p50, p95)


class TestBucketCounts(unittest.TestCase):
    def test_buckets_populated(self):
        h = HistogramCollector([10, 100])
        h.record_many([1, 5, 50, 200])
        counts = h.bucket_counts()
        self.assertEqual(counts, [2, 1, 1])

    def test_bucket_counts_is_a_copy(self):
        h = HistogramCollector([10, 100])
        h.record(5)
        c1 = h.bucket_counts()
        c1[0] = 999
        c2 = h.bucket_counts()
        self.assertEqual(c2[0], 1)


if __name__ == "__main__":
    unittest.main()
