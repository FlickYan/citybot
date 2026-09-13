"""Offline smoke test.

Exercises fixture parsing, the SQLite layer and every message formatter with
synthetic payloads, so it needs no API keys and makes no network calls. It also
prints a sample of each message, which is the quickest way to see what the bot
will actually send.

    python tests/smoke_test.py
"""
import os, pathlib, sys, tempfile
from datetime import datetime, timedelta, timezone

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "0:test")
os.environ.setdefault("FOOTBALL_DATA_TOKEN", "x")
os.environ.setdefault("API_FOOTBALL_KEY", "y")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from mancity_bot.config import load_config, current_season
from mancity_bot.db import Database
from mancity_bot import formatting as fmt
from mancity_bot.models import Fixture, MatchReport, PlayerMatchStats, MatchEvent, SquadPlayer, TableRow
from mancity_bot.providers.football_data import FootballDataProvider
from mancity_bot.providers.api_football import ApiFootballProvider
from mancity_bot.providers import FootballService
from mancity_bot.bot import build_application  # import-time check only

cfg = load_config()
assert current_season(datetime(2026, 9, 9).date()) == 2026
assert current_season(datetime(2026, 3, 9).date()) == 2025
print("config + season OK ->", cfg.team_name, cfg.default_lead_times)

# ---- football-data parsing
fd = FootballDataProvider("tok", 65)
match = {
    "id": 498112, "utcDate": "2026-09-13T15:30:00Z", "status": "FINISHED",
    "matchday": 4, "stage": "REGULAR_SEASON",
    "competition": {"name": "Premier League", "code": "PL"},
    "homeTeam": {"id": 65, "name": "Manchester City FC", "shortName": "Man City"},
    "awayTeam": {"id": 57, "name": "Arsenal FC", "shortName": "Arsenal"},
    "score": {"fullTime": {"home": 3, "away": 1}},
}
f = fd._to_fixture(match)
assert f.home and f.opponent == "Arsenal" and f.goals_for == 3 and f.goals_against == 1
assert f.outcome == "W" and f.finished and f.scoreline == "3-1"
print("football-data parse OK ->", f.key, f.competition, f.scoreline, f.outcome)

# away + unfinished
away = dict(match, id=1, status="TIMED",
            homeTeam={"id": 57, "shortName": "Arsenal"},
            awayTeam={"id": 65, "shortName": "Man City"},
            score={"fullTime": {"home": None, "away": None}})
fa = fd._to_fixture(away)
assert not fa.home and fa.opponent == "Arsenal" and fa.outcome is None
print("away/unfinished parse OK")

# ---- api-football parsing
af = ApiFootballProvider("k", "v3.football.api-sports.io", 50)
assert af.base_url == "https://v3.football.api-sports.io"
assert "x-apisports-key" in af._headers()
af_rapid = ApiFootballProvider("k", "api-football-v1.p.rapidapi.com", 50)
assert af_rapid.base_url == "https://api-football-v1.p.rapidapi.com/v3"
assert "x-rapidapi-key" in af_rapid._headers()
row = {
    "fixture": {"id": 1035048, "date": "2026-09-13T15:30:00+00:00",
                "status": {"short": "FT"}, "venue": {"name": "Etihad Stadium"}},
    "league": {"id": 39, "name": "Premier League", "round": "Regular Season - 4"},
    "teams": {"home": {"id": 50, "name": "Manchester City"}, "away": {"id": 42, "name": "Arsenal"}},
    "goals": {"home": 3, "away": 1},
}
f2 = af._to_fixture(row)
assert f2.api_football_id == 1035048 and f2.venue == "Etihad Stadium" and f2.outcome == "W"
print("api-football parse OK ->", f2.key, f2.matchday, f2.venue)

from mancity_bot.providers.api_football import _pct, _num, _name_tokens
assert _pct("87%") == 87 and _pct(87) == 87 and _pct(None) is None and _pct("-") is None
assert _num(None) == 0 and _num("3") == 0 and _num(3) == 3
assert _name_tokens("Arsenal FC") == {"arsenal"}
assert _name_tokens("Manchester United") == {"manchester"}
print("coercion helpers OK")

# ---- DB
tmp = os.path.join(tempfile.mkdtemp(), "t.sqlite3")
db = Database(tmp)
assert db.subscribe(111, "Europe/London", [1440, 60, 15]) is True
assert db.subscribe(111, "Europe/London", [60]) is False
s = db.get(111)
assert s.lead_times == [1440, 60, 15] and s.prematch and s.postmatch and s.weekly
db.set_timezone(111, "Asia/Seoul"); db.set_lead_times(111, [90]); db.set_flag(111, "weekly", False)
s = db.get(111)
assert s.tz == "Asia/Seoul" and s.lead_times == [90] and not s.weekly
assert not db.was_sent(111, "pre:fd:1:60")
db.mark_sent(111, "pre:fd:1:60"); db.mark_sent(111, "pre:fd:1:60")
assert db.was_sent(111, "pre:fd:1:60")
assert len(db.all()) == 1
assert db.unsubscribe(111) and not db.unsubscribe(111)
try:
    db.set_flag(1, "bogus; DROP TABLE", True); raise AssertionError("should reject")
