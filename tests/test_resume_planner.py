"""Tests for resume projection and policy behavior."""

from diskguard.models import ResumePolicy
from diskguard.resume_planner import ResumePlanner
from tests.helpers import FakeQbClient, disk_stats, make_config, torrent


async def test_negative_or_zero_budget_means_no_resumes() -> None:
    """Tests that negative or zero budget means no resumes."""
    config = make_config(resume_floor_pct=10.0, safety_buffer_gb=0.0)
    qb = FakeQbClient()
    planner = ResumePlanner(config, qb)

    torrents = [
        torrent("candidate", state="pausedDL", amount_left=10, tags=("diskguard_paused",)),
        torrent("active", state="downloading", amount_left=100),
    ]
    stats = disk_stats(total_bytes=1000, free_bytes=100)

    summary = await planner.execute(torrents, stats)
    assert summary.budget <= 0
    assert summary.resumed_hashes == ()
    assert qb.resume_calls == []


async def test_missing_active_amount_left_skips_resumes_for_safety() -> None:
    """Tests that missing active amount left skips resumes for safety."""
    config = make_config(resume_floor_pct=5.0, safety_buffer_gb=0.0)
    qb = FakeQbClient()
    planner = ResumePlanner(config, qb)

    torrents = [
        torrent("candidate", state="pausedDL", amount_left=10, tags=("diskguard_paused",)),
        torrent("active", state="downloading", amount_left=None),
    ]
    stats = disk_stats(total_bytes=1000, free_bytes=500)

    summary = await planner.execute(torrents, stats)
    assert summary.active_remaining is None
    assert qb.resume_calls == []


async def test_priority_fifo_strict_stops_at_first_non_fitting_candidate() -> None:
    """Tests that priority fifo strict stops at first non fitting candidate."""
    config = make_config(policy=ResumePolicy.PRIORITY_FIFO, strict_fifo=True, resume_floor_pct=0.0, safety_buffer_gb=0.0)
    qb = FakeQbClient()
    planner = ResumePlanner(config, qb)

    torrents = [
        torrent("first", state="pausedDL", amount_left=60, priority=10, added_on=1, tags=("diskguard_paused",)),
        torrent("second", state="pausedDL", amount_left=10, priority=5, added_on=2, tags=("diskguard_paused",)),
    ]
    stats = disk_stats(total_bytes=100, free_bytes=50)

    summary = await planner.execute(torrents, stats)
    assert summary.resumed_hashes == ()
    assert qb.resume_calls == []
    assert summary.decisions[0].hash == "first"
    assert summary.decisions[0].reason == "does_not_fit"


async def test_priority_fifo_skip_mode_continues_after_non_fit() -> None:
    """Tests that priority fifo skip mode continues after non fit."""
    config = make_config(policy=ResumePolicy.PRIORITY_FIFO, strict_fifo=False, resume_floor_pct=0.0, safety_buffer_gb=0.0)
    qb = FakeQbClient()
    planner = ResumePlanner(config, qb)

    torrents = [
        torrent("first", state="pausedDL", amount_left=60, priority=10, added_on=1, tags=("diskguard_paused",)),
        torrent("second", state="pausedDL", amount_left=10, priority=5, added_on=2, tags=("diskguard_paused",)),
    ]
    stats = disk_stats(total_bytes=100, free_bytes=50)

    summary = await planner.execute(torrents, stats)
    assert summary.resumed_hashes == ("second",)
    assert qb.resume_calls == ["second"]


async def test_smallest_first_resumes_best_fit_order() -> None:
    """Tests that smallest first resumes best fit order."""
    config = make_config(policy=ResumePolicy.SMALLEST_FIRST, resume_floor_pct=0.0, safety_buffer_gb=0.0)
    qb = FakeQbClient()
    planner = ResumePlanner(config, qb)

    torrents = [
        torrent("a", state="pausedDL", amount_left=15, tags=("diskguard_paused",), added_on=3),
        torrent("b", state="pausedDL", amount_left=5, tags=("diskguard_paused",), added_on=2),
        torrent("c", state="pausedDL", amount_left=20, tags=("diskguard_paused",), added_on=1),
    ]
    stats = disk_stats(total_bytes=100, free_bytes=25)

    summary = await planner.execute(torrents, stats)
    assert summary.resumed_hashes == ("b", "a")
    assert qb.resume_calls == ["b", "a"]


