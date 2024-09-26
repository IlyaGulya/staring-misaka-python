from sqlalchemy.orm import Session
from telethon import TelegramClient, events

from db import NewUser, PendingBanRequest
from env import TRACKING_CHAT_IDS, SESSION_PATH, API_ID, API_HASH, ADMIN_ID
from llm import Llm


def create_bot(session: Session, llm: Llm) -> TelegramClient:
    client = TelegramClient(SESSION_PATH, API_ID, API_HASH)

    @client.on(events.ChatAction(chats=TRACKING_CHAT_IDS))
    async def chat_action_handler(event):
        if event.chat_id not in TRACKING_CHAT_IDS:
            return
        # Check if a user has joined or been added to the group
        if event.user_joined or event.user_added:
            if event.user:
                # Add new user to the database
                new_user = NewUser(user_id=event.user.id)
                session.add(new_user)
                session.commit()
                print(f"User {event.user.id} joined the group.")

    @client.on(events.NewMessage(chats=TRACKING_CHAT_IDS))
    async def message_handler(event):
        sender = await event.get_sender()
        # Check if sender is in the new_users table
        new_user = session.query(NewUser).filter_by(user_id=sender.id).first()
        if new_user:
            # Process the first message
            message_text = event.raw_text
            print(f"Processing first message from new user {sender.id}: {message_text}")
            # Send to Claude API to check if spam
            is_spam = await llm.is_spam(message_text)
            if is_spam:
                # Notify admin
                await notify_admin(sender, message_text, event)
            # Remove user from new_users table
            session.delete(new_user)
            session.commit()

    async def notify_admin(sender, message_text, event):
        # Send a message to the admin
        admin_message = (
            f"User {sender.first_name} ({sender.id}) sent a message:\n\n"
            f"{message_text}\n\nShould I ban this user? Reply 'yes' to ban."
        )
        sent_message = await client.send_message(ADMIN_ID, admin_message)
        # Store the pending request in the database
        pending_request = PendingBanRequest(
            admin_message_id=sent_message.id,
            sender_id=sender.id,
            original_chat_id=event.chat_id,
            original_message_id=event.id
        )
        session.add(pending_request)
        session.commit()

    @client.on(events.NewMessage(chats=[ADMIN_ID], from_users=[ADMIN_ID]))
    async def admin_reply_handler(event):
        if event.reply_to_msg_id:
            # Check if this is a reply to our pending ban request
            pending_request = session.query(PendingBanRequest).filter_by(
                admin_message_id=event.reply_to_msg_id
            ).first()
            if pending_request:
                if event.raw_text.strip().lower() == 'yes':
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
                    await client.send_message(
                        ADMIN_ID, f"No action taken against user {pending_request.sender_id}."
                    )
                # Remove the pending request from the database
                session.delete(pending_request)
                session.commit()
        else:
            is_spam = await llm.is_spam(event.raw_text)
            await client.send_message(ADMIN_ID, f"Is spam: {is_spam}")

    return client