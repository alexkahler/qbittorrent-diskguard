"""Integration-style tests for mode enforcement behavior."""

import asyncio
import logging

from diskguard.engine import ModeEngine
from diskguard.errors import QbittorrentUnavailableError
from diskguard.resume_planner import ResumePlanner
from tests.helpers import FakeDiskProbe, FakeQbClient, disk_stats, make_config, missing_path_error, torrent


async def test_normal_to_soft_transition_adds_soft_allowed_without_pausing_existing_downloaders() -> None:
    """Tests that normal to soft transition adds soft allowed without pausing existing downloaders."""
    config = make_config()
    qb = FakeQbClient(
        torrents_sequence=[
            [torrent("a", state="downloading", amount_left=200)],
            [torrent("a", state="downloading", amount_left=200)],
        ]
    )
    probe = FakeDiskProbe(
        stats_sequence=[
            disk_stats(total_bytes=1_000, free_bytes=200),  # NORMAL (20%)
            disk_stats(total_bytes=1_000, free_bytes=90),  # SOFT (9%)
        ]
    )
    planner = ResumePlanner(config, qb)
    engine = ModeEngine(config, qb_client=qb, disk_probe=probe, resume_planner=planner)

    await engine.tick()
    await engine.tick()

    assert ("a", "soft_allowed") in qb.add_tag_calls
    assert "a" not in qb.pause_calls


async def test_normal_to_soft_transition_tags_unknown_size_downloaders_as_soft_allowed() -> None:
    """Tests that normal to soft transition tags unknown size downloaders as soft allowed."""
    config = make_config()
    qb = FakeQbClient(
        torrents_sequence=[
            [],
            [torrent("meta", state="metaDL", amount_left=0)],
        ]
    )
    probe = FakeDiskProbe(
        stats_sequence=[
            disk_stats(total_bytes=1_000, free_bytes=200),  # NORMAL (20%)
            disk_stats(total_bytes=1_000, free_bytes=90),  # SOFT (9%)
        ]
    )
    planner = ResumePlanner(config, qb)
    engine = ModeEngine(config, qb_client=qb, disk_probe=probe, resume_planner=planner)

    await engine.tick()
    await engine.tick()

    assert ("meta", "soft_allowed") in qb.add_tag_calls
    assert "meta" not in qb.pause_calls


async def test_soft_mode_steady_enforcement_pauses_new_downloaders() -> None:
    """Tests that soft mode steady enforcement pauses new downloaders."""
    config = make_config()
    qb = FakeQbClient(
        torrents_sequence=[
            [],
            [torrent("old", state="downloading", amount_left=50)],
            [
                torrent("old", state="downloading", amount_left=40, tags=("soft_allowed",)),
                torrent("new", state="downloading", amount_left=10),
            ],
        ]
    )
    probe = FakeDiskProbe(
        stats_sequence=[
            disk_stats(total_bytes=1_000, free_bytes=300),  # NORMAL
            disk_stats(total_bytes=1_000, free_bytes=90),  # SOFT transition
            disk_stats(total_bytes=1_000, free_bytes=85),  # SOFT steady
        ]
    )
    planner = ResumePlanner(config, qb)
    engine = ModeEngine(config, qb_client=qb, disk_probe=probe, resume_planner=planner)

    await engine.tick()
    await engine.tick()
    await engine.tick()

    assert "new" in qb.pause_calls
    assert ("new", "diskguard_paused") in qb.add_tag_calls
    assert "old" not in qb.pause_calls


async def test_soft_mode_ignores_unknown_size_until_size_is_known() -> None:
    """Tests that soft mode ignores unknown size until size is known."""
    config = make_config()
    qb = FakeQbClient(
        torrents_sequence=[
            [torrent("new", state="downloading", amount_left=0)],
            [torrent("new", state="downloading", amount_left=25)],
        ]
    )
    probe = FakeDiskProbe(
        stats_sequence=[
            disk_stats(total_bytes=1_000, free_bytes=90),  # SOFT
            disk_stats(total_bytes=1_000, free_bytes=90),  # SOFT
        ]
    )
    planner = ResumePlanner(config, qb)
    engine = ModeEngine(config, qb_client=qb, disk_probe=probe, resume_planner=planner)

    await engine.tick()
    assert qb.pause_calls == []

    await engine.tick()
    assert qb.pause_calls == ["new"]
    assert qb.add_tag_calls == [("new", "diskguard_paused")]


