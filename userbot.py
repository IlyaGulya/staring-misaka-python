import logging

from telethon import TelegramClient

from env import API_ID, API_HASH, USERBOT_SESSION_PATH

logger = logging.getLogger(__name__)


class UserBot:
    def __init__(self):
        self.client = TelegramClient(USERBOT_SESSION_PATH, API_ID, API_HASH)
        logger.info("UserBot client initialized")

    def start(self):
        self.client.start()
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


def create_userbot() -> UserBot:
    return UserBot()
