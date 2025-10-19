import asyncio
import logging

from config import load_config
from db import make_session_factory, initialize_database
from llm import create_llm
from telegram import create_bot
from userbot import create_userbot
from queue_processor import QueueProcessor


async def main():
    logger = logging.getLogger(__name__)

    # Load and validate configuration first
    try:
        config = load_config()
        logger.info("Configuration loaded and validated successfully")
    except Exception as e:
        logger.error(f"Failed to load configuration: {e}")
        return 1

    try:
        # Initialize components with configuration
        session_factory = make_session_factory(config)
        logger.info("Database session factory created")

        # Initialize database with default settings
        initialize_database(session_factory, config)
        logger.info("Database initialized")

        llm = create_llm(config)
        userbot = create_userbot(config)

        # Start userbot first
        await userbot.start()
        logger.info("Userbot started")

        # Create and start bot
        bot = create_bot(session_factory, llm, userbot, config)
        await bot.start(bot_token=config.bot_token)
        logger.info("Telegram bot started")

        # Create queue processor
        queue_processor = QueueProcessor(session_factory, llm, userbot, bot, config)

        # Set queue processor reference on bot
        bot.queue_processor = queue_processor
        logger.info("Queue processor connected to bot")

        # Start queue processor
        queue_processor_task = asyncio.create_task(queue_processor.start())
        logger.info("Queue processor started")

        logger.info("All components started successfully")

        try:
            # Run both the bot and queue processor
            await asyncio.gather(
                bot.run_until_disconnected(),
                queue_processor_task
            )
        except KeyboardInterrupt:
            logger.info("Shutting down...")
            queue_processor.stop()
            await bot.disconnect()
            await userbot.disconnect()
            return 0

    except Exception as e:
        logger.error(f"Error during startup: {e}")
        return 1


if __name__ == '__main__':
    # Configure logging with a cleaner format
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(name)s - %(message)s'
    )

    # Reduce verbosity of third-party libraries
    logging.getLogger('telethon').setLevel(logging.WARNING)
    logging.getLogger('anthropic').setLevel(logging.WARNING)
    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('httpcore').setLevel(logging.WARNING)

    asyncio.run(main())
