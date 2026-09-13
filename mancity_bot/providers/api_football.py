"""API-Football (api-sports / RapidAPI) adapter.

This is the provider that carries per-player match statistics, lineups,
timeline events and team match stats.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any

from ..config import current_season
from ..models import (
    Fixture,
    MatchEvent,
    MatchReport,
    PlayerMatchStats,
    SquadPlayer,
    TableRow,
)
from .base import PlanLimitError, Provider, ProviderError

log = logging.getLogger(__name__)

#: API-Football league ids worth surfacing by name
LEAGUES = {
    39: "Premier League",
    2: "UEFA Champions League",
    45: "FA Cup",
    48: "EFL Cup",
    528: "Community Shield",
    15: "FIFA Club World Cup",
}


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _num(value: Any, default: int = 0) -> int:
    return int(value) if isinstance(value, (int, float)) else default


def _pct(value: Any) -> int | None:
    """Normalise "87%", 87 or "87" to 87."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip().rstrip("%")
    try:
        return int(float(text))
    except ValueError:
        return None


def _name_tokens(name: str) -> set[str]:
    noise = {"fc", "afc", "cf", "united", "city", "the", "club"}
    cleaned = "".join(c if c.isalnum() else " " for c in name.lower())
    return {tok for tok in cleaned.split() if len(tok) > 2 and tok not in noise}


