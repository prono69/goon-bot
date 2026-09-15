import logging
from logging.handlers import RotatingFileHandler
import sys

import bot.config as config

_configured = False  # guards against double-setup


def setup_logger() -> logging.Logger:
    """Configures root logger handlers. Call this ONCE, from main.py."""
    global _configured
    root_logger = logging.getLogger()

    if _configured:
        return logging.getLogger("goon_bot")

    level_name = getattr(config, "LOG_LEVEL", "INFO").upper()
    log_level = getattr(logging, level_name, logging.INFO)

    log_format = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    date_format = "%Y-%m-%d %H:%M:%S"
    formatter = logging.Formatter(log_format, datefmt=date_format)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)

    file_handler = RotatingFileHandler(
        filename="log.txt",
        maxBytes=700 * 1024,
        backupCount=1,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)

    root_logger.setLevel(log_level)
    root_logger.addHandler(stream_handler)
    root_logger.addHandler(file_handler)

    logging.getLogger("pyrogram").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("pymongo").setLevel(logging.WARNING)
    logging.getLogger("motor").setLevel(logging.WARNING)

    _configured = True
    return logging.getLogger("goon_bot")


# Safe to import anywhere, anytime — this line does NOT open files or
# add handlers. logging.getLogger() just returns/creates a named logger
# object; it's a no-op the first N times you call it with the same name.
logger = logging.getLogger("goon_bot")