async def test_hard_mode_pauses_all_downloading_non_forced_and_cleans_soft_tags() -> None:
    """Tests that hard mode pauses all downloading non forced and cleans soft tags."""
    config = make_config()
    qb = FakeQbClient(
        torrents_sequence=[
            [torrent("base", state="downloading", amount_left=50, tags=("soft_allowed",))],  # SOFT baseline
            [
                torrent("base", state="downloading", amount_left=40, tags=("soft_allowed",)),
                torrent("x", state="downloading", amount_left=30),
                torrent("forced", state="forcedDL", amount_left=30),
            ],
        ]
    )
    probe = FakeDiskProbe(
        stats_sequence=[
            disk_stats(total_bytes=1_000, free_bytes=90),  # SOFT
            disk_stats(total_bytes=1_000, free_bytes=40),  # HARD
        ]
    )
    planner = ResumePlanner(config, qb)
    engine = ModeEngine(config, qb_client=qb, disk_probe=probe, resume_planner=planner)

    await engine.tick()
    await engine.tick()

    assert ("base", "soft_allowed") in qb.remove_tag_calls
    assert "base" in qb.pause_calls
    assert "x" in qb.pause_calls
    assert "forced" not in qb.pause_calls
    assert ("base", "diskguard_paused") in qb.add_tag_calls
    assert ("x", "diskguard_paused") in qb.add_tag_calls


async def test_soft_mode_removes_paused_tag_from_forced_download() -> None:
    """Tests that soft mode removes paused tag from forced download."""
    config = make_config()
    qb = FakeQbClient(
        torrents_sequence=[
            [torrent("forced", state="forcedDL", amount_left=10, tags=("diskguard_paused",))]
        ]
    )
    probe = FakeDiskProbe(stats_sequence=[disk_stats(total_bytes=1_000, free_bytes=90)])  # SOFT
    planner = ResumePlanner(config, qb)
    engine = ModeEngine(config, qb_client=qb, disk_probe=probe, resume_planner=planner)

    await engine.tick()

    assert ("forced", "diskguard_paused") in qb.remove_tag_calls
    assert "forced" not in qb.pause_calls


async def test_hard_mode_removes_paused_tag_from_forced_download() -> None:
    """Tests that hard mode removes paused tag from forced download."""
    config = make_config()
    qb = FakeQbClient(
        torrents_sequence=[
            [torrent("forced", state="forcedDL", amount_left=10, tags=("diskguard_paused",))]
        ]
    )
    probe = FakeDiskProbe(stats_sequence=[disk_stats(total_bytes=1_000, free_bytes=40)])  # HARD
    planner = ResumePlanner(config, qb)
    engine = ModeEngine(config, qb_client=qb, disk_probe=probe, resume_planner=planner)

    await engine.tick()

    assert ("forced", "diskguard_paused") in qb.remove_tag_calls
    assert "forced" not in qb.pause_calls


async def test_hard_mode_ignores_unknown_size_until_size_is_known() -> None:
    """Tests that hard mode ignores unknown size until size is known."""
    config = make_config()
    qb = FakeQbClient(
        torrents_sequence=[
            [torrent("x", state="downloading", amount_left=0)],
            [torrent("x", state="downloading", amount_left=10)],
        ]
    )
    probe = FakeDiskProbe(
        stats_sequence=[
            disk_stats(total_bytes=1_000, free_bytes=40),  # HARD
            disk_stats(total_bytes=1_000, free_bytes=40),  # HARD
        ]
    )
    planner = ResumePlanner(config, qb)
    engine = ModeEngine(config, qb_client=qb, disk_probe=probe, resume_planner=planner)

    await engine.tick()
    assert qb.pause_calls == []

    await engine.tick()
    assert qb.pause_calls == ["x"]
    assert qb.add_tag_calls == [("x", "diskguard_paused")]


