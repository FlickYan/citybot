"""football-data.org v4 adapter: fixtures, results, standings, squad."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from ..models import Fixture, SquadPlayer, TableRow
from .base import Provider, ProviderError

log = logging.getLogger(__name__)


def _parse_utc(value: str) -> datetime:
    # football-data returns e.g. "2026-09-13T15:30:00Z"
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


class FootballDataProvider(Provider):
    name = "football-data"
    base_url = "https://api.football-data.org/v4"
    min_interval = 6.5  # free tier: 10 requests / minute

    def __init__(self, token: str, team_id: int, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._token = token
        self.team_id = team_id

    def _headers(self) -> dict[str, str]:
        return {"X-Auth-Token": self._token}

    async def fixtures(self, days_back: int = 21, days_ahead: int = 120) -> list[Fixture]:
        """All matches for the tracked team in a window, across every competition."""
        today = datetime.now(timezone.utc).date()
        data = await self._get(
            f"/teams/{self.team_id}/matches",
            params={
                "dateFrom": (today - timedelta(days=days_back)).isoformat(),
                "dateTo": (today + timedelta(days=days_ahead)).isoformat(),
            },
        )
        out: list[Fixture] = []
        for match in data.get("matches", []):
            try:
                out.append(self._to_fixture(match))
            except (KeyError, TypeError, ValueError) as exc:
                log.warning("skipping unparseable match %s: %s", match.get("id"), exc)
        out.sort(key=lambda f: f.kickoff)
        return out

    def _to_fixture(self, match: dict[str, Any]) -> Fixture:
        home_team = match["homeTeam"]
        away_team = match["awayTeam"]
        we_are_home = home_team.get("id") == self.team_id
        opponent_raw = away_team if we_are_home else home_team
        opponent = (
            opponent_raw.get("shortName")
            or opponent_raw.get("name")
            or "Unknown"
        )

        full_time = (match.get("score") or {}).get("fullTime") or {}
        home_goals, away_goals = full_time.get("home"), full_time.get("away")
        goals_for = home_goals if we_are_home else away_goals
        goals_against = away_goals if we_are_home else home_goals

        competition = match.get("competition") or {}
        matchday = match.get("matchday")
        return Fixture(
            key=f"fd:{match['id']}",
            provider=self.name,
            competition=competition.get("name", "Unknown"),
            competition_code=competition.get("code", ""),
            opponent=opponent,
            home=we_are_home,
            kickoff=_parse_utc(match["utcDate"]),
            status=match.get("status", "SCHEDULED"),
            goals_for=goals_for,
            goals_against=goals_against,
            matchday=f"Matchday {matchday}" if matchday else None,
            stage=(match.get("stage") or "").replace("_", " ").title() or None,
            raw=match,
        )

    async def squad(self) -> list[SquadPlayer]:
        data = await self._get(f"/teams/{self.team_id}")
        out = []
        for person in data.get("squad", []):
            out.append(
                SquadPlayer(
                    name=person.get("name", "Unknown"),
                    position=person.get("position"),
                    nationality=person.get("nationality"),
                    age=_age_from(person.get("dateOfBirth")),
                )
            )
        return out

    async def standings(self, competition: str = "PL") -> list[TableRow]:
        data = await self._get(f"/competitions/{competition}/standings")
        groups = data.get("standings") or []
        total = next((g for g in groups if g.get("type") == "TOTAL"), None)
        if total is None:
            raise ProviderError(f"no TOTAL standings for {competition}")
        rows = []
        for entry in total.get("table", []):
            team = entry.get("team") or {}
            rows.append(
                TableRow(
                    position=entry.get("position", 0),
                    team=team.get("shortName") or team.get("name") or "?",
                    played=entry.get("playedGames", 0),
                    won=entry.get("won", 0),
                    drawn=entry.get("draw", 0),
                    lost=entry.get("lost", 0),
                    goal_difference=entry.get("goalDifference", 0),
                    points=entry.get("points", 0),
                    is_tracked=team.get("id") == self.team_id,
                )
            )
        return rows


def _age_from(date_of_birth: str | None) -> int | None:
    if not date_of_birth:
        return None
    try:
        born = datetime.fromisoformat(date_of_birth).date()
    except ValueError:
        return None
    today = datetime.now(timezone.utc).date()
    return today.year - born.year - ((today.month, today.day) < (born.month, born.day))
