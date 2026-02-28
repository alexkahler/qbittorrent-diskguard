"""Domain models used by DiskGuard."""

from dataclasses import dataclass
from enum import Enum


class Mode(str, Enum):
    """DiskGuard operating modes."""

    NORMAL = "NORMAL"
    SOFT = "SOFT"
    HARD = "HARD"


class ResumePolicy(str, Enum):
    """Supported resume ordering policies."""

    SMALLEST_FIRST = "smallest_first"
    PRIORITY_FIFO = "priority_fifo"
    LARGEST_FIRST = "largest_first"


@dataclass(frozen=True)
class DiskStats:
    """Disk measurements for a single polling tick."""

    total_bytes: int
    free_bytes: int
    free_pct: float


@dataclass(frozen=True)
class ResumeDecision:
    """Decision trace for one candidate considered by the resume planner."""

    hash: str
    amount_left: int | None
    fits: bool
    resumed: bool
    reason: str


@dataclass(frozen=True)
class ResumeSummary:
    """Summary output from a resume planning tick."""

    budget: int
    active_remaining: int | None
    resumed_hashes: tuple[str, ...]
    decisions: tuple[ResumeDecision, ...]