class ApiFootballProvider(Provider):
    name = "api-football"
    min_interval = 1.2

    def __init__(self, key: str, host: str, team_id: int, **kwargs: Any) -> None:
        self.host = host.strip()
        # The RapidAPI gateway namespaces the API under /v3; the direct
        # api-sports host serves the same endpoints at the root.
        self.base_url = (
            f"https://{self.host}/v3" if "rapidapi" in self.host else f"https://{self.host}"
        )
        super().__init__(**kwargs)
        self._key = key
        self.team_id = team_id

    def _headers(self) -> dict[str, str]:
        if "rapidapi" in self.host:
            return {"x-rapidapi-key": self._key, "x-rapidapi-host": self.host}
        return {"x-apisports-key": self._key}

    async def _payload(self, path: str, params: dict[str, Any]) -> list[Any]:
        data = await self._get(path, params=params)
        errors = data.get("errors")
        # API-Football answers 200 with a populated `errors` object on plan
        # or quota problems, so a bare status check is not enough.
        if errors and not isinstance(errors, list):
            if "plan" in errors:
                raise PlanLimitError(f"api-football: {errors['plan']}")
            raise ProviderError(f"api-football: {errors}")
        return data.get("response", []) or []

    async def account(self) -> dict[str, Any]:
        """Subscription tier and today's request count, from /status."""
        data = await self._get("/status", params={})
        return data.get("response") or {}

    # ------------------------------------------------------------------ fixtures

    async def fixtures(self, season: int) -> list[Fixture]:
        rows = await self._payload("/fixtures", {"team": self.team_id, "season": season})
        out: list[Fixture] = []
        for row in rows:
            try:
                out.append(self._to_fixture(row))
            except (KeyError, TypeError, ValueError) as exc:
                log.warning("skipping api-football fixture: %s", exc)
        out.sort(key=lambda f: f.kickoff)
        return out

    async def fixtures_on(self, day: date) -> list[Fixture]:
        """Fixtures for the tracked team on a single date.

        API-Football rejects a date filter without an explicit season, so the
        season is derived from the date itself.
        """
        rows = await self._payload(
            "/fixtures",
            {"team": self.team_id, "date": day.isoformat(), "season": current_season(day)},
        )
        return [self._to_fixture(r) for r in rows]

    def _to_fixture(self, row: dict[str, Any]) -> Fixture:
        fx = row["fixture"]
        teams = row["teams"]
        goals = row.get("goals") or {}
        league = row.get("league") or {}
        we_are_home = teams["home"]["id"] == self.team_id
        opponent = teams["away" if we_are_home else "home"]["name"]

        return Fixture(
            key=f"af:{fx['id']}",
            provider=self.name,
            competition=league.get("name", "Unknown"),
            competition_code=str(league.get("id", "")),
            opponent=opponent,
            home=we_are_home,
            kickoff=_parse_utc(fx["date"]),
            status=(fx.get("status") or {}).get("short", "NS"),
            goals_for=goals.get("home") if we_are_home else goals.get("away"),
            goals_against=goals.get("away") if we_are_home else goals.get("home"),
            venue=(fx.get("venue") or {}).get("name"),
            matchday=league.get("round"),
            api_football_id=fx["id"],
            raw=row,
        )

    async def find_fixture_id(self, fixture: Fixture) -> int | None:
        """Map a fixture from another provider onto an API-Football fixture id.

        Matches on kickoff date plus a loose opponent-name overlap, which is
        enough in practice because a club plays at most one match per day.
        """
        if fixture.api_football_id:
            return fixture.api_football_id
        try:
            candidates = await self.fixtures_on(fixture.kickoff.date())
        except PlanLimitError:
            raise  # let the service switch the provider off
        except ProviderError as exc:
            log.warning("could not resolve fixture id for %s: %s", fixture.key, exc)
            return None
        if len(candidates) == 1:
            return candidates[0].api_football_id
        wanted = _name_tokens(fixture.opponent)
        for candidate in candidates:
            if wanted & _name_tokens(candidate.opponent):
                return candidate.api_football_id
        return None

    # -------------------------------------------------------------------- report

    async def match_report(self, fixture: Fixture) -> MatchReport:
        """Player stats, events and team stats for a finished match."""
        report = MatchReport(fixture=fixture)
        fixture_id = await self.find_fixture_id(fixture)
        if fixture_id is None:
            log.info("no api-football id for %s, returning bare report", fixture.key)
            return report
        fixture.api_football_id = fixture_id

        report.players = await self._players(fixture_id)
        report.events = await self._events(fixture_id)
        await self._team_stats(fixture_id, report)
        await self._formation(fixture_id, report)
        return report

    async def _players(self, fixture_id: int) -> list[PlayerMatchStats]:
        rows = await self._payload("/fixtures/players", {"fixture": fixture_id})
        for block in rows:
            if (block.get("team") or {}).get("id") != self.team_id:
                continue
            out = []
            for item in block.get("players", []):
                stats = (item.get("statistics") or [{}])[0]
                games = stats.get("games") or {}
                shots = stats.get("shots") or {}
                goals = stats.get("goals") or {}
                passes = stats.get("passes") or {}
                tackles = stats.get("tackles") or {}
                duels = stats.get("duels") or {}
                cards = stats.get("cards") or {}
                rating = games.get("rating")
                out.append(
                    PlayerMatchStats(
                        name=(item.get("player") or {}).get("name", "Unknown"),
                        position=games.get("position"),
                        number=games.get("number"),
                        minutes=games.get("minutes"),
                        rating=float(rating) if rating else None,
                        goals=_num(goals.get("total")),
                        assists=_num(goals.get("assists")),
                        shots=_num(shots.get("total")),
                        shots_on=_num(shots.get("on")),
                        passes=_num(passes.get("total")),
                        pass_accuracy=_pct(passes.get("accuracy")),
                        key_passes=_num(passes.get("key")),
                        tackles=_num(tackles.get("total")),
                        duels_won=_num(duels.get("won")),
                        duels_total=_num(duels.get("total")),
                        saves=_num(goals.get("saves")),
                        yellow=_num(cards.get("yellow")),
                        red=_num(cards.get("red")),
                        captain=bool(games.get("captain")),
                        substitute=bool(games.get("substitute")),
                    )
                )
            return out
        return []

    async def _events(self, fixture_id: int) -> list[MatchEvent]:
        rows = await self._payload("/fixtures/events", {"fixture": fixture_id})
        out = []
        for row in rows:
            team = row.get("team") or {}
            block = row.get("time") or {}
            minute, extra = block.get("elapsed"), block.get("extra")
            out.append(
                MatchEvent(
                    minute=(minute + extra) if minute and extra else minute,
                    kind=row.get("type", ""),
                    detail=row.get("detail", ""),
                    player=(row.get("player") or {}).get("name"),
                    assist=(row.get("assist") or {}).get("name"),
                    team=team.get("name", ""),
                    ours=team.get("id") == self.team_id,
                )
            )
        return out

    async def _team_stats(self, fixture_id: int, report: MatchReport) -> None:
        try:
            rows = await self._payload("/fixtures/statistics", {"fixture": fixture_id})
        except ProviderError as exc:
            log.info("no team stats for %s: %s", fixture_id, exc)
            return
        for block in rows:
            if (block.get("team") or {}).get("id") != self.team_id:
                continue
            for stat in block.get("statistics", []):
                label, value = stat.get("type"), stat.get("value")
                if label == "Ball Possession":
                    report.possession = _pct(value)
                elif label == "Total Shots":
                    report.shots = _num(value) or None
                elif label == "Shots on Goal":
                    report.shots_on = _num(value) or None
                elif label == "Corner Kicks":
                    report.corners = _num(value) or None
                elif label == "Fouls":
                    report.fouls = _num(value) or None
                elif label == "expected_goals" and value is not None:
                    try:
                        report.expected_goals = float(value)
                    except (TypeError, ValueError):
                        pass

    async def _formation(self, fixture_id: int, report: MatchReport) -> None:
        try:
            rows = await self._payload("/fixtures/lineups", {"fixture": fixture_id})
        except ProviderError:
            return
        for block in rows:
            if (block.get("team") or {}).get("id") == self.team_id:
                report.formation = block.get("formation")
                return

    # --------------------------------------------------------------------- squad

    async def squad(self) -> list[SquadPlayer]:
        rows = await self._payload("/players/squads", {"team": self.team_id})
        out = []
        for block in rows:
            for person in block.get("players", []):
                out.append(
                    SquadPlayer(
                        name=person.get("name", "Unknown"),
                        position=person.get("position"),
                        number=person.get("number"),
                        age=person.get("age"),
                    )
                )
        return out

    async def player_season(self, name: str, season: int) -> dict[str, Any] | None:
        """Season aggregate for one squad player, searched by name."""
        rows = await self._payload(
            "/players", {"team": self.team_id, "season": season, "search": name[:20]}
        )
        return rows[0] if rows else None

    async def standings(self, league_id: int, season: int) -> list[TableRow]:
        rows = await self._payload("/standings", {"league": league_id, "season": season})
        groups = (rows[0].get("league") or {}).get("standings") if rows else None
        if not groups:
            raise ProviderError("api-football: empty standings")
        out = []
        for entry in groups[0]:
            team = entry.get("team") or {}
            played = entry.get("all") or {}
            out.append(
                TableRow(
                    position=entry.get("rank", 0),
                    team=team.get("name", "?"),
                    played=played.get("played", 0),
                    won=played.get("win", 0),
                    drawn=played.get("draw", 0),
                    lost=played.get("lose", 0),
                    goal_difference=entry.get("goalsDiff", 0),
                    points=entry.get("points", 0),
                    is_tracked=team.get("id") == self.team_id,
                )
            )
        return out
