import os
import sys
import asyncio
import signal
from bot import setup_logger
from bot.config import API_ID, API_HASH, BOT_TOKEN
from bot.database.db import mongo
from pyrogram import Client, idle

# Logging
logger = setup_logger()  # opens log.txt, attaches handlers — runs once, here only

# Bot
async def run_bot() -> None:
    """Start and keep the Telegram bot running."""

    app = Client(
        name="GoonBot",
        api_id=int(API_ID),
        api_hash=API_HASH,
        bot_token=BOT_TOKEN,
        plugins={
            "root": "bot/plugins",
        },
    )

    loop = asyncio.get_running_loop()

    def shutdown_handler() -> None:
        logger.info("Shutdown signal received.")
        loop.create_task(shutdown())

    async def shutdown() -> None:
        if app.is_connected:
            logger.info("Stopping Telegram bot...")
            await app.stop()
            logger.info("Telegram bot stopped successfully.")

    # Register graceful shutdown handlers
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, shutdown_handler)
        except (NotImplementedError, RuntimeError):
            # Some environments don't support custom signal handlers.
            pass

    try:
        logger.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        logger.info("Starting Telegram bot...")
        logger.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

        await app.start()
        await mongo.connect()

        me = await app.get_me()

        logger.info("Bot started successfully!")
        logger.info("Username : @%s", me.username)
        logger.info("Bot ID   : %s", me.id)
        logger.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

        await idle()

    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot interrupted by user.")

    except Exception:
        logger.critical(
            "Fatal error occurred while running the bot.",
            exc_info=True,
        )

    finally:
        if app.is_connected:
            try:
                logger.info("Performing final shutdown...")
                await app.stop()
                logger.info("Bot shutdown complete.")
            except Exception:
                logger.exception("Error while stopping the bot.")


def main() -> None:
    """Application entry point."""
    try:
        asyncio.run(run_bot())
    except KeyboardInterrupt:
        logger.info("Application terminated.")

if __name__ == "__main__":
    main()