async def test_forced_download_paused_tag_cleanup_failure_does_not_abort_tick(
    caplog,
) -> None:
    """Tests that forcedDL paused-tag cleanup failures do not abort the tick."""
    caplog.set_level(logging.WARNING)
    config = make_config()
    qb = FakeQbClient(
        torrents_sequence=[
            [
                torrent("forced", state="forcedDL", amount_left=10, tags=("diskguard_paused",)),
                torrent("down", state="downloading", amount_left=20),
            ]
        ],
        fail_remove_tag={("forced", "diskguard_paused")},
    )
    probe = FakeDiskProbe(stats_sequence=[disk_stats(total_bytes=1_000, free_bytes=40)])  # HARD
    planner = ResumePlanner(config, qb)
    engine = ModeEngine(config, qb_client=qb, disk_probe=probe, resume_planner=planner)

    await engine.tick()

    assert ("forced", "diskguard_paused") in qb.remove_tag_calls
    assert "forced" not in qb.pause_calls
    assert "down" in qb.pause_calls
    assert any(
        record.levelno == logging.WARNING
        and "Failed to remove tag diskguard_paused from torrent forced" in record.getMessage()
        for record in caplog.records
    )


async def test_normal_mode_self_heals_drifted_paused_tag_and_resumes_candidates() -> None:
    """Tests that normal mode self heals drifted paused tag and resumes candidates."""
    config = make_config(resume_floor_pct=0.0, safety_buffer_gb=0.0)
    qb = FakeQbClient(
        torrents_sequence=[
            [
                torrent("drifted", state="downloading", amount_left=30, tags=("diskguard_paused",)),
                torrent("candidate", state="pausedDL", amount_left=10, tags=("diskguard_paused",)),
            ]
        ]
    )
    probe = FakeDiskProbe(stats_sequence=[disk_stats(total_bytes=1_000, free_bytes=700)])
    planner = ResumePlanner(config, qb)
    engine = ModeEngine(config, qb_client=qb, disk_probe=probe, resume_planner=planner)

    await engine.tick()

    assert ("drifted", "diskguard_paused") in qb.remove_tag_calls
    assert "candidate" in qb.resume_calls


async def test_normal_mode_cleans_soft_allowed_tags() -> None:
    """Tests that normal mode cleans soft allowed tags."""
    config = make_config(resume_floor_pct=0.0, safety_buffer_gb=0.0)
    qb = FakeQbClient(
        torrents_sequence=[
            [
                torrent("soft", state="pausedDL", amount_left=20, tags=("soft_allowed",)),
            ]
        ]
    )
    probe = FakeDiskProbe(stats_sequence=[disk_stats(total_bytes=1_000, free_bytes=700)])
    planner = ResumePlanner(config, qb)
    engine = ModeEngine(config, qb_client=qb, disk_probe=probe, resume_planner=planner)

    await engine.tick()

    assert ("soft", "soft_allowed") in qb.remove_tag_calls


async def test_soft_allowed_removed_when_torrent_is_seeding_completed(
    caplog,
) -> None:
    """Tests that soft allowed removed when torrent is seeding completed."""
    caplog.set_level(logging.INFO)
    config = make_config()
    qb = FakeQbClient(
        torrents_sequence=[
            [torrent("seed", state="uploading", amount_left=0, tags=("soft_allowed",))]
        ]
    )
    probe = FakeDiskProbe(stats_sequence=[disk_stats(total_bytes=1_000, free_bytes=90)])  # SOFT
    planner = ResumePlanner(config, qb)
    logger = logging.getLogger("diskguard.engine.test")
    engine = ModeEngine(config, qb_client=qb, disk_probe=probe, resume_planner=planner, logger=logger)

    await engine.tick()

    assert ("seed", "soft_allowed") in qb.remove_tag_calls
    assert "seed" not in qb.pause_calls
    assert any(
        record.levelno == logging.INFO
        and record.getMessage() == "Removed soft_allowed from seed (now seeding/completed)"
        for record in caplog.records
    )


