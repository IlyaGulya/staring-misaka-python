import asyncio
import logging
from datetime import datetime, timedelta, UTC
from typing import Optional

from sqlalchemy.orm import Session, sessionmaker
from telethon import TelegramClient

from db import MessageQueue, NewUser, PendingBanRequest, AdminSettings, ApprovedUser, BannedUser, GroupSettings
from llm import Llm
from moderation import ban_user, purge_user_messages

logger = logging.getLogger(__name__)


class QueueProcessor:
    def __init__(
        self,
        session_factory: sessionmaker,
        llm: Llm,
        telegram_client: TelegramClient,
        config,
        *,
        processing_delay: float = 1.0,
        max_concurrent_jobs: int = 3,
    ):
        self.session_factory = session_factory
        self.llm = llm
        self.telegram_client = telegram_client
        self.config = config
        self.running = False
        self.processing_delay = processing_delay  # Base delay between processing iterations
        self.max_concurrent_jobs = max_concurrent_jobs  # Max concurrent spam checks

    def _log_dest(self, chat_id: int) -> int:
        """Resolve log channel for a group, fallback to admin_id."""
        return self.config.log_channel_map.get(chat_id, self.config.admin_id)

    async def _log(self, chat_id: int, text: str):
        try:
            await self.telegram_client.send_message(self._log_dest(chat_id), text)
        except Exception as e:
            logger.warning(f"Failed to log action for chat {chat_id}: {e}")

    def _get_session(self) -> Session:
        """Create a new session for database operations"""
        return self.session_factory()
        
    async def start(self):
        self.running = True
        logger.info("[QUEUE] Processor started")

        while self.running:
            try:
                await self._process_pending_messages()
                await asyncio.sleep(self.processing_delay)
            except Exception as e:
                logger.error(f"Error in queue processor main loop: {str(e)}")
                await asyncio.sleep(5.0)  # Wait longer on error

    def stop(self):
        self.running = False
        logger.info("[QUEUE] Processor stopped")
        
    async def _process_pending_messages(self):
        now = datetime.now(UTC)

        # Use a separate session for message selection to avoid lock conflicts
        with self._get_session() as selection_session:
            # Get messages ready for processing (pending or failed with retry time reached)
            pending_messages = selection_session.query(MessageQueue).filter(
                ((MessageQueue.status == 'pending') |
                 ((MessageQueue.status == 'failed') & (MessageQueue.next_retry_at <= now))),
                MessageQueue.retry_count < MessageQueue.max_retries
            ).order_by(MessageQueue.created_at).limit(self.max_concurrent_jobs).all()

            # For SQLite, we need to manually check and update status to avoid conflicts
            locked_messages = []
            for msg in pending_messages:
                # Try to atomically claim the message by updating its status
                updated_rows = selection_session.query(MessageQueue).filter_by(
                    id=msg.id,
                    status=msg.status  # Only update if status hasn't changed
                ).update({'status': 'claimed'})
                selection_session.commit()

                if updated_rows > 0:
                    locked_messages.append(msg)

            pending_messages = locked_messages

            # Get the message IDs to process
            message_ids = [msg.id for msg in pending_messages]

        if not message_ids:
            return

        # Process each message in its own session to avoid conflicts
        tasks = []
        for message_id in message_ids:
            task = asyncio.create_task(self._process_message_by_id(message_id))
            tasks.append(task)

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
            
    async def _process_message_by_id(self, message_id: int):
        """Process a message by its ID, using a fresh session"""
        # Fetch message data first, then close session before long operations
        with self._get_session() as process_session:
            # Fetch the message
            queue_item = process_session.query(MessageQueue).filter_by(id=message_id).first()
            if not queue_item:
                logger.debug(f"Message {message_id} not found or already processed")
                return

            # Check if already being processed (should be 'claimed' from selection phase)
            if queue_item.status not in ['claimed', 'pending', 'failed']:
                logger.debug(f"Message {message_id} already being processed or completed")
                return

            # Extract data we need before closing session
            user_id = queue_item.user_id
            chat_id = queue_item.chat_id
            message_text = queue_item.message_text

        # Process message WITHOUT holding database session open
        await self._process_message(message_id, user_id, chat_id, message_text)
    
    async def _process_message(self, message_id: int, user_id: int, chat_id: int, message_text: str):
        """Process a message from the queue. Creates its own sessions to avoid holding locks."""
        logger.debug(f"Processing queue item {message_id} for user {user_id}")

        try:
            # Mark as processing (quick database operation)
            with self._get_session() as session:
                queue_item = session.query(MessageQueue).filter_by(id=message_id).first()
                if not queue_item:
                    logger.debug(f"Message {message_id} not found")
                    return

                queue_item.status = 'processing'
                queue_item.retry_count += 1
                session.commit()

            # Check conditions (quick database reads)
            with self._get_session() as session:
                # Check if bot is enabled for this chat
                group_settings = session.query(GroupSettings).filter_by(chat_id=chat_id).first()
                if group_settings and not group_settings.enabled:
                    logger.debug(f"Bot disabled for chat {chat_id}, skipping queue item")
                    # Update status and exit
                    with self._get_session() as update_session:
                        queue_item = update_session.query(MessageQueue).filter_by(id=message_id).first()
                        if queue_item:
                            queue_item.status = 'completed'
                            queue_item.processed_at = datetime.now(UTC)
                            queue_item.error_message = "Bot disabled for this chat"
                            update_session.commit()
                    return

                # Check if user is still being monitored
                new_user = session.query(NewUser).filter_by(user_id=user_id, chat_id=chat_id).first()
                if not new_user:
                    logger.debug(f"User {user_id} no longer monitored, skipping")
                    with self._get_session() as update_session:
                        queue_item = update_session.query(MessageQueue).filter_by(id=message_id).first()
                        if queue_item:
                            queue_item.status = 'completed'
                            queue_item.processed_at = datetime.now(UTC)
                            update_session.commit()
                    return

                # Check if user is pre-approved
                approved_user = session.query(ApprovedUser).filter_by(user_id=user_id, chat_id=chat_id).first()
                if approved_user:
                    logger.debug(f"User {user_id} is pre-approved, skipping")
                    with self._get_session() as update_session:
                        queue_item = update_session.query(MessageQueue).filter_by(id=message_id).first()
                        if queue_item:
                            queue_item.status = 'completed'
                            queue_item.processed_at = datetime.now(UTC)
                            update_session.commit()
                    return

            # Perform spam check WITHOUT holding database session
            logger.debug(f"Running spam check for user {user_id}")
            is_spam = await self.llm.is_spam(message_text)

            # Update queue item with result
            with self._get_session() as session:
                queue_item = session.query(MessageQueue).filter_by(id=message_id).first()
                if queue_item:
                    queue_item.spam_result = is_spam
                    queue_item.status = 'completed'
                    queue_item.processed_at = datetime.now(UTC)
                    queue_item.error_message = None
                    session.commit()

            if is_spam:
                logger.info(f"[SPAM] Detected from user_id={user_id} chat_id={chat_id}")
            else:
                logger.debug(f"Not spam: user {user_id}")

            # Process the spam result
            await self._handle_spam_result(message_id, user_id, chat_id, message_text, is_spam)

        except Exception as e:
            error_msg = str(e)
            logger.error(f"Error processing queue item {message_id}: {error_msg}")

            # Update queue item with error
            with self._get_session() as session:
                queue_item = session.query(MessageQueue).filter_by(id=message_id).first()
                if queue_item:
                    queue_item.error_message = error_msg
                    queue_item.status = 'failed'

                    # Calculate next retry time with exponential backoff
                    backoff_seconds = min(300, 30 * (2 ** (queue_item.retry_count - 1)))  # Max 5 minutes
                    queue_item.next_retry_at = datetime.now(UTC) + timedelta(seconds=backoff_seconds)

                    logger.warning(f"Will retry message {message_id} in {backoff_seconds}s (attempt {queue_item.retry_count}/{queue_item.max_retries})")
                    session.commit()
            
    async def _handle_spam_result(self, message_id: int, user_id: int, chat_id: int, message_text: str, is_spam: bool):
        """Handle the result of spam detection. Creates its own sessions."""
        if is_spam:
            # Check admin settings
            with self._get_session() as session:
                admin_settings = session.query(AdminSettings).first()
                require_approval = admin_settings.require_approval if admin_settings else False

            if require_approval:
                # Notify admin
                await self._notify_admin(message_id, user_id, chat_id, message_text)
                await self._log(chat_id, f"🚩 Potential spam by `{user_id}` queued for admin review.")
            else:
                # Automatically ban the user
                await self._process_ban(
                    user_id=user_id,
                    chat_id=chat_id,
                    message_id=message_id,
                    message_text=message_text,
                    is_automatic=True
                )
        else:
            # If not spam, approve the user
            await self._auto_approve_user(user_id, chat_id)
            await self._log(chat_id, f"✅ User `{user_id}` auto-approved (message passed spam check).")
            
    async def _notify_admin(self, message_id: int, user_id: int, chat_id: int, message_text: str):
        """Notify admin about potential spam. Creates its own session."""
        logger.info(f"[SPAM] Requesting admin approval for user_id={user_id} chat_id={chat_id}")

        try:
            # Get user name
            user_name = await self._get_user_name(user_id)

            admin_message = (
                f"User {user_name} ({user_id}) sent a message in chat {chat_id}:\n\n"
                f"{message_text}\n\nShould I ban this user? Reply 'yes' to ban."
            )
            sent_message = await self.telegram_client.send_message(self.config.admin_id, admin_message)
            logger.debug(f"Admin notification sent with message ID: {sent_message.id}")

            # Store the pending request in the database
            with self._get_session() as session:
                pending_request = PendingBanRequest(
                    admin_message_id=sent_message.id,
                    sender_id=user_id,
                    original_chat_id=chat_id,
                    original_message_id=message_id,
                    message_text=message_text,
                    created_at=datetime.now(UTC)
                )
                session.add(pending_request)
                session.commit()
                logger.debug(f"Pending ban request stored for user {user_id}")

        except Exception as e:
            logger.error(f"Error notifying admin about user {user_id}: {str(e)}")
            raise
            
    async def _process_ban(self, user_id: int, chat_id: int, message_id: int, message_text: str, is_automatic: bool):
        """Process a ban for a user. Creates its own session."""
        ban_type = "automatic" if is_automatic else "manual"
        logger.info(f"[BAN] user_id={user_id} chat_id={chat_id} type={ban_type}")

        errors = []

        # Step 1: Ban via bot admin rights (CRITICAL - must succeed)
        try:
            await self._ban_user_via_bot(chat_id, user_id)
        except Exception as e:
            logger.error(f"Error banning user {user_id}: {str(e)}")
            errors.append(f"ban: {str(e)}")

        # Step 2: Purge recent messages from the user (OPTIONAL - can fail)
        try:
            purge_n = self.config.default_purge_count
            await self._purge_user_messages(chat_id, user_id, purge_n)
        except Exception as e:
            logger.warning(f"Error purging messages for user {user_id}: {str(e)}")
            errors.append(f"purge: {str(e)}")

        # Step 3: Store the ban information in the database (always do this)
        user_name = await self._get_user_name(user_id)
        with self._get_session() as session:
            banned_user = BannedUser(
                user_id=user_id,
                user_name=user_name,
                chat_id=chat_id,
                message_text=message_text,
                banned_at=datetime.now(UTC)
            )
            session.add(banned_user)

            # Remove the user from NewUser table if they're still there
            new_user = session.query(NewUser).filter_by(user_id=user_id, chat_id=chat_id).first()
            if new_user:
                session.delete(new_user)

            session.commit()
            logger.debug(f"Ban information stored for user {user_id}")

        # Step 4: Notify admin about the ban (OPTIONAL - can fail)
        try:
            status_msg = ""
            if errors:
                status_msg = f"\n⚠️ Warnings: {', '.join(errors)}"

            admin_message = (
                f"User {user_id} has been {'automatically ' if is_automatic else ''}banned "
                f"{'due to spam detection' if is_automatic else 'as per admin approval'}.{status_msg}"
            )
            await self.telegram_client.send_message(self.config.admin_id, admin_message)
        except Exception as e:
            logger.warning(f"Failed to notify admin about ban: {e}")
            errors.append(f"admin_notification: {str(e)}")

        # Step 5: Log to channel (OPTIONAL - can fail)
        try:
            await self._log(chat_id, f"🔨 Banned `{user_id}` and purged last {self.config.default_purge_count} messages.")
        except Exception as e:
            logger.warning(f"Failed to log ban action: {e}")
            errors.append(f"logging: {str(e)}")

        # Only raise if the actual ban failed (not purge or logging)
        if any("ban:" in err for err in errors):
            raise Exception(f"Ban failed: {errors}")

    async def _ban_user_via_bot(self, chat_id: int, user_id: int):
        """Apply ban using bot's admin rights."""
        await ban_user(self.telegram_client, chat_id, user_id)

    async def _purge_user_messages(self, chat_id: int, user_id: int, count: int):
        """Delete recent N messages from the user in the chat."""
        await purge_user_messages(self.telegram_client, chat_id, user_id, count)
            
    async def _auto_approve_user(self, user_id: int, chat_id: int):
        """Auto-approve a user who passed spam check. Creates its own session."""
        logger.debug(f"Auto-approving user {user_id} in chat {chat_id}")

        try:
            with self._get_session() as session:
                # Remove from monitoring
                new_user = session.query(NewUser).filter_by(user_id=user_id, chat_id=chat_id).first()
                if new_user:
                    session.delete(new_user)
                    logger.debug(f"User {user_id} removed from monitoring")

                # Add to approved users list
                existing_approval = session.query(ApprovedUser).filter_by(user_id=user_id, chat_id=chat_id).first()
                if not existing_approval:
                    approved_user = ApprovedUser(user_id=user_id, chat_id=chat_id, approved_at=datetime.now(UTC))
                    session.add(approved_user)
                    logger.debug(f"User {user_id} added to approved list")

                session.commit()
                logger.info(f"[AUTO-APPROVE] user_id={user_id} chat_id={chat_id}")

        except Exception as e:
            logger.error(f"Error auto-approving user {user_id}: {str(e)}")
            raise
            
    async def _get_user_name(self, user_id: int) -> Optional[str]:
        try:
            user = await self.telegram_client.get_entity(user_id)
            return user.username if user.username else user.first_name
        except Exception as e:
            if "disconnected" in str(e).lower():
                logger.warning(f"Telegram client disconnected while fetching user name for user_id {user_id}, using fallback")
            else:
                logger.error(f"Error fetching user name for user_id {user_id}: {str(e)}")
            return f"User_{user_id}"
            
    def add_message_to_queue(self, user_id: int, chat_id: int, message_id: int, message_text: str) -> MessageQueue:
        """Add a message to the processing queue using UPSERT to handle duplicates.

        Uses SQLite's INSERT OR IGNORE to atomically handle duplicate messages.
        """
        logger.debug(f"Adding message to queue for user {user_id}")

        from sqlalchemy.dialects.sqlite import insert
        from sqlalchemy.exc import OperationalError
        import time

        payload = {
            'user_id': user_id,
            'chat_id': chat_id,
            'message_id': message_id,
            'message_text': message_text,
            'status': 'pending',
            'created_at': datetime.now(UTC)
        }

        # Retry with exponential backoff for database locks
        for attempt, delay in enumerate((0.05, 0.1, 0.2, 0.4), start=1):
            try:
                with self._get_session() as session:
                    # Use INSERT OR IGNORE to handle duplicates atomically
                    stmt = insert(MessageQueue).values(**payload).on_conflict_do_nothing(
                        index_elements=['user_id', 'chat_id', 'message_id']
                    )
                    session.execute(stmt)
                    session.commit()

                    # Fetch the row to return (either newly inserted or existing)
                    result = session.query(MessageQueue).filter_by(
                        user_id=user_id,
                        chat_id=chat_id,
                        message_id=message_id
                    ).first()

                    if result:
                        logger.debug(f"Message queued with ID: {result.id}")
                        return result
                    else:
                        logger.debug(f"Message not found after insert for user {user_id}")
                        # Continue to retry
                        time.sleep(delay)
                        continue

            except OperationalError as e:
                if 'locked' in str(e).lower() and attempt < 4:
                    logger.warning(f"Database locked on attempt {attempt}, retrying in {delay}s...")
                    time.sleep(delay)
                else:
                    logger.error(f"Failed to add message to queue after {attempt} attempts")
                    raise
            except Exception as e:
                logger.error(f"Unexpected error adding message to queue: {str(e)}")
                raise

        # If we exhausted all retries
        raise OperationalError("database is locked", None, None)
        
    def get_queue_status(self):
        """Get current queue status for admin"""
        with self._get_session() as session:
            try:
                # Use or 0 to handle potential None values from count() in race conditions
                pending_count = session.query(MessageQueue).filter_by(status='pending').count() or 0
                processing_count = session.query(MessageQueue).filter_by(status='processing').count() or 0
                failed_count = session.query(MessageQueue).filter_by(status='failed').count() or 0
                completed_count = session.query(MessageQueue).filter_by(status='completed').count() or 0

                return {
                    'pending': pending_count,
                    'processing': processing_count,
                    'failed': failed_count,
                    'completed': completed_count,
                    'total': pending_count + processing_count + failed_count + completed_count
                }
            except Exception:
                # If there's any database error, return zero counts
                return {
                    'pending': 0,
                    'processing': 0,
                    'failed': 0,
                    'completed': 0,
                    'total': 0
                }
        
    def retry_failed_messages(self) -> int:
        """Retry all failed messages by resetting their status"""
        with self._get_session() as session:
            try:
                failed_messages = session.query(MessageQueue).filter_by(status='failed').all()
                count = 0

                for message in failed_messages:
                    if message.retry_count < message.max_retries:
                        message.status = 'pending'
                        message.next_retry_at = None
                        message.error_message = None
                        count += 1

                session.commit()
                if count > 0:
                    logger.info(f"[QUEUE] Reset {count} failed messages to pending")
                return count
            except Exception:
                # If there's any database error, return 0
                return 0
        
    def clear_completed_messages(self, older_than_hours: int = 24) -> int:
        """Clear completed messages older than specified hours"""
        cutoff_time = datetime.now(UTC) - timedelta(hours=older_than_hours)

        with self._get_session() as session:
            deleted_count = session.query(MessageQueue).filter(
                MessageQueue.status == 'completed',
                MessageQueue.processed_at < cutoff_time
            ).delete()

            session.commit()
            if deleted_count > 0:
                logger.info(f"[QUEUE] Cleared {deleted_count} completed messages")
            return deleted_count