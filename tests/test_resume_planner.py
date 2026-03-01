"""Tests for resume projection and policy behavior."""

import qbittorrentapi

from diskguard.models import ResumePolicy
from diskguard.resume_planner import ResumePlanner
from tests.helpers import FakeQbClient, disk_stats, make_config, torrent


async def test_negative_or_zero_budget_means_no_resumes() -> None:
    """Tests that negative or zero budget means no resumes."""
    config = make_config(resume_floor_pct=10.0, safety_buffer_gb=0.0)
    qb = FakeQbClient()
    planner = ResumePlanner(config, qb)

    torrents = [
        torrent(
            "candidate", state="pausedDL", amount_left=10, tags=("diskguard_paused",)
        ),
        torrent("active", state="downloading", amount_left=100),
    ]
    stats = disk_stats(total_bytes=1000, free_bytes=100)

    summary = await planner.execute(torrents, stats, paused_hashes={"candidate"})
    assert summary.budget <= 0
    assert summary.resumed_hashes == ()
    assert qb.resume_calls == []


async def test_resume_floor_blocks_resumes_when_free_below_floor() -> None:
    """Tests that resume floor blocks resumes when free percent is below floor."""
    config = make_config(
        soft_pause_below_pct=10.0,
        resume_floor_pct=12.0,
        safety_buffer_gb=0.0,
    )
    qb = FakeQbClient()
    planner = ResumePlanner(config, qb)

    torrents = [
        torrent(
            "candidate", state="pausedDL", amount_left=5, tags=("diskguard_paused",)
        ),
    ]
    stats = disk_stats(total_bytes=100, free_bytes=11)

    summary = await planner.execute(torrents, stats, paused_hashes={"candidate"})

    assert summary.budget < 0
    assert summary.resumed_hashes == ()
    assert summary.decisions[0].reason == "does_not_fit"
    assert qb.resume_calls == []
    assert qb.remove_tag_calls == []


async def test_missing_active_amount_left_skips_resumes_for_safety() -> None:
    """Tests that missing active amount left skips resumes for safety."""
    config = make_config(resume_floor_pct=5.0, safety_buffer_gb=0.0)
    qb = FakeQbClient()
    planner = ResumePlanner(config, qb)

    torrents = [
        torrent(
            "candidate", state="pausedDL", amount_left=10, tags=("diskguard_paused",)
        ),
        torrent("active", state="downloading", amount_left=None),
    ]
    stats = disk_stats(total_bytes=1000, free_bytes=500)

    summary = await planner.execute(torrents, stats, paused_hashes={"candidate"})
    assert summary.active_remaining is None
    assert len(summary.decisions) == 1
    assert summary.decisions[0].reason == "active_remaining_unknown"
    assert qb.resume_calls == []


async def test_priority_fifo_strict_stops_at_first_non_fitting_candidate() -> None:
    """Tests that priority fifo strict stops at first non fitting candidate."""
    config = make_config(
        policy=ResumePolicy.PRIORITY_FIFO,
        strict_fifo=True,
        resume_floor_pct=0.0,
        safety_buffer_gb=0.0,
    )
    qb = FakeQbClient()
    planner = ResumePlanner(config, qb)

    torrents = [
        torrent(
            "first",
            state="pausedDL",
            amount_left=60,
            priority=10,
            added_on=1,
            tags=("diskguard_paused",),
        ),
        torrent(
            "second",
            state="pausedDL",
            amount_left=10,
            priority=5,
            added_on=2,
            tags=("diskguard_paused",),
        ),
    ]
    stats = disk_stats(total_bytes=100, free_bytes=50)

    summary = await planner.execute(torrents, stats, paused_hashes={"first", "second"})
    assert summary.resumed_hashes == ()
    assert qb.resume_calls == []
    assert summary.decisions[0].hash == "first"
    assert summary.decisions[0].reason == "does_not_fit"


