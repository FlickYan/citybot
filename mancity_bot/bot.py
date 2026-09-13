"""Telegram command surface and application wiring."""

from __future__ import annotations

import logging
from datetime import time as dtime
from datetime import timezone

from telegram import BotCommand, Update
from telegram.constants import ParseMode
from telegram.error import NetworkError
from telegram.ext import Application, CommandHandler, ContextTypes

from . import formatting as fmt
from .config import Config
from .db import Database, Subscriber
from .notifier import Notifier
from .providers import FootballService

log = logging.getLogger(__name__)

COMMANDS = [
    BotCommand("next", "Upcoming fixtures"),
    BotCommand("results", "Recent results and form"),
    BotCommand("lineup", "Full player ratings from the last match"),
    BotCommand("table", "League table (PL or CL)"),
    BotCommand("squad", "Current squad list"),
    BotCommand("player", "Season stats for one player"),
    BotCommand("subscribe", "Turn notifications on"),
    BotCommand("unsubscribe", "Turn notifications off"),
    BotCommand("settings", "Show your notification settings"),
    BotCommand("timezone", "Set your timezone"),
    BotCommand("leadtimes", "Set reminder lead times in minutes"),
    BotCommand("toggle", "Toggle prematch / postmatch / weekly"),
    BotCommand("status", "Provider and scheduler health"),
    BotCommand("help", "Show all commands"),
]

HELP = """<b>{team} tracker</b>

<b>Fixtures &amp; results</b>
/next [n] — upcoming matches
/results [n] — recent results and form
/lineup — full player ratings from the last match
/table [PL|CL] — league table

<b>Players</b>
/squad — current squad
/player &lt;name&gt; — season stats, e.g. <code>/player Haaland</code>

<b>Notifications</b>
/subscribe — start receiving alerts
/unsubscribe — stop
/settings — what you currently get
/timezone &lt;zone&gt; — e.g. <code>/timezone Asia/Seoul</code>
/leadtimes &lt;mins&gt; — e.g. <code>/leadtimes 1440,60,15</code>
/toggle &lt;prematch|postmatch|weekly&gt;

/status — provider health"""


def _service(context: ContextTypes.DEFAULT_TYPE) -> FootballService:
    return context.application.bot_data["service"]


def _db(context: ContextTypes.DEFAULT_TYPE) -> Database:
    return context.application.bot_data["db"]


def _config(context: ContextTypes.DEFAULT_TYPE) -> Config:
    return context.application.bot_data["config"]


