"""Aggregation layer.

The rest of the app talks only to `FootballService`. It decides which adapter
answers a question, caches responses to protect the free-tier quotas, and
degrades gracefully when one provider is missing or down.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Awaitable, Callable, TypeVar

from ..config import Config, current_season
from ..models import Fixture, MatchReport, SquadPlayer, TableRow
from .api_football import LEAGUES, ApiFootballProvider
from .base import PlanLimitError, ProviderError
from .football_data import FootballDataProvider

log = logging.getLogger(__name__)

T = TypeVar("T")

#: football-data competition code -> API-Football league id
COMPETITION_ALIASES = {
    "PL": 39,
    "CL": 2,
    "FA": 45,
    "EL": 3,
}


class _Cache:
    """Tiny in-process TTL cache. Quota, not latency, is the reason it exists."""

    def __init__(self) -> None:
        self._store: dict[str, tuple[float, Any]] = {}

    async def get_or_set(
        self, key: str, ttl: float, producer: Callable[[], Awaitable[T]]
    ) -> T:
        hit = self._store.get(key)
        if hit and time.monotonic() - hit[0] < ttl:
            return hit[1]
        value = await producer()
        self._store[key] = (time.monotonic(), value)
        return value

    def invalidate(self, prefix: str = "") -> None:
        for key in [k for k in self._store if k.startswith(prefix)]:
            del self._store[key]


class FootballService:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.cache = _Cache()
        self.football_data: FootballDataProvider | None = None
        self.api_football: ApiFootballProvider | None = None
        #: Set once API-Football reports the plan cannot serve what we ask for.
        #: Further calls are skipped rather than spending the daily quota on
        #: requests that are guaranteed to fail.
        self.api_football_limit: str | None = None

        if cfg.has_football_data:
            self.football_data = FootballDataProvider(
                cfg.football_data_token, cfg.football_data_team_id
            )
        if cfg.has_api_football:
            self.api_football = ApiFootballProvider(
                cfg.api_football_key, cfg.api_football_host, cfg.api_football_team_id
            )

    @property
    def player_stats_available(self) -> bool:
        return self.api_football is not None and self.api_football_limit is None

    async def _try_api_football(self, call: Callable[[], Awaitable[T]], what: str) -> T | None:
        """Run an API-Football call, disabling the provider on a plan refusal."""
        if not self.player_stats_available:
            return None
        try:
            return await call()
        except PlanLimitError as exc:
            self.api_football_limit = str(exc).replace("api-football: ", "")
            log.warning(
                "API-Football plan does not cover %s (%s); "
                "player-level data disabled for this run",
                what,
                self.api_football_limit,
            )
        except ProviderError as exc:
            log.warning("api-football %s failed: %s", what, exc)
        return None

    async def aclose(self) -> None:
        for provider in (self.football_data, self.api_football):
            if provider:
                await provider.aclose()

    # ------------------------------------------------------------------ fixtures

    async def fixtures(self, force: bool = False) -> list[Fixture]:
        """Every known fixture in a rolling window, newest provider data first.

        football-data.org is preferred for the schedule because its free tier
        covers more competitions per request; API-Football is the fallback.
        """
        if force:
            self.cache.invalidate("fixtures")

        async def produce() -> list[Fixture]:
            if self.football_data:
                try:
                    rows = await self.football_data.fixtures()
                    if rows:
                        return rows
                except ProviderError as exc:
                    log.warning("football-data fixtures failed: %s", exc)
            rows = await self._try_api_football(
                lambda: self.api_football.fixtures(current_season()), "fixtures"
            )
            return rows or []

        ttl = self.cfg.fixture_refresh_minutes * 60
        return await self.cache.get_or_set("fixtures", ttl, produce)

    async def next_fixtures(self, limit: int = 5) -> list[Fixture]:
        rows = await self.fixtures()
        return [f for f in rows if not f.finished][:limit]

    async def recent_fixtures(self, limit: int = 5) -> list[Fixture]:
        rows = await self.fixtures()
        return [f for f in rows if f.finished][-limit:][::-1]

    async def fixture_by_key(self, key: str) -> Fixture | None:
        return next((f for f in await self.fixtures() if f.key == key), None)

    # -------------------------------------------------------------------- report

    async def match_report(self, fixture: Fixture) -> MatchReport:
        """Post-match detail. Falls back to a scoreline-only report."""
        report = await self._try_api_football(
            lambda: self.api_football.match_report(fixture), f"report for {fixture.key}"
        )
        return report or MatchReport(fixture=fixture)

    # --------------------------------------------------------------------- squad

    async def squad(self) -> list[SquadPlayer]:
        async def produce() -> list[SquadPlayer]:
            if self.football_data:
                try:
                    rows = await self.football_data.squad()
                    if rows:
                        return rows
                except ProviderError as exc:
                    log.warning("football-data squad failed: %s", exc)
            rows = await self._try_api_football(lambda: self.api_football.squad(), "squad")
            return rows or []

        return await self.cache.get_or_set("squad", 24 * 3600, produce)

    async def player_season(self, name: str) -> dict[str, Any] | None:
        if not self.player_stats_available:
            return None

        async def produce() -> dict[str, Any] | None:
            return await self._try_api_football(
                lambda: self.api_football.player_season(name, current_season()),
                f"player {name}",
            )

        return await self.cache.get_or_set(f"player:{name.lower()}", 6 * 3600, produce)

    # ----------------------------------------------------------------- standings

    async def standings(self, competition: str = "PL") -> list[TableRow]:
        competition = competition.upper()
        key = f"table:{competition}"

        async def produce() -> list[TableRow]:
            if self.football_data:
                try:
                    return await self.football_data.standings(competition)
                except ProviderError as exc:
                    log.warning("football-data standings failed: %s", exc)
            league_id = COMPETITION_ALIASES.get(competition)
            if league_id:
                rows = await self._try_api_football(
                    lambda: self.api_football.standings(league_id, current_season()),
                    f"standings {competition}",
                )
                if rows:
                    return rows
            return []

        return await self.cache.get_or_set(key, 3600, produce)

    # ------------------------------------------------------------------- health

    def status_lines(self) -> list[str]:
        lines = [
            f"football-data.org: {'connected' if self.football_data else 'not configured'}"
        ]
        if not self.api_football:
            lines += [
                "API-Football: not configured",
                "Player-level match stats need API_FOOTBALL_KEY.",
            ]
        elif self.api_football_limit:
            lines += [
                "API-Football: plan limit reached",
                f"  {self.api_football_limit}",
                "Player ratings and goal timelines are unavailable on this plan.",
            ]
        else:
            lines.append("API-Football: connected")
        return lines


__all__ = ["FootballService", "COMPETITION_ALIASES", "LEAGUES"]
