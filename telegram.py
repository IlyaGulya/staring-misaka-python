import logging
from datetime import datetime

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from telethon import TelegramClient, events
from telethon.tl.types import UpdateChannelParticipant

from db import NewUser, PendingBanRequest, BannedUser, AdminSettings
from env import TRACKING_CHAT_IDS, API_ID, API_HASH, ADMIN_ID, BOT_SESSION_PATH
from llm import Llm
from userbot import UserBot

# Configure logging
logger = logging.getLogger(__name__)


def create_bot(session: Session, llm: Llm, userbot: UserBot) -> TelegramClient:
    client = TelegramClient(BOT_SESSION_PATH, API_ID, API_HASH)
    logger.info("Creating Telegram bot client")

    @client.on(events.ChatAction(chats=TRACKING_CHAT_IDS))
    async def chat_action_handler(event):
        logger.info(f"Chat action event received: {event}")
        if event.chat_id not in TRACKING_CHAT_IDS:
            logger.info(f"Ignoring event from non-tracked chat: {event.chat_id}")
            return
        # Check if a user has joined or been added to the group
        if (event.user_added or event.user_joined) and isinstance(event.original_update, UpdateChannelParticipant):
            user_id = event.user.id
            logger.info(f"User {user_id} was added to the group {event.chat_id}")

            # Check if the user already exists in the new_users table
            existing_user = session.query(NewUser).filter_by(user_id=user_id, chat_id=event.chat_id).first()

            if existing_user:
                logger.info(f"User {user_id} already exists in NewUser table. Updating join time.")
                existing_user.join_time = datetime.utcnow()
            else:
                logger.info(f"Adding new user {user_id} to NewUser table")
                new_user = NewUser(user_id=user_id, chat_id=event.chat_id)
                session.add(new_user)

            try:
                session.commit()
                logger.info(f"Successfully updated/added user {user_id} in NewUser table")
            except SQLAlchemyError as e:
                logger.error(f"Error updating/adding user {user_id} to NewUser table: {str(e)}")
                session.rollback()
        else:
            logger.info("Ignoring non-user-added event or non-UpdateChannelParticipant event")

    @client.on(events.NewMessage(chats=TRACKING_CHAT_IDS))
    async def message_handler(event):
        logger.info(f"New message event received: {event}")
        sender = await event.get_sender()
        logger.info(f"Message sender: {sender.id}")

        # Check if sender is in the new_users table
        new_user = session.query(NewUser).filter_by(user_id=sender.id, chat_id=event.chat_id).first()
        if new_user:
            logger.info(f"Processing message from new user {sender.id}")
            message_text = event.raw_text
            logger.info(f"Message text: {message_text}")
            # Send to Claude API to check if spam
            is_spam = await llm.is_spam(message_text)
            logger.info(f"Spam check result for user {sender.id}: {is_spam}")

            admin_settings = session.query(AdminSettings).first()

            if is_spam:
                if admin_settings.require_approval:
                    # Notify admin
                    await notify_admin(sender, message_text, event)
                else:
                    # Automatically ban the user
                    await process_ban(
                        user_id=sender.id,
                        chat_id=event.chat_id,
                        message_id=event.id,
                        message_text=message_text,
                        is_automatic=True
                    )
            else:
                # If not spam, check if user should be approved
                await check_user_approval(sender.id, event.chat_id)
        else:
            logger.info(f"Message from existing user {sender.id}, ignoring")

    async def notify_admin(sender, message_text, event):
        logger.info(f"Notifying admin about potential spam from user {sender.id}")
        # Send a message to the admin
        admin_message = (
            f"User {sender.first_name} ({sender.id}) sent a message in chat {event.chat_id}:\n\n"
            f"{message_text}\n\nShould I ban this user? Reply 'yes' to ban."
        )
        sent_message = await client.send_message(ADMIN_ID, admin_message)
        logger.info(f"Sent admin notification message with ID: {sent_message.id}")
        # Store the pending request in the database
        pending_request = PendingBanRequest(
            admin_message_id=sent_message.id,
            sender_id=sender.id,
            original_chat_id=event.chat_id,
            original_message_id=event.id,
            message_text=message_text
        )
        session.add(pending_request)
        session.commit()
        logger.info(f"Added pending ban request for user {sender.id} to database")

    async def process_ban(user_id: int, chat_id: int, message_id: int, message_text: str, is_automatic: bool):
        logger.info(f"{'Automatically banning' if is_automatic else 'Admin approved ban for'} user {user_id}")

        # Send the ban command
        reason = f"autoban by staring misaka. message: {message_text}"
        await userbot.send_ban_command(chat_id, message_id, reason)

        # Store the ban information in the database
        banned_user = BannedUser(
            user_id=user_id,
            user_name=await get_user_name(user_id),
            chat_id=chat_id,
            message_text=message_text
        )
        session.add(banned_user)

        # Remove the user from NewUser table if they're still there
        new_user = session.query(NewUser).filter_by(user_id=user_id, chat_id=chat_id).first()
        if new_user:
            session.delete(new_user)

        session.commit()
        logger.info(f"Stored ban information for user {user_id}")

        # Notify admin about the ban
        admin_message = (
            f"User {user_id} has been {'automatically ' if is_automatic else ''}banned "
            f"{'due to spam detection' if is_automatic else 'as per admin approval'}."
        )
        await client.send_message(ADMIN_ID, admin_message)

        return banned_user

    @client.on(events.NewMessage(chats=[ADMIN_ID], from_users=[ADMIN_ID]))
    async def admin_reply_handler(event):
        logger.info(f"Received message from admin: {event}")

        if event.raw_text.startswith('/'):
            command = event.raw_text.lower().split()[0]
            if command == '/toggle_approval':
                admin_settings = session.query(AdminSettings).first()
                admin_settings.require_approval = not admin_settings.require_approval
                session.commit()
                await event.reply(
                    f"Admin approval is now {'required' if admin_settings.require_approval else 'not required'}")
            elif command == '/status':
                admin_settings = session.query(AdminSettings).first()
                await event.reply(
                    f"Admin approval is currently {'required' if admin_settings.require_approval else 'not required'}")
            else:
                await event.reply("Unknown command. Available commands: /toggle_approval, /status")
        elif event.reply_to_msg_id:
            # Check if this is a reply to our pending ban request
            pending_request = session.query(PendingBanRequest).filter_by(
                admin_message_id=event.reply_to_msg_id
            ).first()
            if pending_request:
                logger.info(f"Processing admin reply for pending ban request: {pending_request.sender_id}")
                if event.raw_text.strip().lower() == 'yes':
                    logger.info(f"Admin approved ban for user {pending_request.sender_id}")

                    await process_ban(
                        user_id=pending_request.sender_id,
                        chat_id=pending_request.original_chat_id,
                        message_id=pending_request.original_message_id,
                        message_text=pending_request.message_text,
                        is_automatic=False
                    )

                    # Remove the pending request from the database
                    session.delete(pending_request)
                    session.commit()
                    logger.info(f"Removed pending ban request for user {pending_request.sender_id} from database")
                else:
                    logger.info(f"Admin did not approve ban for user {pending_request.sender_id}")
                    await client.send_message(
                        ADMIN_ID, f"No action taken against user {pending_request.sender_id}."
                    )
                    # Remove the pending request from the database
                    session.delete(pending_request)
                    session.commit()
            else:
                logger.info("Admin reply does not correspond to a pending ban request")
        else:
            # Existing code for processing non-reply messages from admin
            is_spam = await llm.is_spam(event.raw_text)
            await client.send_message(ADMIN_ID, f"Is spam: {is_spam}")

    async def get_user_name(user_id):
        try:
            user = await client.get_entity(user_id)
            return user.username if user.username else user.first_name
        except Exception as e:
            logger.error(f"Error fetching user name for user_id {user_id}: {str(e)}")
            return None

    async def check_user_approval(user_id: int, chat_id: int):
        # This is a placeholder function. You should implement your own logic
        # to determine when a user should be approved (e.g., after a certain number of non-spam messages)
        # For now, we'll just remove the user from the NewUser table
        new_user = session.query(NewUser).filter_by(user_id=user_id, chat_id=chat_id).first()
        if new_user:
            session.delete(new_user)
            session.commit()
            logger.info(f"User {user_id} has been approved and removed from NewUser table")

    logger.info("Bot setup complete")
    return client
