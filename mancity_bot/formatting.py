"""Turning raw provider data into short, readable Telegram messages.

Everything here targets Telegram's HTML parse mode and its 4096-character
message limit.
"""

from __future__ import annotations

from datetime import datetime, timezone
from html import escape
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .models import Fixture, MatchReport, PlayerMatchStats, SquadPlayer, TableRow

MAX_MESSAGE = 4000  # a little under Telegram's 4096, leaving room for footers

OUTCOME_ICON = {"W": "✅", "D": "➡️", "L": "❌"}
COMPETITION_ICON = {
    "Premier League": "\U0001f3f4",
    "UEFA Champions League": "\U0001f3c6",
    "FA Cup": "\U0001f3c5",
    "EFL Cup": "\U0001f3c5",
    "Carabao Cup": "\U0001f3c5",
}


def esc(value: object) -> str:
    return escape(str(value), quote=False)


def zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def local(dt: datetime, tz: str) -> datetime:
    return dt.astimezone(zone(tz))


def clock(dt: datetime, tz: str) -> str:
    """e.g. 'Sat 13 Sep, 16:30 BST'"""
    moment = local(dt, tz)
    return (
        f"{moment.strftime('%a')} {moment.day} {moment.strftime('%b')}, "
        f"{moment.strftime('%H:%M %Z')}".strip()
    )


