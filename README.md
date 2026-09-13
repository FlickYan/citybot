# Manchester City tracker — Telegram bot

A long-polling Telegram bot that follows one club across every competition it
plays in (Premier League, Champions League, domestic cups), reminds you before
kick-off, and sends a cleaned-up player-level digest once the final whistle goes.

Configured for Manchester City out of the box; point the three `*_TEAM_ID`
variables at another club and everything else follows.

## What it does

| | |
|---|---|
| **Pre-match reminders** | At each lead time you choose (default 24h / 1h / 15min), with competition, opponent, venue and kick-off in *your* timezone. |
| **Post-match digest** | Scoreline, goal timeline with assists, team stats (possession, shots, xG, formation), and standout performers by match rating — not a raw API dump. |
| **Weekly preview** | Every Monday morning (configurable), the week's fixtures across all competitions. |
| **On demand** | `/next`, `/results`, `/lineup`, `/table`, `/squad`, `/player <name>`. |

Multiple chats can subscribe; each keeps its own timezone, lead times and
per-notification-type on/off switches.

## Setup

**1. Get a bot token** — message [@BotFather](https://t.me/BotFather) on
Telegram, send `/newbot`, follow the prompts, copy the token.

**2. Get data API keys.** The bot uses two providers behind one interface:

- [football-data.org](https://www.football-data.org/client/register) — free, no
  card. Supplies the fixture list, results, standings and squad. This is the
  provider that gives broad competition coverage cheaply.
- [API-Football](https://dashboard.api-football.com/register) — supplies
  **per-player match statistics**, lineups, the goal timeline and xG.

Either key alone is enough to start; both together give the full experience.

> **API-Football's free plan cannot serve the current season.** It is limited to
> seasons 2022–2024 and refuses any request for a later one
> (`Free plans do not have access to this season, try from 2022 to 2024`). The
> code detects this on the first refusal, disables the provider for the run so
> it does not burn the 100/day quota on requests that cannot succeed, and says
> so in `/status`. Everything sourced from football-data.org — fixtures,
> reminders, results, standings, squad — is unaffected.
>
> To get player ratings, goal timelines and xG for live matches you need a paid
> API-Football tier (Pro is ~$19/month). No code change is required: drop the
> new key into `.env` and restart.

**3. Configure and run.**

```bash
cp .env.example .env    # then fill in the three keys
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt
.venv/Scripts/python run.py
```

On macOS/Linux use `.venv/bin/python` instead.

**4. In Telegram**, send your bot `/start`. That subscribes the chat and prints
the command list.

### Docker

```bash
docker compose up -d --build
```

The SQLite file lives in `./data`, so subscriptions and the sent-message log
survive rebuilds.

## Commands

```
/next [n]            upcoming fixtures
/results [n]         recent results and form guide
/lineup              full player ratings from the last match
/table [PL|CL]       league table
/squad               current squad by position
/player <name>       season stats, e.g. /player Haaland

/subscribe           start receiving alerts
/unsubscribe         stop
/settings            show your current configuration
/timezone <zone>     IANA name, e.g. /timezone Asia/Seoul
/leadtimes <mins>    e.g. /leadtimes 1440,60,15
/toggle <type>       prematch | postmatch | weekly
/status              provider health and cache state
```

## Configuration

All settings live in `.env` (see `.env.example` for the annotated list). The
ones worth knowing:

| Variable | Default | Meaning |
|---|---|---|
| `DEFAULT_LEAD_TIMES` | `1440,60,15` | Minutes before kick-off to remind, for new subscribers. |
| `FIXTURE_REFRESH_MINUTES` | `180` | How long the fixture list is cached. |
| `RESULT_POLL_MINUTES` | `10` | How often to check whether a match has finished. |
| `WEEKLY_DIGEST_DOW` / `_HOUR` | `0` / `9` | Monday 09:00, in each subscriber's local time. |
| `TEAM_NAME`, `FOOTBALL_DATA_TEAM_ID`, `API_FOOTBALL_TEAM_ID` | Man City (`65`, `50`) | The club being tracked. |

## How it stays inside the free tiers

The free plans are the real constraint (football-data.org allows 10
requests/minute; API-Football 100 requests/day), so the design is built around
spending as few requests as possible:

- Every outbound call passes through a per-provider token-bucket throttle with
  429-aware backoff (`providers/base.py`).
- The fixture list is fetched once every `FIXTURE_REFRESH_MINUTES` and shared by
  every subscriber and every job.
- The result poller only forces a fresh fetch when a match kicked off more than
  105 minutes ago and still is not marked finished — otherwise it reads the cache.
- The expensive per-player endpoints are called **once per match**, not once per
  subscriber: the report is built once and formatted per recipient.
- Squad data is cached for 24h, standings for 1h, player season stats for 6h.

## Reliability

- Every notification is written to a `sent` table keyed by
  `(chat_id, notification key)` before it counts as delivered, so restarts,
  retries and overlapping ticks cannot produce a duplicate message.
- Reminders have a 15-minute grace window: a short outage still lets a missed
  reminder through, but the bot will not resurrect reminders from hours ago.
- If a user blocks the bot, the `Forbidden` error unsubscribes them instead of
  retrying forever.
- A provider failure degrades rather than crashes — if API-Football is down,
  out of quota or refusing on plan grounds, digests fall back to the scoreline
  from football-data.org.
- A plan refusal is treated differently from a transient error: retrying cannot
  help, so the provider is switched off for the run instead of being retried.

## Requirements note

Needs `python-telegram-bot >= 22.8`. Earlier releases call
`asyncio.get_event_loop()` inside `run_polling()`, which raises on Python 3.13+
(`RuntimeError: There is no current event loop`). The Docker image pins Python
3.12, so this only bites local runs on a newer interpreter.

## Project layout

```
run.py                      entry point (long polling)
mancity_bot/
  config.py                 .env loading, season calculation
  models.py                 provider-neutral Fixture / PlayerMatchStats / MatchReport
  db.py                     SQLite: subscribers + sent-notification log
  formatting.py             all Telegram message rendering
  notifier.py               the four scheduled jobs
  bot.py                    command handlers and application wiring
  providers/
    base.py                 throttled HTTP client with retries
    football_data.py        football-data.org v4 adapter
    api_football.py         API-Football v3 adapter
    service.py              provider selection, caching, fallback
tests/smoke_test.py         offline test — no keys, no network
```

## Tests

```bash
.venv/Scripts/python tests/smoke_test.py
```

Runs entirely offline against synthetic payloads: fixture parsing for both
providers (home/away, finished/scheduled), the database layer, HTML escaping,
message truncation, and every formatter. It also prints a sample of each
message type, which is the fastest way to preview a formatting change.

## Extending it

- **Live goal alerts** — add a job polling `/fixtures/events` while
  `fixture.live` is true and diff against the events already sent. The dedupe
  table already supports it (`mark_sent(chat_id, f"event:{fixture_id}:{n}")`).
- **A second club** — the `team_id` is a constructor argument on both adapters,
  so a per-subscriber club is a schema change plus a loop, not a rewrite.
- **Webhooks instead of polling** — replace `app.run_polling()` in `run.py` with
  `app.run_webhook(...)`; nothing else assumes the transport.