async def test_soft_allowed_not_removed_while_downloading() -> None:
    """Tests that soft allowed not removed while downloading."""
    config = make_config()
    qb = FakeQbClient(
        torrents_sequence=[
            [torrent("down", state="downloading", amount_left=100, tags=("soft_allowed",))]
        ]
    )
    probe = FakeDiskProbe(stats_sequence=[disk_stats(total_bytes=1_000, free_bytes=90)])  # SOFT
    planner = ResumePlanner(config, qb)
    engine = ModeEngine(config, qb_client=qb, disk_probe=probe, resume_planner=planner)

    await engine.tick()

    assert ("down", "soft_allowed") not in qb.remove_tag_calls


async def test_soft_allowed_seeding_cleanup_is_idempotent_without_duplicate_logs(
    caplog,
) -> None:
    """Tests that soft allowed seeding cleanup is idempotent without duplicate logs."""
    caplog.set_level(logging.INFO)
    config = make_config()
    qb = FakeQbClient(
        torrents_sequence=[
            [torrent("seed", state="uploading", amount_left=0, tags=("soft_allowed",))],
            [torrent("seed", state="uploading", amount_left=0, tags=())],
        ]
    )
    probe = FakeDiskProbe(
        stats_sequence=[
            disk_stats(total_bytes=1_000, free_bytes=90),  # SOFT
            disk_stats(total_bytes=1_000, free_bytes=90),  # SOFT
        ]
    )
    planner = ResumePlanner(config, qb)
    logger = logging.getLogger("diskguard.engine.test")
    engine = ModeEngine(config, qb_client=qb, disk_probe=probe, resume_planner=planner, logger=logger)

    await engine.tick()
    await engine.tick()

    assert qb.remove_tag_calls == [("seed", "soft_allowed")]
    cleanup_logs = [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.INFO
        and record.getMessage() == "Removed soft_allowed from seed (now seeding/completed)"
    ]
    assert len(cleanup_logs) == 1


async def test_missing_watch_path_is_safe_noop_for_tick() -> None:
    """Tests that missing watch path is safe noop for tick."""
    config = make_config()
    qb = FakeQbClient(torrents_sequence=[[torrent("x", state="downloading", amount_left=10)]])
    probe = FakeDiskProbe(stats_sequence=None, error=missing_path_error())
    planner = ResumePlanner(config, qb)
    engine = ModeEngine(config, qb_client=qb, disk_probe=probe, resume_planner=planner)

    await engine.tick()

    assert qb.fetch_calls == 0
    assert qb.pause_calls == []
    assert qb.resume_calls == []


async def test_qbittorrent_unreachable_skips_tick_without_actions() -> None:
    """Tests that qbittorrent unreachable skips tick without actions."""
    config = make_config()
    qb = FakeQbClient(fetch_error=QbittorrentUnavailableError("offline"))
    probe = FakeDiskProbe(stats_sequence=[disk_stats(total_bytes=1_000, free_bytes=90)])
    planner = ResumePlanner(config, qb)
    engine = ModeEngine(config, qb_client=qb, disk_probe=probe, resume_planner=planner)

    await engine.tick()

    assert qb.fetch_calls == 1
    assert qb.pause_calls == []
    assert qb.add_tag_calls == []


async def test_run_forever_recovers_from_tick_exception_and_stops() -> None:
    """Tests that run forever recovers from tick exception and stops."""
    config = make_config(interval_seconds=0)
    qb = FakeQbClient()
    probe = FakeDiskProbe(stats_sequence=[disk_stats(total_bytes=1_000, free_bytes=500)])
    planner = ResumePlanner(config, qb)
    engine = ModeEngine(config, qb_client=qb, disk_probe=probe, resume_planner=planner)
    stop_event = asyncio.Event()
    calls: list[int] = []

    async def fake_tick() -> None:
        """Fake tick."""
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        stop_event.set()

    engine.tick = fake_tick  # type: ignore[method-assign]
    await engine.run_forever(stop_event)
    assert len(calls) == 2


