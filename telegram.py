import logging
from datetime import datetime, UTC

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from telethon import TelegramClient, events
from telethon.tl.types import UpdateChannelParticipant

from db import NewUser, PendingBanRequest, BannedUser, AdminSettings, ApprovedUser, MessageQueue
from llm import Llm
from userbot import UserBot

# Configure logging
logger = logging.getLogger(__name__)


def create_bot(session: Session, llm: Llm, userbot: UserBot, config, queue_processor=None) -> TelegramClient:
    client = TelegramClient(config.bot_session_path, config.api_id, config.api_hash)
    logger.info("Creating Telegram bot client")

    @client.on(events.ChatAction(chats=config.tracking_chat_ids))
    async def chat_action_handler(event):
        logger.info(f"Chat action event received: {event}")
        if event.chat_id not in config.tracking_chat_ids:
            logger.info(f"Ignoring event from non-tracked chat: {event.chat_id}")
            return
        # Check if a user has joined or been added to the group
        if (event.user_added or event.user_joined) and isinstance(event.original_update, UpdateChannelParticipant):
            user_id = event.user.id
            logger.info(f"User {user_id} was added to the group {event.chat_id}")

            # Check if user is pre-approved
            approved_user = session.query(ApprovedUser).filter_by(user_id=user_id, chat_id=event.chat_id).first()
            if approved_user:
                logger.info(f"User {user_id} is pre-approved, skipping monitoring")
                return

            # Check if the user already exists in the new_users table
            existing_user = session.query(NewUser).filter_by(user_id=user_id, chat_id=event.chat_id).first()

            if existing_user:
                logger.info(f"User {user_id} already exists in NewUser table. Updating join time.")
                existing_user.join_time = datetime.now(UTC)
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

    @client.on(events.NewMessage(chats=config.tracking_chat_ids))
    async def message_handler(event):
        logger.info(f"New message event received: {event}")
        sender = await event.get_sender()
        logger.info(f"Message sender: {sender.id}")

        # Check if sender is pre-approved
        approved_user = session.query(ApprovedUser).filter_by(user_id=sender.id, chat_id=event.chat_id).first()
        if approved_user:
            logger.info(f"Message from pre-approved user {sender.id}, ignoring")
            return

        # Check if sender is in the new_users table
        new_user = session.query(NewUser).filter_by(user_id=sender.id, chat_id=event.chat_id).first()
        if new_user:
            logger.info(f"Processing message from new user {sender.id}")
            message_text = event.raw_text
            logger.info(f"Message text: {message_text}")
            
            # Add message to queue for processing instead of direct spam check
            if queue_processor:
                queue_processor.add_message_to_queue(
                    user_id=sender.id,
                    chat_id=event.chat_id,
                    message_id=event.id,
                    message_text=message_text
                )
                logger.info(f"Added message from user {sender.id} to processing queue")
            else:
                logger.warning("Queue processor not available, falling back to direct spam check")
                # Fallback to direct spam check if queue processor is not available
                try:
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
                except Exception as e:
                    logger.error(f"Error in fallback spam check for user {sender.id}: {str(e)}")
        else:
            logger.info(f"Message from existing user {sender.id}, ignoring")

    @client.on(events.NewMessage(chats=config.tracking_chat_ids, pattern=r'^/approve'))
    async def approve_command_handler(event):
        # Only allow admin to use this command
        if event.sender_id != config.admin_id:
            logger.info(f"Non-admin user {event.sender_id} tried to use /approve command")
            await event.reply("Don't touch me, baka!")
            return
            
        logger.info(f"Admin {event.sender_id} used /approve command in group chat {event.chat_id}")
        
        parts = event.raw_text.split()
        if len(parts) < 2:
            await event.reply("Usage: /approve <@username or user_id>")
            return
        
        user_identifier = parts[1]
        approved_user = await approve_user(user_identifier, event.chat_id)
            
        if approved_user:
            await event.reply(f"User {approved_user['name']} (ID: {approved_user['id']}) has been approved and removed from monitoring.")
        else:
            await event.reply("User not found in monitoring list or error occurred.")

    @client.on(events.NewMessage(chats=config.tracking_chat_ids, pattern=r'^/unapprove'))
    async def unapprove_command_handler(event):
        # Only allow admin to use this command
        if event.sender_id != config.admin_id:
            logger.info(f"Non-admin user {event.sender_id} tried to use /unapprove command")
            await event.reply("Don't touch me, baka!")
            return

        logger.info(f"Admin {event.sender_id} used /unapprove command in group chat {event.chat_id}")

        parts = event.raw_text.split()
        if len(parts) < 2:
            await event.reply("Usage: /unapprove <@username or user_id>")
            return

        user_identifier = parts[1]
        removed_user = await remove_approval(user_identifier, event.chat_id)

        if removed_user:
            await event.reply(f"User {removed_user['name']} (ID: {removed_user['id']}) approval has been removed. They will be monitored for spam again.")
        else:
            await event.reply("User not found in approved list or error occurred.")

    @client.on(events.NewMessage(chats=config.tracking_chat_ids, pattern=r'^/(toggle_approval|status|queue_status|retry_failed|clear_completed)'))
    async def admin_commands_group_handler(event):
        # Only allow admin to use these commands in group chats
        if event.sender_id != config.admin_id:
            logger.info(f"Non-admin user {event.sender_id} tried to use admin command: {event.raw_text}")
            await event.reply("Don't touch me, baka!")
            return

        # Admin is using command in group - redirect to private chat
        await event.reply("Please use admin commands in private chat with me.")

    async def notify_admin(sender, message_text, event):
        logger.info(f"Notifying admin about potential spam from user {sender.id}")
        # Send a message to the admin
        admin_message = (
            f"User {sender.first_name} ({sender.id}) sent a message in chat {event.chat_id}:\n\n"
            f"{message_text}\n\nShould I ban this user? Reply 'yes' to ban."
        )
        sent_message = await client.send_message(config.admin_id, admin_message)
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
        await client.send_message(config.admin_id, admin_message)

        return banned_user

    @client.on(events.NewMessage(chats=[config.admin_id], from_users=[config.admin_id]))
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
            elif command == '/queue_status':
                if queue_processor:
                    status = queue_processor.get_queue_status()
                    status_msg = (
                        f"Queue Status:\n"
                        f"• Pending: {status['pending']}\n"
                        f"• Processing: {status['processing']}\n"
                        f"• Failed: {status['failed']}\n"
                        f"• Completed: {status['completed']}\n"
                        f"• Total: {status['total']}"
                    )
                    await event.reply(status_msg)
                else:
                    await event.reply("Queue processor not available")
            elif command == '/retry_failed':
                if queue_processor:
                    count = queue_processor.retry_failed_messages()
                    await event.reply(f"Reset {count} failed messages to pending status")
                else:
                    await event.reply("Queue processor not available")
            elif command == '/clear_completed':
                if queue_processor:
                    count = queue_processor.clear_completed_messages()
                    await event.reply(f"Cleared {count} completed messages from queue")
                else:
                    await event.reply("Queue processor not available")
            else:
                await event.reply("Unknown command. Available commands: /toggle_approval, /status, /queue_status, /retry_failed, /clear_completed")
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
                        config.admin_id, f"No action taken against user {pending_request.sender_id}."
                    )
                    # Remove the pending request from the database
                    session.delete(pending_request)
                    session.commit()
            else:
                logger.info("Admin reply does not correspond to a pending ban request")
        else:
            # Existing code for processing non-reply messages from admin
            is_spam = await llm.is_spam(event.raw_text)
            await client.send_message(config.admin_id, f"Is spam: {is_spam}")

    async def approve_user(user_identifier: str, target_chat_id: int):
        try:
            user_id = None
            user_name = None
            
            # Parse user identifier - could be @username or user_id
            if user_identifier.startswith('@'):
                # Username format
                username = user_identifier[1:]  # Remove @ symbol
                try:
                    user = await client.get_entity(username)
                    user_id = user.id
                    user_name = user.username if user.username else user.first_name
                except Exception as e:
                    logger.warning(f"Could not fetch user entity by username {username}: {str(e)}")
                    # Try to find user in database by searching for username in stored data
                    # This is a fallback - we'll search by user_id if possible from database
                    logger.info(f"Attempting database fallback for username {username}")
                    return None  # Username fallback is complex, require user_id for approval
            else:
                # Assume it's a user ID
                try:
                    user_id = int(user_identifier)
                    try:
                        user = await client.get_entity(user_id)
                        user_name = user.username if user.username else user.first_name
                    except Exception as e:
                        logger.warning(f"Could not fetch user entity by ID {user_id}: {str(e)}")
                        # Continue with approval using just the user_id - entity fetching failed but we can still approve
                        user_name = f"User_{user_id}"  # Fallback name
                        logger.info(f"Using fallback name for user {user_id}")
                except ValueError:
                    logger.error(f"Invalid user ID format: {user_identifier}")
                    return None

            if user_id is None:
                return None

            # Remove user from NewUser table for the specific chat
            new_user = session.query(NewUser).filter_by(user_id=user_id, chat_id=target_chat_id).first()
            removed_count = 0
            if new_user:
                session.delete(new_user)
                removed_count = 1
                logger.info(f"Removed user {user_id} from monitoring in chat {target_chat_id}")

            # Add user to approved users list for the specific chat only
            existing_approval = session.query(ApprovedUser).filter_by(user_id=user_id, chat_id=target_chat_id).first()
            approved_count = 0
            if not existing_approval:
                approved_user = ApprovedUser(user_id=user_id, chat_id=target_chat_id)
                session.add(approved_user)
                approved_count = 1
                logger.info(f"Added user {user_id} to approved list for chat {target_chat_id}")

            if removed_count > 0 or approved_count > 0:
                session.commit()
                if removed_count > 0:
                    logger.info(f"User {user_id} removed from monitoring in chat {target_chat_id}")
                if approved_count > 0:
                    logger.info(f"User {user_id} added to approved list for chat {target_chat_id}")
                return {"id": user_id, "name": user_name}
            else:
                logger.info(f"User {user_id} was already approved for chat {target_chat_id}")
                return {"id": user_id, "name": user_name}

        except Exception as e:
            logger.error(f"Error approving user {user_identifier}: {str(e)}")
            return None

    async def remove_approval(user_identifier: str, target_chat_id: int):
        try:
            user_id = None
            user_name = None

            # Parse user identifier - could be @username or user_id
            if user_identifier.startswith('@'):
                # Username format
                username = user_identifier[1:]  # Remove @ symbol
                try:
                    user = await client.get_entity(username)
                    user_id = user.id
                    user_name = user.username if user.username else user.first_name
                except Exception as e:
                    logger.warning(f"Could not fetch user entity by username {username}: {str(e)}")
                    logger.info(f"Attempting database fallback for username {username}")
                    return None  # Username fallback is complex, require user_id for removal
            else:
                # Assume it's a user ID
                try:
                    user_id = int(user_identifier)
                    try:
                        user = await client.get_entity(user_id)
                        user_name = user.username if user.username else user.first_name
                    except Exception as e:
                        logger.warning(f"Could not fetch user entity by ID {user_id}: {str(e)}")
                        # Continue with removal using just the user_id - entity fetching failed but we can still remove
                        user_name = f"User_{user_id}"  # Fallback name
                        logger.info(f"Using fallback name for user {user_id}")
                except ValueError:
                    logger.error(f"Invalid user ID format: {user_identifier}")
                    return None

            if user_id is None:
                return None

            # Remove user from approved users list for the specific chat
            approved_user = session.query(ApprovedUser).filter_by(user_id=user_id, chat_id=target_chat_id).first()
            if approved_user:
                session.delete(approved_user)
                session.commit()
                logger.info(f"Removed user {user_id} from approved list for chat {target_chat_id}")
                return {"id": user_id, "name": user_name}
            else:
                logger.info(f"User {user_id} was not in approved list for chat {target_chat_id}")
                return None

        except Exception as e:
            logger.error(f"Error removing approval for user {user_identifier}: {str(e)}")
            return None

    async def get_user_name(user_id):
        try:
            user = await client.get_entity(user_id)
            return user.username if user.username else user.first_name
        except Exception as e:
            logger.error(f"Error fetching user name for user_id {user_id}: {str(e)}")
            return None

    async def check_user_approval(user_id: int, chat_id: int):
        # Auto-approve users who pass spam checks by removing from monitoring and adding to approved list
        new_user = session.query(NewUser).filter_by(user_id=user_id, chat_id=chat_id).first()
        if new_user:
            # Remove from monitoring
            session.delete(new_user)
            logger.info(f"User {user_id} removed from monitoring in chat {chat_id}")
            
            # Add to approved users list
            existing_approval = session.query(ApprovedUser).filter_by(user_id=user_id, chat_id=chat_id).first()
            if not existing_approval:
                approved_user = ApprovedUser(user_id=user_id, chat_id=chat_id)
                session.add(approved_user)
                logger.info(f"User {user_id} auto-approved and added to approved list for chat {chat_id}")
            
            session.commit()
            logger.info(f"User {user_id} has been automatically approved after passing spam check")

    logger.info("Bot setup complete")
    # Expose handlers for tests to call directly without poking into Telethon internals.
    # This is inert in production and simplifies unit/integration tests.
    client._handlers = {
        "chat_action_handler": chat_action_handler,
        "message_handler": message_handler,
        "approve_command_handler": approve_command_handler,
        "unapprove_command_handler": unapprove_command_handler,
        "admin_commands_group_handler": admin_commands_group_handler,
        "admin_reply_handler": admin_reply_handler,
        # expose helpers used by handlers when convenient to assert on behavior
        "approve_user": approve_user,
        "remove_approval": remove_approval,
        "get_user_name": get_user_name,
        "check_user_approval": check_user_approval,
    }
    return client