async def test_priority_fifo_skip_mode_continues_after_non_fit() -> None:
    """Tests that priority fifo skip mode continues after non fit."""
    config = make_config(
        policy=ResumePolicy.PRIORITY_FIFO,
        strict_fifo=False,
        resume_floor_pct=0.0,
        safety_buffer_gb=0.0,
    )
    qb = FakeQbClient()
    planner = ResumePlanner(config, qb)

    torrents = [
        torrent(
            "first",
            state="pausedDL",
            amount_left=60,
            priority=10,
            added_on=1,
            tags=("diskguard_paused",),
        ),
        torrent(
            "second",
            state="pausedDL",
            amount_left=10,
            priority=5,
            added_on=2,
            tags=("diskguard_paused",),
        ),
    ]
    stats = disk_stats(total_bytes=100, free_bytes=50)

    summary = await planner.execute(torrents, stats, paused_hashes={"first", "second"})
    assert summary.resumed_hashes == ("second",)
    assert qb.resume_calls == ["second"]


async def test_batch_resume_failure_marks_all_planned_candidates_as_failed() -> None:
    """Tests that batch resume failure marks planned candidates as failed."""
    config = make_config(
        policy=ResumePolicy.PRIORITY_FIFO,
        strict_fifo=False,
        resume_floor_pct=0.0,
        safety_buffer_gb=0.0,
    )
    qb = FakeQbClient(fail_resume={"first"})
    planner = ResumePlanner(config, qb)

    torrents = [
        torrent(
            "first",
            state="pausedDL",
            amount_left=40,
            priority=10,
            added_on=1,
            tags=("diskguard_paused",),
        ),
        torrent(
            "second",
            state="pausedDL",
            amount_left=20,
            priority=5,
            added_on=2,
            tags=("diskguard_paused",),
        ),
    ]
    stats = disk_stats(total_bytes=100, free_bytes=70)

    summary = await planner.execute(torrents, stats, paused_hashes={"first", "second"})

    assert summary.resumed_hashes == ()
    assert summary.decisions[0].hash == "first"
    assert summary.decisions[0].reason == "resume_failed"
    assert summary.decisions[1].hash == "second"
    assert summary.decisions[1].reason == "resume_failed"
    assert qb.resume_request_payloads == [("first", "second")]


async def test_priority_fifo_strict_stops_after_first_non_fit_even_if_later_would_fit() -> (
    None
):
    """Tests strict FIFO stop behavior when a non-fitting candidate is encountered."""
    config = make_config(
        policy=ResumePolicy.PRIORITY_FIFO,
        strict_fifo=True,
        resume_floor_pct=0.0,
        safety_buffer_gb=0.0,
    )
    qb = FakeQbClient()
    planner = ResumePlanner(config, qb)

    torrents = [
        torrent(
            "fit",
            state="pausedDL",
            amount_left=10,
            priority=20,
            added_on=1,
            tags=("diskguard_paused",),
        ),
        torrent(
            "non_fit",
            state="pausedDL",
            amount_left=50,
            priority=10,
            added_on=2,
            tags=("diskguard_paused",),
        ),
        torrent(
            "later_fit",
            state="pausedDL",
            amount_left=5,
            priority=1,
            added_on=3,
            tags=("diskguard_paused",),
        ),
    ]
    stats = disk_stats(total_bytes=100, free_bytes=20)

    summary = await planner.execute(
        torrents, stats, paused_hashes={"fit", "non_fit", "later_fit"}
    )

    assert summary.resumed_hashes == ("fit",)
    assert [decision.hash for decision in summary.decisions] == ["fit", "non_fit"]
    assert summary.decisions[1].reason == "does_not_fit"
    assert "later_fit" not in qb.resume_calls