except ValueError:
    pass
db.close()
print("database OK")

# ---- formatting
now = datetime.now(timezone.utc)
soon = Fixture(key="fd:2", provider="fd", competition="UEFA Champions League",
               competition_code="CL", opponent="Real Madrid", home=False,
               kickoff=now + timedelta(hours=26), status="TIMED",
               venue="Santiago Bernabéu", stage="League Stage")
print("\n--- prematch ---")
print(fmt.format_prematch(soon, "Asia/Seoul", "Manchester City", 1440))

report = MatchReport(
    fixture=f,
    formation="4-2-3-1", possession=64, shots=18, shots_on=8, expected_goals=2.71,
    players=[
        PlayerMatchStats("Erling Haaland", "F", 9, 90, 8.4, goals=2, shots=5, shots_on=3, passes=21, pass_accuracy=81, duels_won=6, duels_total=11),
        PlayerMatchStats("Phil Foden", "M", 47, 78, 7.9, assists=2, key_passes=4, passes=63, pass_accuracy=91),
        PlayerMatchStats("Rodri", "M", 16, 90, 7.6, passes=98, pass_accuracy=95, tackles=4, yellow=1),
        PlayerMatchStats("Ederson", "G", 31, 90, 6.8, saves=3, passes=34),
        PlayerMatchStats("Rico Lewis", "D", 82, 12, 6.4, substitute=True),
        PlayerMatchStats("A Very Long Playername Indeed", "D", 5, 0, None, substitute=True),
    ],
    events=[
        MatchEvent(12, "Goal", "Normal Goal", "Erling Haaland", "Phil Foden", "Manchester City", True),
        MatchEvent(58, "Goal", "Penalty", "Erling Haaland", None, "Manchester City", True),
        MatchEvent(71, "Goal", "Normal Goal", "Bukayo Saka", None, "Arsenal", False),
        MatchEvent(90, "Card", "Yellow Card", "Rodri", None, "Manchester City", True),
        MatchEvent(64, "Goal", "Missed Penalty", "Kai Havertz", None, "Arsenal", False),
    ],
)
print("\n--- postmatch (digest) ---")
print(fmt.format_postmatch(report, "Europe/London", "Manchester City"))
print("\n--- postmatch (full /lineup) ---")
full = fmt.format_postmatch(report, "Europe/London", "Manchester City", full=True)
print(full)
assert "Missed Penalty" not in full  # missed penalties are not goals
assert "8.4" in full and "Haaland" in full
assert len(full) <= fmt.MAX_MESSAGE

print("\n--- next / results / weekly ---")
print(fmt.format_next([soon, fa], "Asia/Seoul", "Manchester City"))
print()
print(fmt.format_results([f], "Europe/London", "Manchester City"))
print()
print(fmt.format_weekly([soon], "America/New_York", "Manchester City"))
print()
print(fmt.format_weekly([], "UTC", "Manchester City"))

print("\n--- table ---")
print(fmt.format_table([
    TableRow(1, "Manchester City", 5, 4, 1, 0, 9, 13, is_tracked=True),
    TableRow(2, "Arsenal", 5, 4, 0, 1, 6, 12),
    TableRow(3, "Wolverhampton Wanderers", 5, 0, 1, 4, -8, 1),
], "Premier League"))

print("\n--- squad ---")
print(fmt.format_squad([
    SquadPlayer("Ederson", "Goalkeeper", 31, "Brazil", 32),
    SquadPlayer("Rúben Dias", "Defender", 3, "Portugal", 29),
    SquadPlayer("Rodri", "Midfielder", 16, "Spain", 30),
    SquadPlayer("Erling Haaland", "Offence", 9, "Norway", 26),
], "Manchester City"))

print("\n--- player season ---")
print(fmt.format_player_season({
    "player": {"name": "Erling Haaland", "age": 26, "nationality": "Norway", "height": "195 cm"},
    "statistics": [{"league": {"name": "Premier League"},
                    "games": {"appearences": 5, "minutes": 441, "rating": "8.216"},
                    "goals": {"total": 7, "assists": 1},
                    "shots": {"total": 19}, "passes": {"key": 6}}],
}, "Manchester City"))

# escaping / truncation
nasty = Fixture(key="x", provider="p", competition="A <b>& C</b>", competition_code="",
                opponent="Bad <script>", home=True, kickoff=now + timedelta(hours=2), status="TIMED")
out = fmt.format_prematch(nasty, "UTC", "Man <City>", 60)
assert "<script>" not in out and "&lt;script&gt;" in out
assert fmt.zone("Not/AZone").key == "UTC"
assert len(fmt.truncate("x" * 9000)) <= fmt.MAX_MESSAGE
print("\nescaping + truncation OK")

svc = FootballService(cfg)
assert svc.football_data and svc.api_football
print("service wiring OK ->", svc.status_lines())

print("\nALL SMOKE TESTS PASSED")