async def test_largest_first_chooses_largest_that_fits_first() -> None:
    """Tests that largest first chooses largest that fits first."""
    config = make_config(policy=ResumePolicy.LARGEST_FIRST, resume_floor_pct=0.0, safety_buffer_gb=0.0)
    qb = FakeQbClient()
    planner = ResumePlanner(config, qb)

    torrents = [
        torrent("small", state="pausedDL", amount_left=10, tags=("diskguard_paused",)),
        torrent("large", state="pausedDL", amount_left=30, tags=("diskguard_paused",)),
        torrent("medium", state="pausedDL", amount_left=20, tags=("diskguard_paused",)),
    ]
    stats = disk_stats(total_bytes=100, free_bytes=35)

    summary = await planner.execute(torrents, stats)
    assert summary.resumed_hashes == ("large",)
    assert qb.resume_calls == ["large"]


async def test_zero_or_invalid_amount_left_candidates_are_skipped() -> None:
    """Tests that zero or invalid amount left candidates are skipped."""
    config = make_config(policy=ResumePolicy.PRIORITY_FIFO, strict_fifo=False, resume_floor_pct=0.0, safety_buffer_gb=0.0)
    qb = FakeQbClient()
    planner = ResumePlanner(config, qb)

    torrents = [
        torrent("zero", state="pausedDL", amount_left=0, tags=("diskguard_paused",)),
        torrent("none", state="pausedDL", amount_left=None, tags=("diskguard_paused",)),
        torrent("good", state="pausedDL", amount_left=5, tags=("diskguard_paused",)),
    ]
    stats = disk_stats(total_bytes=100, free_bytes=10)

    summary = await planner.execute(torrents, stats)
    assert summary.resumed_hashes == ("good",)
    assert qb.resume_calls == ["good"]


async def test_candidate_not_in_paused_download_state_is_not_resumed() -> None:
    """Tests that candidate not in paused download state is not resumed."""
    config = make_config(resume_floor_pct=0.0, safety_buffer_gb=0.0)
    qb = FakeQbClient()
    planner = ResumePlanner(config, qb)

    torrents = [
        torrent("not-paused-download", state="pausedUP", amount_left=10, tags=("diskguard_paused",)),
        torrent("good", state="pausedDL", amount_left=5, tags=("diskguard_paused",)),
    ]
    stats = disk_stats(total_bytes=100, free_bytes=20)

    summary = await planner.execute(torrents, stats)
    assert summary.resumed_hashes == ("good",)
    assert qb.resume_calls == ["good"]


async def test_resume_failure_keeps_torrent_unresumed() -> None:
    """Tests that resume failure keeps torrent unresumed."""
    config = make_config(resume_floor_pct=0.0, safety_buffer_gb=0.0)
    qb = FakeQbClient(fail_resume={"bad"})
    planner = ResumePlanner(config, qb)

    torrents = [torrent("bad", state="pausedDL", amount_left=5, tags=("diskguard_paused",))]
    stats = disk_stats(total_bytes=100, free_bytes=20)

    summary = await planner.execute(torrents, stats)
    assert summary.resumed_hashes == ()
    assert summary.decisions[0].reason == "resume_failed"
    assert qb.resume_calls == ["bad"]


async def test_remove_tag_failure_after_resume_still_counts_as_resumed() -> None:
    """Tests that remove tag failure after resume still counts as resumed."""
    config = make_config(resume_floor_pct=0.0, safety_buffer_gb=0.0)
    qb = FakeQbClient(fail_remove_tag={("ok", "diskguard_paused")})
    planner = ResumePlanner(config, qb)

    torrents = [torrent("ok", state="pausedDL", amount_left=5, tags=("diskguard_paused",))]
    stats = disk_stats(total_bytes=100, free_bytes=20)

    summary = await planner.execute(torrents, stats)
    assert summary.resumed_hashes == ("ok",)
    assert qb.resume_calls == ["ok"]