async def test_smallest_first_resumes_best_fit_order() -> None:
    """Tests that smallest first resumes best fit order."""
    config = make_config(
        policy=ResumePolicy.SMALLEST_FIRST, resume_floor_pct=0.0, safety_buffer_gb=0.0
    )
    qb = FakeQbClient()
    planner = ResumePlanner(config, qb)

    torrents = [
        torrent(
            "a",
            state="pausedDL",
            amount_left=15,
            tags=("diskguard_paused",),
            added_on=3,
        ),
        torrent(
            "b", state="pausedDL", amount_left=5, tags=("diskguard_paused",), added_on=2
        ),
        torrent(
            "c",
            state="pausedDL",
            amount_left=20,
            tags=("diskguard_paused",),
            added_on=1,
        ),
    ]
    stats = disk_stats(total_bytes=100, free_bytes=25)

    summary = await planner.execute(torrents, stats, paused_hashes={"a", "b", "c"})
    assert summary.resumed_hashes == ("b", "a")
    assert qb.resume_calls == ["b", "a"]


async def test_largest_first_chooses_largest_that_fits_first() -> None:
    """Tests that largest first chooses largest that fits first."""
    config = make_config(
        policy=ResumePolicy.LARGEST_FIRST, resume_floor_pct=0.0, safety_buffer_gb=0.0
    )
    qb = FakeQbClient()
    planner = ResumePlanner(config, qb)

    torrents = [
        torrent("small", state="pausedDL", amount_left=10, tags=("diskguard_paused",)),
        torrent("large", state="pausedDL", amount_left=30, tags=("diskguard_paused",)),
        torrent("medium", state="pausedDL", amount_left=20, tags=("diskguard_paused",)),
    ]
    stats = disk_stats(total_bytes=100, free_bytes=35)

    summary = await planner.execute(
        torrents, stats, paused_hashes={"small", "large", "medium"}
    )
    assert summary.resumed_hashes == ("large",)
    assert qb.resume_calls == ["large"]


async def test_zero_or_invalid_amount_left_candidates_are_skipped() -> None:
    """Tests that zero or invalid amount left candidates are skipped."""
    config = make_config(
        policy=ResumePolicy.PRIORITY_FIFO,
        strict_fifo=False,
        resume_floor_pct=0.0,
        safety_buffer_gb=0.0,
    )
    qb = FakeQbClient()
    planner = ResumePlanner(config, qb)

    torrents = [
        torrent("zero", state="pausedDL", amount_left=0, tags=("diskguard_paused",)),
        torrent("none", state="pausedDL", amount_left=None, tags=("diskguard_paused",)),
        torrent("good", state="pausedDL", amount_left=5, tags=("diskguard_paused",)),
    ]
    stats = disk_stats(total_bytes=100, free_bytes=10)

    summary = await planner.execute(
        torrents, stats, paused_hashes={"zero", "none", "good"}
    )
    assert summary.resumed_hashes == ("good",)
    assert qb.resume_calls == ["good"]


async def test_candidate_not_in_paused_download_state_is_not_resumed() -> None:
    """Tests that candidate not in paused download state is not resumed."""
    config = make_config(resume_floor_pct=0.0, safety_buffer_gb=0.0)
    qb = FakeQbClient()
    planner = ResumePlanner(config, qb)

    torrents = [
        torrent(
            "not-paused-download",
            state="pausedUP",
            amount_left=10,
            tags=("diskguard_paused",),
        ),
        torrent("good", state="pausedDL", amount_left=5, tags=("diskguard_paused",)),
    ]
    stats = disk_stats(total_bytes=100, free_bytes=20)

    summary = await planner.execute(
        torrents, stats, paused_hashes={"not-paused-download", "good"}
    )
    assert summary.resumed_hashes == ("good",)
    assert qb.resume_calls == ["good"]


