#!/usr/bin/env python3
"""Entry point. Starts the Telegram bot in long-polling mode."""

from __future__ import annotations

import logging

from mancity_bot.bot import build_application
from mancity_bot.config import load_config


def main() -> None:
    cfg = load_config()
    logging.basicConfig(
        level=getattr(logging, cfg.log_level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)

    log = logging.getLogger("run")
    log.info("tracking %s", cfg.team_name)

    app = build_application(cfg)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
