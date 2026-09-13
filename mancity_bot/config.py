"""Runtime configuration, loaded from environment / .env."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date

from dotenv import load_dotenv

load_dotenv()


def _int_list(raw: str) -> list[int]:
    out = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if chunk:
            out.append(int(chunk))
    return sorted(set(out), reverse=True)


def current_season(today: date | None = None) -> int:
    """European seasons are labelled by their starting year (2026 == 2026/27)."""
    today = today or date.today()
    return today.year if today.month >= 7 else today.year - 1


@dataclass(frozen=True)
class Config:
    telegram_token: str
    football_data_token: str
    api_football_key: str
    api_football_host: str

    team_name: str
    football_data_team_id: int
    api_football_team_id: int

    default_timezone: str
    default_lead_times: list[int] = field(default_factory=lambda: [1440, 60, 15])

    fixture_refresh_minutes: int = 180
    result_poll_minutes: int = 10
    weekly_digest_dow: int = 0
    weekly_digest_hour: int = 9

    db_path: str = "data/mancity.sqlite3"
    log_level: str = "INFO"

    @property
    def has_football_data(self) -> bool:
        return bool(self.football_data_token)

    @property
    def has_api_football(self) -> bool:
        return bool(self.api_football_key)


def load_config() -> Config:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit(
            "TELEGRAM_BOT_TOKEN is not set. Copy .env.example to .env and fill it in."
        )

    cfg = Config(
        telegram_token=token,
        football_data_token=os.getenv("FOOTBALL_DATA_TOKEN", "").strip(),
        api_football_key=os.getenv("API_FOOTBALL_KEY", "").strip(),
        api_football_host=os.getenv(
            "API_FOOTBALL_HOST", "v3.football.api-sports.io"
        ).strip(),
        team_name=os.getenv("TEAM_NAME", "Manchester City").strip(),
        football_data_team_id=int(os.getenv("FOOTBALL_DATA_TEAM_ID", "65")),
        api_football_team_id=int(os.getenv("API_FOOTBALL_TEAM_ID", "50")),
        default_timezone=os.getenv("DEFAULT_TIMEZONE", "Europe/London").strip(),
        default_lead_times=_int_list(os.getenv("DEFAULT_LEAD_TIMES", "1440,60,15")),
        fixture_refresh_minutes=int(os.getenv("FIXTURE_REFRESH_MINUTES", "180")),
        result_poll_minutes=int(os.getenv("RESULT_POLL_MINUTES", "10")),
        weekly_digest_dow=int(os.getenv("WEEKLY_DIGEST_DOW", "0")),
        weekly_digest_hour=int(os.getenv("WEEKLY_DIGEST_HOUR", "9")),
        db_path=os.getenv("DB_PATH", "data/mancity.sqlite3").strip(),
        log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper(),
    )

    if not (cfg.has_football_data or cfg.has_api_football):
        raise SystemExit(
            "No data provider configured. Set FOOTBALL_DATA_TOKEN and/or "
            "API_FOOTBALL_KEY in .env."
        )
    return cfg
