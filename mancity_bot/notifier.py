"""Scheduled jobs: pre-match reminders, post-match digests, weekly fixture lists.

Every job is idempotent. Before sending anything it checks the `sent` table, so
restarts, retries and overlapping ticks never produce a duplicate message.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from telegram.constants import ParseMode
from telegram.error import Forbidden, TelegramError
from telegram.ext import ContextTypes

from . import formatting as fmt
from .db import Database, Subscriber
from .models import Fixture
from .providers import FootballService

log = logging.getLogger(__name__)

#: How late a reminder may still be sent after its exact moment passed.
#: Covers a short outage without resurrecting reminders from hours ago.
GRACE_MINUTES = 15

#: A match is assumed over (and worth polling for a result) this long after kick-off.
ASSUMED_DURATION_MINUTES = 105


class Notifier:
    def __init__(self, db: Database, service: FootballService, team_name: str) -> None:
        self.db = db
        self.service = service
        self.team = team_name

    # ------------------------------------------------------------------ sending

    async def _send(
        self, context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str, key: str
    ) -> bool:
        if self.db.was_sent(chat_id, key):
            return False
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except Forbidden:
            # User blocked the bot or deleted the chat: stop tracking them.
            log.info("chat %s blocked the bot, unsubscribing", chat_id)
            self.db.unsubscribe(chat_id)
            return False
        except TelegramError as exc:
            log.warning("send to %s failed (%s): %s", chat_id, key, exc)
            return False
        self.db.mark_sent(chat_id, key)
        return True

    # --------------------------------------------------------------- pre-match

    async def prematch_job(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        subscribers = [s for s in self.db.all() if s.prematch]
        if not subscribers:
            return
        try:
            fixtures = await self.service.fixtures()
        except Exception:  # noqa: BLE001 - a job must never kill the loop
            log.exception("prematch job could not load fixtures")
            return

        now = datetime.now(timezone.utc)
        horizon = now + timedelta(minutes=max(_max_lead(subscribers), 1) + GRACE_MINUTES)
        pending = [f for f in fixtures if not f.finished and now < f.kickoff <= horizon]

        for fixture in pending:
            remaining = (fixture.kickoff - now).total_seconds() / 60
            for sub in subscribers:
                for lead in sub.lead_times:
                    if not (lead - GRACE_MINUTES < remaining <= lead):
                        continue
                    text = fmt.format_prematch(fixture, sub.tz, self.team, lead)
                    if await self._send(
                        context, sub.chat_id, text, f"pre:{fixture.key}:{lead}"
                    ):
                        log.info(
                            "reminder %s min sent to %s for %s",
                            lead,
                            sub.chat_id,
                            fixture.key,
                        )

    # --------------------------------------------------------------- post-match

    async def results_job(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        subscribers = [s for s in self.db.all() if s.postmatch]
        if not subscribers:
            return
        try:
            fixtures = await self.service.fixtures()
            if self._awaiting_result(fixtures):
                # Only spend a request when a match should already have ended.
                fixtures = await self.service.fixtures(force=True)
        except Exception:  # noqa: BLE001
            log.exception("results job could not load fixtures")
            return

        cutoff = datetime.now(timezone.utc) - timedelta(days=3)
        finished = [f for f in fixtures if f.finished and f.kickoff > cutoff]

        for fixture in finished:
            targets = [
                s for s in subscribers if not self.db.was_sent(s.chat_id, f"post:{fixture.key}")
            ]
            if not targets:
                continue
            try:
                report = await self.service.match_report(fixture)
            except Exception:  # noqa: BLE001
                log.exception("match report failed for %s", fixture.key)
                continue
            for sub in targets:
                text = fmt.format_postmatch(report, sub.tz, self.team)
                await self._send(context, sub.chat_id, text, f"post:{fixture.key}")
            log.info("post-match digest sent for %s", fixture.key)

    @staticmethod
    def _awaiting_result(fixtures: list[Fixture]) -> bool:
        deadline = datetime.now(timezone.utc) - timedelta(minutes=ASSUMED_DURATION_MINUTES)
        return any(not f.finished and f.kickoff <= deadline for f in fixtures)

    # -------------------------------------------------------------- weekly digest

    async def weekly_job(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Runs hourly; fires per subscriber when it is their chosen local hour."""
        subscribers = [s for s in self.db.all() if s.weekly]
        if not subscribers:
            return
        cfg = context.application.bot_data["config"]
        try:
            fixtures = await self.service.fixtures()
        except Exception:  # noqa: BLE001
            log.exception("weekly job could not load fixtures")
            return

        now = datetime.now(timezone.utc)
        week_ahead = now + timedelta(days=7)
        upcoming = [f for f in fixtures if not f.finished and now < f.kickoff <= week_ahead]

        for sub in subscribers:
            moment = fmt.local(now, sub.tz)
            if moment.weekday() != cfg.weekly_digest_dow or moment.hour != cfg.weekly_digest_hour:
                continue
            year, week, _ = moment.isocalendar()
            text = fmt.format_weekly(upcoming, sub.tz, self.team)
            await self._send(context, sub.chat_id, text, f"weekly:{year}-W{week:02d}")

    # ------------------------------------------------------------- housekeeping

    async def maintenance_job(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        removed = self.db.prune_sent()
        if removed:
            log.info("pruned %d old notification records", removed)


def _max_lead(subscribers: list[Subscriber]) -> int:
    return max((max(s.lead_times) for s in subscribers if s.lead_times), default=0)