async def test_soft_transition_skips_forced_non_downloading_and_existing_soft_allowed() -> None:
    """Tests that soft transition skips forced non downloading and existing soft allowed."""
    config = make_config()
    qb = FakeQbClient(
        torrents_sequence=[
            [],
            [
                torrent("pausedtag", state="downloading", amount_left=10, tags=("diskguard_paused",)),
                torrent("forced", state="forcedDL", amount_left=10),
                torrent("paused", state="pausedDL", amount_left=10),
                torrent("already", state="downloading", amount_left=10, tags=("soft_allowed",)),
                torrent("valid", state="downloading", amount_left=10),
            ],
        ]
    )
    probe = FakeDiskProbe(
        stats_sequence=[
            disk_stats(total_bytes=1_000, free_bytes=500),  # NORMAL
            disk_stats(total_bytes=1_000, free_bytes=90),  # SOFT
        ]
    )
    planner = ResumePlanner(config, qb)
    engine = ModeEngine(config, qb_client=qb, disk_probe=probe, resume_planner=planner)

    await engine.tick()
    await engine.tick()

    assert ("valid", "soft_allowed") in qb.add_tag_calls
    assert "forced" not in qb.pause_calls
    assert "paused" not in qb.pause_calls
    assert "already" not in qb.pause_calls
    assert "pausedtag" in qb.pause_calls


async def test_hard_mode_ignores_non_downloading_states() -> None:
    """Tests that hard mode ignores non downloading states."""
    config = make_config()
    qb = FakeQbClient(
        torrents_sequence=[
            [],
            [
                torrent("non_download", state="pausedDL", amount_left=10),
                torrent("download", state="downloading", amount_left=20),
            ],
        ]
    )
    probe = FakeDiskProbe(
        stats_sequence=[
            disk_stats(total_bytes=1_000, free_bytes=90),  # SOFT baseline
            disk_stats(total_bytes=1_000, free_bytes=40),  # HARD
        ]
    )
    planner = ResumePlanner(config, qb)
    engine = ModeEngine(config, qb_client=qb, disk_probe=probe, resume_planner=planner)

    await engine.tick()
    await engine.tick()

    assert "download" in qb.pause_calls
    assert "non_download" not in qb.pause_calls


async def test_pause_and_mark_stops_when_pause_fails() -> None:
    """Tests that pause and mark stops when pause fails."""
    config = make_config()
    qb = FakeQbClient(fail_pause={"x"})
    probe = FakeDiskProbe(stats_sequence=[disk_stats(total_bytes=1_000, free_bytes=500)])
    planner = ResumePlanner(config, qb)
    engine = ModeEngine(config, qb_client=qb, disk_probe=probe, resume_planner=planner)

    await engine._pause_and_mark("x")

    assert qb.pause_calls == ["x"]
    assert qb.add_tag_calls == []


async def test_pause_and_mark_continues_when_add_tag_fails() -> None:
    """Tests that pause and mark continues when add tag fails."""
    config = make_config()
    qb = FakeQbClient(fail_add_tag={("x", "diskguard_paused")})
    probe = FakeDiskProbe(stats_sequence=[disk_stats(total_bytes=1_000, free_bytes=500)])
    planner = ResumePlanner(config, qb)
    engine = ModeEngine(config, qb_client=qb, disk_probe=probe, resume_planner=planner)

    await engine._pause_and_mark("x")

    assert qb.pause_calls == ["x"]
    assert qb.add_tag_calls == [("x", "diskguard_paused")]


async def test_add_and_remove_tag_helpers_return_false_on_failure() -> None:
    """Tests that add and remove tag helpers return false on failure."""
    config = make_config()
    qb = FakeQbClient(
        fail_add_tag={("x", "soft_allowed")},
        fail_remove_tag={("x", "soft_allowed")},
    )
    probe = FakeDiskProbe(stats_sequence=[disk_stats(total_bytes=1_000, free_bytes=500)])
    planner = ResumePlanner(config, qb)
    engine = ModeEngine(config, qb_client=qb, disk_probe=probe, resume_planner=planner)

    add_result = await engine._add_tag("x", "soft_allowed")
    remove_result = await engine._remove_tag("x", "soft_allowed", reason="test")

    assert add_result is False
    assert remove_result is False
