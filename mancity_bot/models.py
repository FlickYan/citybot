"""Provider-neutral domain objects.

Every adapter converts its own JSON into these, so the bot, the scheduler and
the formatter never see a provider-specific payload.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

FINISHED = {"FINISHED", "AWARDED", "FT", "AET", "PEN"}
LIVE = {"IN_PLAY", "PAUSED", "1H", "2H", "HT", "ET", "BT", "P", "LIVE"}


@dataclass
class Fixture:
    """One match involving the tracked club, seen from that club's point of view."""

    key: str  # stable id, e.g. "fd:498112" or "af:1035048"
    provider: str
    competition: str
    competition_code: str
    opponent: str
    home: bool
    kickoff: datetime  # always timezone-aware UTC
    status: str  # raw provider status
    goals_for: int | None = None
    goals_against: int | None = None
    venue: str | None = None
    matchday: str | None = None
    stage: str | None = None
    api_football_id: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def finished(self) -> bool:
        return self.status.upper() in FINISHED

    @property
    def live(self) -> bool:
        return self.status.upper() in LIVE

    @property
    def upcoming(self) -> bool:
        return not self.finished and not self.live and self.kickoff > _now()

    @property
    def outcome(self) -> str | None:
        """'W', 'D', 'L' or None if not decided."""
        if not self.finished or self.goals_for is None or self.goals_against is None:
            return None
        if self.goals_for > self.goals_against:
            return "W"
        if self.goals_for < self.goals_against:
            return "L"
        return "D"

    @property
    def scoreline(self) -> str | None:
        if self.goals_for is None or self.goals_against is None:
            return None
        return f"{self.goals_for}-{self.goals_against}"


@dataclass
class PlayerMatchStats:
    """Per-player numbers for a single match."""

    name: str
    position: str | None = None
    number: int | None = None
    minutes: int | None = None
    rating: float | None = None
    goals: int = 0
    assists: int = 0
    shots: int = 0
    shots_on: int = 0
    passes: int = 0
    pass_accuracy: int | None = None
    key_passes: int = 0
    tackles: int = 0
    duels_won: int = 0
    duels_total: int = 0
    saves: int = 0
    yellow: int = 0
    red: int = 0
    captain: bool = False
    substitute: bool = False

    @property
    def played(self) -> bool:
        return bool(self.minutes)

    @property
    def contributions(self) -> int:
        return self.goals + self.assists


@dataclass
class MatchEvent:
    minute: int | None
    kind: str  # Goal, Card, subst, Var
    detail: str
    player: str | None
    assist: str | None
    team: str
    ours: bool


@dataclass
class MatchReport:
    """Everything needed for the post-match digest."""

    fixture: Fixture
    players: list[PlayerMatchStats] = field(default_factory=list)
    events: list[MatchEvent] = field(default_factory=list)
    formation: str | None = None
    possession: int | None = None
    shots: int | None = None
    shots_on: int | None = None
    expected_goals: float | None = None
    corners: int | None = None
    fouls: int | None = None


@dataclass
class SquadPlayer:
    name: str
    position: str | None = None
    number: int | None = None
    nationality: str | None = None
    age: int | None = None


@dataclass
class TableRow:
    position: int
    team: str
    played: int
    won: int
    drawn: int
    lost: int
    goal_difference: int
    points: int
    is_tracked: bool = False


def _now() -> datetime:
    return datetime.now(timezone.utc)
