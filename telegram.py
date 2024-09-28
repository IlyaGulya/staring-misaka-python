import logging
from sqlalchemy.orm import Session
from telethon import TelegramClient, events

from db import NewUser, PendingBanRequest
from env import TRACKING_CHAT_IDS, SESSION_PATH, API_ID, API_HASH, ADMIN_ID
from llm import Llm

# Configure logging
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def create_bot(session: Session, llm: Llm) -> TelegramClient:
    client = TelegramClient(SESSION_PATH, API_ID, API_HASH)
    logger.info("Creating Telegram bot client")

    @client.on(events.ChatAction(chats=TRACKING_CHAT_IDS))
    async def chat_action_handler(event):
        logger.debug(f"Chat action event received: {event}")
        if event.chat_id not in TRACKING_CHAT_IDS:
            logger.debug(f"Ignoring event from non-tracked chat: {event.chat_id}")
            return
        # Check if a user has joined or been added to the group
        if event.user_joined or event.user_added:
            if event.user:
                logger.info(f"User {event.user.id} joined or was added to the group {event.chat_id}")
                # Add new user to the database
                new_user = NewUser(user_id=event.user.id)
                session.add(new_user)
                session.commit()
                logger.debug(f"Added user {event.user.id} to NewUser table")
            else:
                logger.warning("User joined event received, but user object is None")

    @client.on(events.NewMessage(chats=TRACKING_CHAT_IDS))
    async def message_handler(event):
        logger.debug(f"New message event received: {event}")
        sender = await event.get_sender()
        logger.debug(f"Message sender: {sender.id}")
        # Check if sender is in the new_users table
        new_user = session.query(NewUser).filter_by(user_id=sender.id).first()
        if new_user:
            logger.info(f"Processing first message from new user {sender.id}")
            # Process the first message
            message_text = event.raw_text
            logger.debug(f"Message text: {message_text}")
            # Send to Claude API to check if spam
            is_spam = await llm.is_spam(message_text)
            logger.info(f"Spam check result for user {sender.id}: {is_spam}")
            if is_spam:
                # Notify admin
                await notify_admin(sender, message_text, event)
            # Remove user from new_users table
            session.delete(new_user)
            session.commit()
            logger.debug(f"Removed user {sender.id} from NewUser table")
        else:
            logger.debug(f"Message from existing user {sender.id}, ignoring")

    async def notify_admin(sender, message_text, event):
        logger.info(f"Notifying admin about potential spam from user {sender.id}")
        # Send a message to the admin
        admin_message = (
            f"User {sender.first_name} ({sender.id}) sent a message:\n\n"
            f"{message_text}\n\nShould I ban this user? Reply 'yes' to ban."
        )
        sent_message = await client.send_message(ADMIN_ID, admin_message)
        logger.debug(f"Sent admin notification message with ID: {sent_message.id}")
        # Store the pending request in the database
        pending_request = PendingBanRequest(
            admin_message_id=sent_message.id,
            sender_id=sender.id,
            original_chat_id=event.chat_id,
            original_message_id=event.id
        )
        session.add(pending_request)
        session.commit()
        logger.debug(f"Added pending ban request for user {sender.id} to database")

    @client.on(events.NewMessage(chats=[ADMIN_ID], from_users=[ADMIN_ID]))
    async def admin_reply_handler(event):
        logger.debug(f"Received message from admin: {event}")
        if event.reply_to_msg_id:
            logger.debug(f"Admin message is a reply to message ID: {event.reply_to_msg_id}")
            # Check if this is a reply to our pending ban request
            pending_request = session.query(PendingBanRequest).filter_by(
                admin_message_id=event.reply_to_msg_id
            ).first()
            if pending_request:
                logger.info(f"Processing admin reply for pending ban request: {pending_request.sender_id}")
                if event.raw_text.strip().lower() == 'yes':
                    logger.info(f"Admin approved ban for user {pending_request.sender_id}")
                    # Reply to the original message with '/sban'
                    await client.send_message(
                        entity=pending_request.original_chat_id,
                        message='/sban',
                        reply_to=pending_request.original_message_id
                    )
                    # Notify admin that the user has been banned
                    await client.send_message(
                        ADMIN_ID, f"User {pending_request.sender_id} has been banned."
                    )
                else:
                    logger.info(f"Admin did not approve ban for user {pending_request.sender_id}")
                    await client.send_message(
                        ADMIN_ID, f"No action taken against user {pending_request.sender_id}."
                    )
                # Remove the pending request from the database
                session.delete(pending_request)
                session.commit()
                logger.debug(f"Removed pending ban request for user {pending_request.sender_id} from database")
            else:
                logger.debug("Admin reply does not correspond to a pending ban request")
        else:
            logger.debug("Processing non-reply message from admin")
            is_spam = await llm.is_spam(event.raw_text)
            await client.send_message(ADMIN_ID, f"Is spam: {is_spam}")

    logger.info("Bot setup complete")
    return client
