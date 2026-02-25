"""Tests for pure state/classification helpers."""

from diskguard.models import ResumePolicy
from diskguard.state import (
    classify_mode,
    is_active_downloader_for_projection,
    is_downloading_ish_state,
    is_forced_download_state,
    is_paused_download_state,
    parse_tags,
    sort_resume_candidates,
)
from tests.helpers import torrent


def test_parse_tags_trims_and_ignores_empty_values() -> None:
    """Tests that parse tags trims and ignores empty values."""
    assert parse_tags("foo, bar, ,baz,,") == frozenset({"foo", "bar", "baz"})
    assert parse_tags("") == frozenset()
    assert parse_tags(None) == frozenset()


def test_mode_classification_boundaries() -> None:
    """Tests that mode classification boundaries."""
    assert classify_mode(10.0, soft_pause_below_pct=10.0, hard_pause_below_pct=5.0).value == "NORMAL"
    assert classify_mode(9.99, soft_pause_below_pct=10.0, hard_pause_below_pct=5.0).value == "SOFT"
    assert classify_mode(5.0, soft_pause_below_pct=10.0, hard_pause_below_pct=5.0).value == "SOFT"
    assert classify_mode(4.99, soft_pause_below_pct=10.0, hard_pause_below_pct=5.0).value == "HARD"


def test_state_classifiers() -> None:
    """Tests that state classifiers."""
    downloading_states = ("downloading", "metaDL", "queuedDL")
    assert is_downloading_ish_state("downloading", downloading_states)
    assert is_downloading_ish_state("metaDL", downloading_states)
    assert not is_downloading_ish_state("pausedDL", downloading_states)
    assert is_forced_download_state("forcedDL")
    assert not is_forced_download_state("downloading")
    assert is_paused_download_state("pausedDL")
    assert is_paused_download_state("stoppedDL")
    assert not is_paused_download_state("pausedUP")


def test_active_downloader_projection_filter() -> None:
    """Tests that active downloader projection filter."""
    downloading_states = ("downloading", "metaDL")
    assert is_active_downloader_for_projection(
        torrent("a", state="downloading", amount_left=10),
        paused_tag="diskguard_paused",
        downloading_states=downloading_states,
    )
    assert not is_active_downloader_for_projection(
        torrent("b", state="downloading", amount_left=10, tags=("diskguard_paused",)),
        paused_tag="diskguard_paused",
        downloading_states=downloading_states,
    )
    assert not is_active_downloader_for_projection(
        torrent("c", state="forcedDL", amount_left=10),
        paused_tag="diskguard_paused",
        downloading_states=downloading_states,
    )


def test_sort_resume_candidates_for_each_policy() -> None:
    """Tests that sort resume candidates for each policy."""
    items = [
        torrent("a", state="pausedDL", amount_left=30, priority=1, added_on=300, tags=("diskguard_paused",)),
        torrent("b", state="pausedDL", amount_left=10, priority=2, added_on=200, tags=("diskguard_paused",)),
        torrent("c", state="pausedDL", amount_left=20, priority=2, added_on=100, tags=("diskguard_paused",)),
    ]

    smallest = sort_resume_candidates(items, ResumePolicy.SMALLEST_FIRST)
    assert [item.hash for item in smallest] == ["b", "c", "a"]

    largest = sort_resume_candidates(items, ResumePolicy.LARGEST_FIRST)
    assert [item.hash for item in largest] == ["a", "c", "b"]

    fifo = sort_resume_candidates(items, ResumePolicy.PRIORITY_FIFO)
    assert [item.hash for item in fifo] == ["c", "b", "a"]