async def test_resume_failure_keeps_torrent_unresumed() -> None:
    """Tests that resume failure keeps torrent unresumed."""
    config = make_config(resume_floor_pct=0.0, safety_buffer_gb=0.0)
    qb = FakeQbClient(fail_resume={"bad"})
    planner = ResumePlanner(config, qb)

    torrents = [
        torrent("bad", state="pausedDL", amount_left=5, tags=("diskguard_paused",))
    ]
    stats = disk_stats(total_bytes=100, free_bytes=20)

    summary = await planner.execute(torrents, stats, paused_hashes={"bad"})
    assert summary.resumed_hashes == ()
    assert summary.decisions[0].reason == "resume_failed"
    assert qb.resume_calls == ["bad"]
    assert qb.resume_request_payloads == [("bad",)]


async def test_remove_tag_failure_after_resume_still_counts_as_resumed() -> None:
    """Tests that remove tag failure after resume still counts as resumed."""
    config = make_config(resume_floor_pct=0.0, safety_buffer_gb=0.0)
    qb = FakeQbClient(fail_remove_tag={("ok", "diskguard_paused")})
    planner = ResumePlanner(config, qb)

    torrents = [
        torrent("ok", state="pausedDL", amount_left=5, tags=("diskguard_paused",))
    ]
    stats = disk_stats(total_bytes=100, free_bytes=20)

    summary = await planner.execute(torrents, stats, paused_hashes={"ok"})
    assert summary.resumed_hashes == ("ok",)
    assert qb.resume_calls == ["ok"]


async def test_resume_planner_batches_resume_and_paused_tag_removals() -> None:
    """Tests that multiple resumable candidates are executed with batch requests."""
    config = make_config(
        policy=ResumePolicy.PRIORITY_FIFO,
        strict_fifo=False,
        resume_floor_pct=0.0,
        safety_buffer_gb=0.0,
    )
    qb = FakeQbClient()
    planner = ResumePlanner(config, qb)

    torrents = [
        torrent("a", state="pausedDL", amount_left=10, tags=("diskguard_paused",)),
        torrent("b", state="pausedDL", amount_left=20, tags=("diskguard_paused",)),
    ]
    stats = disk_stats(total_bytes=100, free_bytes=50)

    summary = await planner.execute(torrents, stats, paused_hashes={"a", "b"})

    assert summary.resumed_hashes == ("a", "b")
    assert ("a", "b") in qb.resume_request_payloads
    assert (("a", "b"), ("diskguard_paused",)) in qb.remove_tag_request_payloads


class BatchResumeFailsQbClient(FakeQbClient):
    """Fake client that fails resume calls only for batch hash payloads."""

    def torrents_resume(self, *, torrent_hashes=None) -> None:  # type: ignore[override]
        """Fails batch resume calls."""
        if torrent_hashes is not None and not isinstance(torrent_hashes, str):
            requested_hashes = [
                str(part).strip() for part in torrent_hashes if str(part).strip()
            ]
            self.resume_request_payloads.append(tuple(requested_hashes))
            raise qbittorrentapi.APIConnectionError("batch resume failed")
        super().torrents_resume(torrent_hashes=torrent_hashes)


async def test_resume_planner_marks_batch_resume_failure_without_per_hash_retry() -> (
    None
):
    """Tests that batch resume failure does not trigger per-hash retries."""
    config = make_config(
        policy=ResumePolicy.PRIORITY_FIFO,
        strict_fifo=False,
        resume_floor_pct=0.0,
        safety_buffer_gb=0.0,
    )
    qb = BatchResumeFailsQbClient()
    planner = ResumePlanner(config, qb)

    torrents = [
        torrent("a", state="pausedDL", amount_left=10, tags=("diskguard_paused",)),
        torrent("b", state="pausedDL", amount_left=10, tags=("diskguard_paused",)),
    ]
    stats = disk_stats(total_bytes=100, free_bytes=50)

    summary = await planner.execute(torrents, stats, paused_hashes={"a", "b"})

    assert summary.resumed_hashes == ()
    assert [decision.reason for decision in summary.decisions] == [
        "resume_failed",
        "resume_failed",
    ]
    assert qb.resume_request_payloads == [("a", "b")]


