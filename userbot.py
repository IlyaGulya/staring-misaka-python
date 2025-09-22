import logging

from telethon import TelegramClient

logger = logging.getLogger(__name__)


class UserBot:
    def __init__(self, config):
        self.config = config
        self.client = TelegramClient(config.userbot_session_path, config.api_id, config.api_hash)
        logger.info("UserBot client initialized")

    async def start(self):
        await self.client.start()
        logger.info("UserBot started")

    async def stop(self):
        await self.client.disconnect()
        logger.info("UserBot stopped")

    async def send_ban_command(self, chat_id: int, message_id: int, reason: str):
        try:
            await self.client.send_message(
                entity=chat_id,
                message=f'/sban {reason}',
                reply_to=message_id
            )
            logger.info(f"Ban command sent for message {message_id} in chat {chat_id}")
        except Exception as e:
            logger.error(f"Error sending ban command: {str(e)}")


def create_userbot(config) -> UserBot:
    """Create UserBot with the given configuration"""
    return UserBot(config)