def countdown(dt: datetime) -> str:
    delta = dt - datetime.now(timezone.utc)
    minutes = int(delta.total_seconds() // 60)
    if minutes < 0:
        return "underway"
    if minutes < 60:
        return f"in {minutes} min"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"in {hours}h {minutes:02d}m"
    days, hours = divmod(hours, 24)
    return f"in {days}d {hours}h"


def truncate(text: str, limit: int = MAX_MESSAGE) -> str:
    if len(text) <= limit:
        return text
    cut = text[: limit - 20]
    return cut.rsplit("\n", 1)[0] + "\n<i>…truncated</i>"


def _icon(fixture: Fixture) -> str:
    return COMPETITION_ICON.get(fixture.competition, "⚽")


def _versus(fixture: Fixture, team: str) -> str:
    if fixture.home:
        return f"{esc(team)} vs {esc(fixture.opponent)}"
    return f"{esc(fixture.opponent)} vs {esc(team)}"


# --------------------------------------------------------------------- listings


def fixture_line(fixture: Fixture, tz: str) -> str:
    where = "H" if fixture.home else "A"
    return (
        f"{_icon(fixture)} <b>{esc(fixture.opponent)}</b> ({where}) — "
        f"{esc(clock(fixture.kickoff, tz))}\n"
        f"    <i>{esc(fixture.competition)}</i> · {esc(countdown(fixture.kickoff))}"
    )


def result_line(fixture: Fixture, tz: str) -> str:
    icon = OUTCOME_ICON.get(fixture.outcome or "", "⚪")
    where = "H" if fixture.home else "A"
    score = fixture.scoreline or "—"
    return (
        f"{icon} <b>{esc(score)}</b> vs {esc(fixture.opponent)} ({where})\n"
        f"    <i>{esc(fixture.competition)}</i> · "
        f"{esc(local(fixture.kickoff, tz).strftime('%d %b'))}"
    )


def format_next(fixtures: list[Fixture], tz: str, team: str) -> str:
    if not fixtures:
        return "No upcoming fixtures found. The season may be on a break."
    body = "\n\n".join(fixture_line(f, tz) for f in fixtures)
    return truncate(f"<b>Next up — {esc(team)}</b>\n\n{body}")


def format_results(fixtures: list[Fixture], tz: str, team: str) -> str:
    if not fixtures:
        return "No completed matches found yet."
    body = "\n\n".join(result_line(f, tz) for f in fixtures)
    form = " ".join(f.outcome or "?" for f in reversed(fixtures))
    return truncate(
        f"<b>Recent results — {esc(team)}</b>\n\n{body}\n\n<b>Form:</b> {esc(form)}"
    )


def format_weekly(fixtures: list[Fixture], tz: str, team: str) -> str:
    header = f"\U0001f4c5 <b>{esc(team)} this week</b>"
    if not fixtures:
        return f"{header}\n\nNo matches scheduled in the next 7 days. Enjoy the quiet."
    body = "\n\n".join(fixture_line(f, tz) for f in fixtures)
    return truncate(f"{header}\n\n{body}")


# -------------------------------------------------------------------- pre-match


def format_prematch(fixture: Fixture, tz: str, team: str, lead_minutes: int) -> str:
    if lead_minutes >= 1440:
        lead = f"{lead_minutes // 1440}d"
    elif lead_minutes >= 60:
        lead = f"{lead_minutes // 60}h"
    else:
        lead = f"{lead_minutes}m"

    lines = [
        f"⏰ <b>Kick-off in {esc(lead)}</b>",
        "",
        f"{_icon(fixture)} {_versus(fixture, team)}",
        f"<i>{esc(fixture.competition)}</i>"
        + (f" · {esc(fixture.matchday)}" if fixture.matchday else ""),
        f"\U0001f552 {esc(clock(fixture.kickoff, tz))}",
    ]
    if fixture.venue:
        lines.append(f"\U0001f3df {esc(fixture.venue)}")
    lines.append(f"\U0001f4cd {'Home' if fixture.home else 'Away'}")
    return truncate("\n".join(lines))


# ------------------------------------------------------------------- post-match


def _goal_lines(report: MatchReport) -> list[str]:
    goals = [
        e
        for e in report.events
        if e.kind.lower() == "goal" and "missed" not in e.detail.lower()
    ]
    if not goals:
        return []
    out = []
    for event in sorted(goals, key=lambda e: e.minute or 0):
        marker = "⚽" if event.ours else "⬛"
        minute = f"{event.minute}'" if event.minute else "—"
        text = f"{marker} {esc(minute)} {esc(event.player or 'Unknown')}"
        if event.detail and event.detail.lower() not in {"normal goal"}:
            text += f" <i>({esc(event.detail)})</i>"
        if event.assist:
            text += f" — assist {esc(event.assist)}"
        out.append(text)
    return out


def _star_lines(players: list[PlayerMatchStats], limit: int = 3) -> list[str]:
    rated = [p for p in players if p.played and p.rating is not None]
    if not rated:
        return []
    rated.sort(key=lambda p: (p.rating or 0, p.contributions), reverse=True)
    out = []
    for player in rated[:limit]:
        bits = [f"{player.rating:.1f}"]
        if player.goals:
            bits.append(f"{player.goals}G")
        if player.assists:
            bits.append(f"{player.assists}A")
        if player.key_passes:
            bits.append(f"{player.key_passes} KP")
        if player.saves:
            bits.append(f"{player.saves} saves")
        out.append(
            f"⭐ <b>{esc(player.name)}</b> — {esc(' · '.join(bits))} "
            f"<i>({player.minutes}')</i>"
        )
    return out


def _squad_table(players: list[PlayerMatchStats]) -> list[str]:
    played = [p for p in players if p.played]
    if not played:
        return []
    played.sort(key=lambda p: (p.substitute, -(p.minutes or 0)))
    out = []
    for player in played:
        rating = f"{player.rating:.1f}" if player.rating else " – "
        marks = ""
        if player.goals:
            marks += "⚽" * player.goals
        if player.assists:
            marks += "\U0001f3af" * player.assists
        if player.yellow:
            marks += "\U0001f7e8"
        if player.red:
            marks += "\U0001f7e5"
        name = player.name if len(player.name) <= 18 else player.name[:17] + "…"
        out.append(
            f"<code>{esc(rating):>4} {esc(str(player.minutes or 0)):>3}'</code> "
            f"{esc(name)}{marks}"
        )
    return out


def format_postmatch(report: MatchReport, tz: str, team: str, full: bool = False) -> str:
    fixture = report.fixture
    icon = OUTCOME_ICON.get(fixture.outcome or "", "⚪")
    score = fixture.scoreline or "—"

    lines = [
        f"{icon} <b>Full time — {esc(score)}</b>",
        f"{_icon(fixture)} {_versus(fixture, team)}",
        f"<i>{esc(fixture.competition)}</i>"
        + (f" · {esc(fixture.matchday)}" if fixture.matchday else ""),
    ]

    goals = _goal_lines(report)
    if goals:
        lines += ["", "<b>Goals</b>", *goals]

    team_bits = []
    if report.possession is not None:
        team_bits.append(f"Possession {report.possession}%")
    if report.shots is not None:
        on = f" ({report.shots_on} on target)" if report.shots_on is not None else ""
        team_bits.append(f"Shots {report.shots}{on}")
    if report.expected_goals is not None:
        team_bits.append(f"xG {report.expected_goals:.2f}")
    if report.formation:
        team_bits.append(f"Formation {report.formation}")
    if team_bits:
        lines += ["", "<b>Team</b>", esc(" · ".join(team_bits))]

    stars = _star_lines(report.players)
    if stars:
        lines += ["", "<b>Standout performers</b>", *stars]

    if full:
        table = _squad_table(report.players)
        if table:
            lines += ["", "<b>Player ratings</b>", *table]
    elif report.players:
        lines += ["", "<i>/lineup for the full player ratings</i>"]

    if not report.players and not goals:
        if fixture.venue:
            lines.append(f"\U0001f3df {esc(fixture.venue)}")
        lines += [
            "",
            "<i>Player ratings and the goal timeline are not available for this "
            "match — see /status.</i>",
        ]

    return truncate("\n".join(lines))


# ------------------------------------------------------------------- reference


def format_table(rows: list[TableRow], competition: str) -> str:
    if not rows:
        return f"No standings available for {esc(competition)}."
    lines = [f"<b>{esc(competition)} table</b>", "<code>#  Team            Pl  GD Pts</code>"]
    for row in rows:
        name = row.team if len(row.team) <= 14 else row.team[:13] + "…"
        marker = "▸" if row.is_tracked else " "
        lines.append(
            f"<code>{row.position:<2} {name:<14} {row.played:>2} "
            f"{row.goal_difference:>+3} {row.points:>3}</code>{marker}"
        )
    return truncate("\n".join(lines))


def format_squad(players: list[SquadPlayer], team: str) -> str:
    if not players:
        return "Squad data unavailable."
    buckets: dict[str, list[SquadPlayer]] = {}
    for player in players:
        buckets.setdefault(player.position or "Other", []).append(player)

    order = ["Goalkeeper", "Defender", "Midfielder", "Attacker", "Offence", "Other"]
    lines = [f"<b>{esc(team)} squad</b> ({len(players)} players)"]
    for group in sorted(buckets, key=lambda g: (order.index(g) if g in order else 99, g)):
        lines.append(f"\n<b>{esc(group)}</b>")
        for player in sorted(buckets[group], key=lambda p: p.name):
            number = f"{player.number}. " if player.number else ""
            age = f" ({player.age})" if player.age else ""
            lines.append(f"  {esc(number)}{esc(player.name)}{esc(age)}")
    return truncate("\n".join(lines))


def format_player_season(payload: dict, team: str) -> str:
    player = payload.get("player") or {}
    name = player.get("name", "Unknown")
    lines = [
        f"<b>{esc(name)}</b>",
        esc(
            " · ".join(
                str(bit)
                for bit in (player.get("age"), player.get("nationality"), player.get("height"))
                if bit
            )
        ),
    ]
    for block in payload.get("statistics", []) or []:
        league = (block.get("league") or {}).get("name")
        if not league:
            continue
        games = block.get("games") or {}
        goals = block.get("goals") or {}
        passes = block.get("passes") or {}
        shots = block.get("shots") or {}
        rating = games.get("rating")
        bits = [
            f"{games.get('appearences') or 0} apps",
            f"{games.get('minutes') or 0}'",
            f"{goals.get('total') or 0}G",
            f"{goals.get('assists') or 0}A",
        ]
        if shots.get("total"):
            bits.append(f"{shots['total']} shots")
        if passes.get("key"):
            bits.append(f"{passes['key']} key passes")
        if rating:
            bits.append(f"avg {float(rating):.2f}")
        lines += ["", f"<b>{esc(league)}</b>", esc(" · ".join(bits))]
    return truncate("\n".join(lines))