async def test_execute_uses_paused_torrents_input_when_provided() -> None:
    """Tests that explicit paused_torrents limits candidate evaluation."""
    config = make_config(
        policy=ResumePolicy.PRIORITY_FIFO,
        strict_fifo=False,
        resume_floor_pct=0.0,
        safety_buffer_gb=0.0,
    )
    qb = FakeQbClient()
    planner = ResumePlanner(config, qb)

    first = torrent(
        "first", state="pausedDL", amount_left=10, tags=("diskguard_paused",)
    )
    second = torrent(
        "second", state="pausedDL", amount_left=10, tags=("diskguard_paused",)
    )
    torrents = [first, second]
    stats = disk_stats(total_bytes=100, free_bytes=30)

    summary = await planner.execute(
        torrents,
        stats,
        paused_hashes={"first", "second"},
        paused_torrents=[second],
    )

    assert summary.resumed_hashes == ("second",)
    assert [decision.hash for decision in summary.decisions] == ["second"]


class BatchRemovePausedTagFailsQbClient(FakeQbClient):
    """Fake client that fails tag removals only for batch hash payloads."""

    def torrents_remove_tags(self, *, tags=None, torrent_hashes=None) -> None:  # type: ignore[override]
        """Fails batch remove-tags to force per-torrent fallback path."""
        if torrent_hashes is not None and not isinstance(torrent_hashes, str):
            requested_hashes = [
                str(part).strip() for part in torrent_hashes if str(part).strip()
            ]
            if isinstance(tags, str):
                normalized_tags = [
                    part.strip() for part in tags.split(",") if part.strip()
                ]
            elif tags is None:
                normalized_tags = []
            else:
                normalized_tags = [
                    str(part).strip() for part in tags if str(part).strip()
                ]
            self.remove_tag_request_payloads.append(
                (tuple(requested_hashes), tuple(normalized_tags))
            )
            raise qbittorrentapi.APIConnectionError("batch remove tags failed")
        super().torrents_remove_tags(tags=tags, torrent_hashes=torrent_hashes)


async def test_resume_planner_keeps_resumed_when_batch_tag_remove_fails() -> None:
    """Tests that batch tag-remove failure does not change resumed outcomes."""
    config = make_config(
        policy=ResumePolicy.PRIORITY_FIFO,
        strict_fifo=False,
        resume_floor_pct=0.0,
        safety_buffer_gb=0.0,
    )
    qb = BatchRemovePausedTagFailsQbClient()
    planner = ResumePlanner(config, qb)

    torrents = [
        torrent("a", state="pausedDL", amount_left=10, tags=("diskguard_paused",)),
        torrent("b", state="pausedDL", amount_left=10, tags=("diskguard_paused",)),
    ]
    stats = disk_stats(total_bytes=100, free_bytes=50)

    summary = await planner.execute(torrents, stats, paused_hashes={"a", "b"})

    assert summary.resumed_hashes == ("a", "b")
    assert qb.remove_tag_request_payloads == [(("a", "b"), ("diskguard_paused",))]


async def test_resume_planner_dedupes_resume_and_tag_batch_payloads() -> None:
    """Tests that duplicate hashes are de-duplicated before API calls."""
    config = make_config(
        policy=ResumePolicy.PRIORITY_FIFO,
        strict_fifo=False,
        resume_floor_pct=0.0,
        safety_buffer_gb=0.0,
    )
    qb = FakeQbClient()
    planner = ResumePlanner(config, qb)

    resumed_hashes = await planner._resume_hashes(["a", "a", "b", "b"])  # noqa: SLF001
    await planner._remove_paused_tag_from_hashes(  # noqa: SLF001
        ["a", "a", "b", "b"]
    )

    assert resumed_hashes == ["a", "b"]
    assert qb.resume_request_payloads == [("a", "b")]
    assert qb.remove_tag_request_payloads == [(("a", "b"), ("diskguard_paused",))]
