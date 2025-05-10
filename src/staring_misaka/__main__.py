# src/staring_misaka/__main__.py
import asyncio
import logging

from telethon import TelegramClient  # Telegram client library

from .action_service import ActionService  # Service for actions like banning
from .command_handlers import CommandHandlers  # Handles bot commands
from .config import load_config  # Application configuration
from .db_utils import create_tables, get_db_session, init_db, initialize_default_data  # Database utilities
from .event_handlers import EventHandlers  # Handles Telegram events
from .llm_service import LLMService  # LLM interaction service
from .metrics_service import start_metrics_server, update_dynamic_gauges  # Prometheus metrics
from .web_ui import launch_gradio_ui  # Import Gradio UI launcher

# Configure basic logging for the application
# More advanced logging (e.g., file rotation, structured logging) can be added.
logging.basicConfig(
    level=logging.INFO,  # Default log level
    format='%(asctime)s - %(levelname)s - %(name)s - %(module)s.%(funcName)s:%(lineno)d - %(message)s'
)
logger = logging.getLogger(__name__)  # Get a logger for this main module


async def main():
    """Main asynchronous function to initialize and run the bot."""
    settings = load_config()  # Load configuration from environment or .env file
    logging.getLogger().setLevel(settings.log_level.upper())  # Apply log level from settings

    logger.info("Initializing Staring Misaka Bot...")

    # 1. Initialize Database
    init_db(settings)  # Setup database engine and session factory
    await create_tables()  # Create database tables if they don't exist
    await initialize_default_data(settings)  # Seed database with essential initial data
    logger.info("Database initialized successfully.")

    # 2. Initialize Prometheus Metrics Server
    start_metrics_server(settings)  # Expose metrics endpoint

    # 3. Initialize Telegram Client
    # The session file stores login information to avoid re-auth on every start.
    client = TelegramClient(
        str(settings.bot_session_path),  # Path to the session file
        settings.api_id,
        settings.api_hash
    )

    # 4. Initialize Core Services
    # LLMService needs the Telegram client to notify admin about queued checks.
    llm_service = LLMService(settings, client) # Defined here
    action_service = ActionService(settings, client) # Defined here

    # Event Handlers and Command Handlers setup
    # Event Handlers instance is passed to Command Handlers to allow cache updates (e.g., monitored chats).
    # Command Handlers instance needs LLMService for manual reprocessing command.
    event_handlers = EventHandlers(settings, client, llm_service, action_service)
    command_handlers = CommandHandlers(settings, client, action_service, event_handlers,
                                       llm_service)  # Pass llm_service

    # 5. Register Event and Command Handlers with the Telegram Client
    event_handlers.register_handlers()
    command_handlers.register_handlers()
    await event_handlers.update_monitored_chats_cache()  # Perform initial load of monitored chats
    logger.info("All services initialized and Telegram handlers registered.")

    # 6. Launch Gradio Web UI (if configured)
    gradio_server_task = None # Initialize
    if settings.gradio_username and settings.gradio_password and settings.gradio_password.get_secret_value():
        # launch_gradio_ui is now async and returns a task to be managed
        gradio_server_task = await launch_gradio_ui(
            settings=settings,
            llm_service=llm_service,         # Pass LLMService
            action_service=action_service,   # Pass ActionService
            # main_event_loop is no longer passed as it runs in the same loop
        )
    else:
        logger.info("Gradio UI not launched due to missing username/password configuration.")


    # --- Background Tasks ---
    # List to keep track of background tasks for graceful shutdown
    background_tasks = []
    if gradio_server_task: # Add Gradio task if it was created
        background_tasks.append(gradio_server_task)

    try:
        logger.info("Connecting to Telegram...")
        await client.start(bot_token=settings.bot_token.get_secret_value())  # Start as a bot
        logger.info("Bot successfully connected to Telegram and is now listening for events.")

        # Task for periodically updating Prometheus dynamic gauges
        async def gauges_updater_task_loop():
            """Background task to update Prometheus gauges periodically."""
            while True:
                try:
                    await update_dynamic_gauges()
                except Exception as e_gauge:  # Catch broad exceptions to keep the loop running
                    logger.error(f"Error in Prometheus gauges updater task: {e_gauge}", exc_info=True)
                await asyncio.sleep(60)  # Update interval (e.g., every 60 seconds)

        # Task for periodically processing the queued LLM checks
        async def llm_queue_processor_task_loop():
            """Background task to process failed LLM checks from the queue."""
            logger.info(
                f"LLM Queue processor starting. Interval: {settings.queue.processing_interval_seconds}s, Batch Size: {settings.queue.batch_size}")
            await asyncio.sleep(30)  # Initial delay before the first run
            while True:
                try:
                    # Use a new session for each batch processing run
                    async with get_db_session() as session:
                        # Pass action_service needed by reprocess_queued_item for creating PendingAdminAction
                        await llm_service.process_llm_queue_batch(session, action_service) # Pass action_service here
                except Exception as e_queue:  # Catch broad exceptions
                    logger.error(f"Error in LLM queue processor task: {e_queue}", exc_info=True)
                # Wait for the configured interval before the next run
                await asyncio.sleep(settings.queue.processing_interval_seconds)

        # Create and store background tasks
        gauges_task = asyncio.create_task(gauges_updater_task_loop())
        gauges_task.set_name("PrometheusGaugesUpdater")  # Set name for easier debugging
        background_tasks.append(gauges_task)

        llm_queue_task = asyncio.create_task(llm_queue_processor_task_loop())
        llm_queue_task.set_name("LLMQueueProcessor")
        background_tasks.append(llm_queue_task)

        if background_tasks: # Check if there are any tasks to log
            logger.info(f"Started {len(background_tasks)} background tasks: {[t.get_name() for t in background_tasks]}.")
        else:
            # This case should ideally not happen if other tasks are always started
            logger.info("No background tasks (including Gradio) were started.")
        # Keep the bot running until it's disconnected (e.g., by Ctrl+C or an error)
        await client.run_until_disconnected()

    except Exception as e:  # Catch any critical errors during bot startup or main execution
        logger.error(f"Critical error in bot main execution loop: {e}", exc_info=True)
    finally:
        # --- Graceful Shutdown ---
        logger.info("Initiating shutdown sequence...")
        # Cancel all background tasks
        for task in background_tasks:
            if task and not task.done():
                logger.info(f"Cancelling background task: {task.get_name()}...")
                task.cancel()
                try:
                    # Wait for the task to acknowledge cancellation
                    await task
                except asyncio.CancelledError:
                    logger.info(f"Background task {task.get_name()} was successfully cancelled.")
                except Exception as e_cancel:  # Catch any error during task's own cleanup
                    # Log error but continue shutdown
                    logger.error(f"Error during cleanup of background task {task.get_name()}: {e_cancel}",
                                 exc_info=True)

        # Disconnect the Telegram client if it's still connected
        if client.is_connected():
            logger.info("Disconnecting Telegram client...")
            await client.disconnect()
        logger.info("Staring Misaka Bot has stopped.")


def run():
    """Entry point function to start the bot using asyncio.run()."""
    try:
        asyncio.run(main())
    except KeyboardInterrupt:  # Handle Ctrl+C gracefully
        logger.info("Bot shutdown requested by user (KeyboardInterrupt).")
    except Exception as e:  # Catch any other unhandled exceptions at the top level
        logger.critical(f"Unhandled exception at run level: {e}", exc_info=True)


if __name__ == "__main__":
    # This block executes when the script is run directly
    run()