def _tz(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> str:
    sub = _db(context).get(chat_id)
    return sub.tz if sub else _config(context).default_timezone


async def _reply(update: Update, text: str) -> None:
    if update.effective_message:
        await update.effective_message.reply_text(
            text, parse_mode=ParseMode.HTML, disable_web_page_preview=True
        )


def _count_arg(context: ContextTypes.DEFAULT_TYPE, default: int, cap: int = 15) -> int:
    if context.args:
        try:
            return max(1, min(cap, int(context.args[0])))
        except ValueError:
            pass
    return default


# ------------------------------------------------------------------- commands


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = _config(context)
    chat_id = update.effective_chat.id
    created = _db(context).subscribe(chat_id, cfg.default_timezone, cfg.default_lead_times)
    intro = HELP.format(team=fmt.esc(cfg.team_name))
    if created:
        leads = ", ".join(str(m) for m in cfg.default_lead_times)
        intro = (
            f"You are subscribed to <b>{fmt.esc(cfg.team_name)}</b> alerts.\n"
            f"Reminders at {fmt.esc(leads)} minutes before kick-off, "
            f"timezone <code>{fmt.esc(cfg.default_timezone)}</code>.\n\n" + intro
        )
    await _reply(update, intro)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply(update, HELP.format(team=fmt.esc(_config(context).team_name)))


async def cmd_subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = _config(context)
    created = _db(context).subscribe(
        update.effective_chat.id, cfg.default_timezone, cfg.default_lead_times
    )
    await _reply(
        update,
        "Subscribed. You will get match reminders, post-match digests and a weekly preview."
        if created
        else "You are already subscribed. /settings shows what you get.",
    )


async def cmd_unsubscribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    removed = _db(context).unsubscribe(update.effective_chat.id)
    await _reply(
        update,
        "Unsubscribed. /subscribe brings the alerts back."
        if removed
        else "You were not subscribed.",
    )


async def cmd_next(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = _config(context)
    fixtures = await _service(context).next_fixtures(_count_arg(context, 5))
    await _reply(
        update, fmt.format_next(fixtures, _tz(context, update.effective_chat.id), cfg.team_name)
    )


async def cmd_results(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = _config(context)
    fixtures = await _service(context).recent_fixtures(_count_arg(context, 5))
    await _reply(
        update,
        fmt.format_results(fixtures, _tz(context, update.effective_chat.id), cfg.team_name),
    )


async def cmd_lineup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = _config(context)
    service = _service(context)
    recent = await service.recent_fixtures(1)
    if not recent:
        await _reply(update, "No finished match to report on yet.")
        return
    report = await service.match_report(recent[0])
    await _reply(
        update,
        fmt.format_postmatch(
            report, _tz(context, update.effective_chat.id), cfg.team_name, full=True
        ),
    )


async def cmd_table(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    competition = (context.args[0] if context.args else "PL").upper()
    rows = await _service(context).standings(competition)
    await _reply(update, fmt.format_table(rows, competition))


async def cmd_squad(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    players = await _service(context).squad()
    await _reply(update, fmt.format_squad(players, _config(context).team_name))


async def cmd_player(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await _reply(update, "Usage: <code>/player Haaland</code>")
        return
    name = " ".join(context.args)
    service = _service(context)
    if not service.player_stats_available:
        await _reply(
            update,
            "Player stats are unavailable.\n\n" + "\n".join(
                fmt.esc(line) for line in service.status_lines()[1:]
            ),
        )
        return
    payload = await service.player_season(name)
    if not payload:
        await _reply(update, f"No player matching {fmt.esc(name)} in the current squad.")
        return
    await _reply(update, fmt.format_player_season(payload, _config(context).team_name))


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    sub = _db(context).get(update.effective_chat.id)
    if not sub:
        await _reply(update, "Not subscribed. /subscribe to start.")
        return
    await _reply(update, _settings_text(sub))


def _settings_text(sub: Subscriber) -> str:
    def mark(on: bool) -> str:
        return "on" if on else "off"

    leads = ", ".join(str(m) for m in sub.lead_times) or "none"
    return (
        "<b>Your settings</b>\n"
        f"Timezone: <code>{fmt.esc(sub.tz)}</code>\n"
        f"Reminder lead times: <code>{fmt.esc(leads)}</code> min\n"
        f"Pre-match reminders: {mark(sub.prematch)}\n"
        f"Post-match digests: {mark(sub.postmatch)}\n"
        f"Weekly preview: {mark(sub.weekly)}"
    )


async def cmd_timezone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db, chat_id = _db(context), update.effective_chat.id
    if not db.get(chat_id):
        await _reply(update, "Not subscribed. /subscribe first.")
        return
    if not context.args:
        await _reply(update, "Usage: <code>/timezone Europe/London</code>")
        return
    name = context.args[0]
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        await _reply(update, f"Unknown timezone {fmt.esc(name)}. Use an IANA name.")
        return
    db.set_timezone(chat_id, name)
    await _reply(update, f"Timezone set to <code>{fmt.esc(name)}</code>.")


async def cmd_leadtimes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db, chat_id = _db(context), update.effective_chat.id
    if not db.get(chat_id):
        await _reply(update, "Not subscribed. /subscribe first.")
        return
    if not context.args:
        await _reply(update, "Usage: <code>/leadtimes 1440,60,15</code> (minutes)")
        return
    try:
        leads = sorted(
            {int(x) for x in " ".join(context.args).replace(",", " ").split() if x},
            reverse=True,
        )
    except ValueError:
        await _reply(update, "Lead times must be whole numbers of minutes.")
        return
    leads = [m for m in leads if 1 <= m <= 10080][:6]
    if not leads:
        await _reply(update, "Give at least one lead time between 1 and 10080 minutes.")
        return
    db.set_lead_times(chat_id, leads)
    await _reply(update, f"Reminders set to {fmt.esc(', '.join(map(str, leads)))} min before kick-off.")


async def cmd_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db, chat_id = _db(context), update.effective_chat.id
    sub = db.get(chat_id)
    if not sub:
        await _reply(update, "Not subscribed. /subscribe first.")
        return
    flag = (context.args[0].lower() if context.args else "")
    if flag not in {"prematch", "postmatch", "weekly"}:
        await _reply(update, "Usage: <code>/toggle prematch|postmatch|weekly</code>")
        return
    new_value = not getattr(sub, flag)
    db.set_flag(chat_id, flag, new_value)
    await _reply(update, f"{fmt.esc(flag)} is now <b>{'on' if new_value else 'off'}</b>.")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    service, db, cfg = _service(context), _db(context), _config(context)
    lines = [f"<b>{fmt.esc(cfg.team_name)} tracker</b>", ""]
    lines += [fmt.esc(line) for line in service.status_lines()]
    if service.api_football:
        try:
            account = await service.api_football.account()
            plan = (account.get("subscription") or {}).get("plan", "?")
            used = account.get("requests") or {}
            lines.append(
                fmt.esc(
                    f"  plan {plan} · {used.get('current', '?')}/"
                    f"{used.get('limit_day', '?')} requests today"
                )
            )
        except Exception:  # noqa: BLE001 - status must never fail loudly
            pass
    try:
        fixtures = await service.fixtures()
        upcoming = [f for f in fixtures if not f.finished]
        lines.append(f"Fixtures cached: {len(fixtures)} ({len(upcoming)} upcoming)")
        if upcoming:
            lines.append(
                "Next: "
                + fmt.esc(
                    f"{upcoming[0].opponent} — "
                    f"{fmt.clock(upcoming[0].kickoff, _tz(context, update.effective_chat.id))}"
                )
            )
    except Exception as exc:  # noqa: BLE001
        lines.append(f"Fixture load failed: {fmt.esc(exc)}")
    lines.append(f"Subscribers: {len(db.all())}")
    await _reply(update, "\n".join(lines))


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    if isinstance(context.error, NetworkError):
        # Connectivity blips (sleep/wake, Wi-Fi drops) are retried by the
        # polling loop; a full traceback per retry just buries real errors.
        log.warning("network error, will retry: %s", context.error)
        return
    log.exception("handler error", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "Something went wrong fetching that. Try again in a moment."
            )
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------- wiring


async def _post_init(app: Application) -> None:
    await app.bot.set_my_commands(COMMANDS)
    log.info("bot @%s ready", (await app.bot.get_me()).username)


async def _post_shutdown(app: Application) -> None:
    service: FootballService = app.bot_data.get("service")
    if service:
        await service.aclose()
    db: Database = app.bot_data.get("db")
    if db:
        db.close()


def build_application(cfg: Config) -> Application:
    db = Database(cfg.db_path)
    service = FootballService(cfg)
    notifier = Notifier(db, service, cfg.team_name)

    app = (
        Application.builder()
        .token(cfg.telegram_token)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )
    app.bot_data.update({"db": db, "service": service, "config": cfg, "notifier": notifier})

    handlers = {
        "start": cmd_start,
        "help": cmd_help,
        "subscribe": cmd_subscribe,
        "unsubscribe": cmd_unsubscribe,
        "next": cmd_next,
        "fixtures": cmd_next,
        "results": cmd_results,
        "last": cmd_results,
        "lineup": cmd_lineup,
        "table": cmd_table,
        "squad": cmd_squad,
        "player": cmd_player,
        "settings": cmd_settings,
        "timezone": cmd_timezone,
        "leadtimes": cmd_leadtimes,
        "toggle": cmd_toggle,
        "status": cmd_status,
    }
    for name, handler in handlers.items():
        app.add_handler(CommandHandler(name, handler))
    app.add_error_handler(on_error)

    queue = app.job_queue
    queue.run_repeating(notifier.prematch_job, interval=60, first=20, name="prematch")
    queue.run_repeating(
        notifier.results_job,
        interval=cfg.result_poll_minutes * 60,
        first=45,
        name="results",
    )
    queue.run_repeating(notifier.weekly_job, interval=3600, first=90, name="weekly")
    queue.run_daily(
        notifier.maintenance_job,
        time=dtime(hour=4, minute=0, tzinfo=timezone.utc),
        name="maintenance",
    )
    return app
