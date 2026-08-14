import pytest

from packages.capacity_testing.stats import SampleStats


def test_stats_are_bounded_and_report_percentiles() -> None:
    stats = SampleStats(max_samples=3)
    for value in (1.0, 2.0, 3.0, 4.0, 5.0):
        stats.observe(value)

    assert stats.count == 5
    assert len(stats.values) == 3
    assert stats.percentile(50) is not None
    assert stats.as_dict()["sampled_values"] == 3


def test_stats_reject_invalid_sample() -> None:
    stats = SampleStats()
    with pytest.raises(ValueError, match="finite"):
        stats.observe(float("inf"))
