import asyncio
import logging
from datetime import datetime, timedelta, UTC
from typing import Optional

from sqlalchemy.orm import Session, sessionmaker
from telethon import TelegramClient

from db import MessageQueue, NewUser, PendingBanRequest, AdminSettings, ApprovedUser, BannedUser
from llm import Llm
from userbot import UserBot

logger = logging.getLogger(__name__)


class QueueProcessor:
    def __init__(
        self,
        session: Session,
        llm: Llm,
        userbot: UserBot,
        telegram_client: TelegramClient,
        config,
        *,
        processing_delay: float = 1.0,
        max_concurrent_jobs: int = 3,
    ):
        self.session = session
        self.session_factory = sessionmaker(bind=session.bind)
        self.llm = llm
        self.userbot = userbot
        self.telegram_client = telegram_client
        self.config = config
        self.running = False
        self.processing_delay = processing_delay  # Base delay between processing iterations
        self.max_concurrent_jobs = max_concurrent_jobs  # Max concurrent spam checks
        
    def _get_thread_safe_session(self) -> Session:
        """Create a new session for thread-safe database operations"""
        return self.session_factory()
        
    async def start(self):
        self.running = True
        logger.info("Queue processor started")
        
        while self.running:
            try:
                await self._process_pending_messages()
                await asyncio.sleep(self.processing_delay)
            except Exception as e:
                logger.error(f"Error in queue processor main loop: {str(e)}")
                await asyncio.sleep(5.0)  # Wait longer on error
                
    def stop(self):
        self.running = False
        logger.info("Queue processor stopped")
        
    async def _process_pending_messages(self):
        now = datetime.now(UTC)
        
        # Use a separate session for message selection to avoid lock conflicts
        with self._get_thread_safe_session() as selection_session:
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
        with self._get_thread_safe_session() as process_session:
            # Fetch the message
            queue_item = process_session.query(MessageQueue).filter_by(id=message_id).first()
            if not queue_item:
                logger.warning(f"Message {message_id} not found or already processed")
                return
                
            # Check if already being processed (should be 'claimed' from selection phase)
            if queue_item.status not in ['claimed', 'pending', 'failed']:
                logger.info(f"Message {message_id} is already being processed or completed")
                return
                
            await self._process_message(queue_item, process_session)
    
    async def _process_message(self, queue_item: MessageQueue, session: Session = None):
        # Use provided session or fall back to instance session
        if session is None:
            session = self.session
            
        logger.info(f"Processing message queue item {queue_item.id} for user {queue_item.user_id}")
        
        # Mark as processing
        queue_item.status = 'processing'
        queue_item.retry_count += 1
        session.commit()
        
        try:
            # Check if user is still being monitored
            new_user = session.query(NewUser).filter_by(
                user_id=queue_item.user_id, 
                chat_id=queue_item.chat_id
            ).first()
            
            if not new_user:
                logger.info(f"User {queue_item.user_id} no longer being monitored, marking queue item as completed")
                queue_item.status = 'completed'
                queue_item.processed_at = datetime.now(UTC)
                session.commit()
                return
                
            # Check if user is pre-approved
            approved_user = session.query(ApprovedUser).filter_by(
                user_id=queue_item.user_id,
                chat_id=queue_item.chat_id
            ).first()
            
            if approved_user:
                logger.info(f"User {queue_item.user_id} is pre-approved, marking queue item as completed")
                queue_item.status = 'completed'
                queue_item.processed_at = datetime.now(UTC)
                session.commit()
                return
            
            # Perform spam check
            logger.info(f"Performing spam check for user {queue_item.user_id}")
            is_spam = await self.llm.is_spam(queue_item.message_text)
            
            # Update queue item with result
            queue_item.spam_result = is_spam
            queue_item.status = 'completed'
            queue_item.processed_at = datetime.now(UTC)
            queue_item.error_message = None
            
            logger.info(f"Spam check completed for user {queue_item.user_id}: is_spam={is_spam}")
            
            # Process the spam result
            await self._handle_spam_result(queue_item, is_spam, session)
            
            session.commit()
            
        except Exception as e:
            error_msg = str(e)
            logger.error(f"Error processing message queue item {queue_item.id}: {error_msg}")
            
            # Update queue item with error
            queue_item.error_message = error_msg
            queue_item.status = 'failed'
            
            # Calculate next retry time with exponential backoff
            backoff_seconds = min(300, 30 * (2 ** (queue_item.retry_count - 1)))  # Max 5 minutes
            queue_item.next_retry_at = datetime.now(UTC) + timedelta(seconds=backoff_seconds)
            
            logger.info(f"Will retry message {queue_item.id} in {backoff_seconds} seconds (attempt {queue_item.retry_count}/{queue_item.max_retries})")
            
            session.commit()
            
    async def _handle_spam_result(self, queue_item: MessageQueue, is_spam: bool, session: Session = None):
        if session is None:
            session = self.session
            
        if is_spam:
            admin_settings = session.query(AdminSettings).first()
            
            if admin_settings.require_approval:
                # Notify admin
                await self._notify_admin(queue_item, session)
            else:
                # Automatically ban the user
                await self._process_ban(
                    user_id=queue_item.user_id,
                    chat_id=queue_item.chat_id,
                    message_id=queue_item.message_id,
                    message_text=queue_item.message_text,
                    is_automatic=True,
                    session=session
                )
        else:
            # If not spam, approve the user
            await self._auto_approve_user(queue_item.user_id, queue_item.chat_id, session)
            
    async def _notify_admin(self, queue_item: MessageQueue, session: Session = None):
        logger.info(f"Notifying admin about potential spam from user {queue_item.user_id}")
        
        try:
            # Get user name
            user_name = await self._get_user_name(queue_item.user_id)
            
            admin_message = (
                f"User {user_name} ({queue_item.user_id}) sent a message in chat {queue_item.chat_id}:\n\n"
                f"{queue_item.message_text}\n\nShould I ban this user? Reply 'yes' to ban."
            )
            sent_message = await self.telegram_client.send_message(self.config.admin_id, admin_message)
            logger.info(f"Sent admin notification message with ID: {sent_message.id}")
            
            # Store the pending request in the database
            pending_request = PendingBanRequest(
                admin_message_id=sent_message.id,
                sender_id=queue_item.user_id,
                original_chat_id=queue_item.chat_id,
                original_message_id=queue_item.message_id,
                message_text=queue_item.message_text,
                created_at=datetime.now()
            )
            if session is None:
                session = self.session
            session.add(pending_request)
            logger.info(f"Added pending ban request for user {queue_item.user_id} to database")
            
        except Exception as e:
            logger.error(f"Error notifying admin about user {queue_item.user_id}: {str(e)}")
            raise
            
    async def _process_ban(self, user_id: int, chat_id: int, message_id: int, message_text: str, is_automatic: bool, session: Session = None):
        logger.info(f"{'Automatically banning' if is_automatic else 'Admin approved ban for'} user {user_id}")

        try:
            # Send the ban command
            reason = f"autoban by staring misaka. message: {message_text}"
            await self.userbot.send_ban_command(chat_id, message_id, reason)

            # Store the ban information in the database
            user_name = await self._get_user_name(user_id)
            banned_user = BannedUser(
                user_id=user_id,
                user_name=user_name,
                chat_id=chat_id,
                message_text=message_text,
                banned_at=datetime.now()
            )
            if session is None:
                session = self.session
            session.add(banned_user)

            # Remove the user from NewUser table if they're still there
            new_user = session.query(NewUser).filter_by(user_id=user_id, chat_id=chat_id).first()
            if new_user:
                session.delete(new_user)

            logger.info(f"Stored ban information for user {user_id}")

            # Notify admin about the ban
            admin_message = (
                f"User {user_id} has been {'automatically ' if is_automatic else ''}banned "
                f"{'due to spam detection' if is_automatic else 'as per admin approval'}."
            )
            await self.telegram_client.send_message(self.config.admin_id, admin_message)
            
        except Exception as e:
            logger.error(f"Error processing ban for user {user_id}: {str(e)}")
            raise
            
    async def _auto_approve_user(self, user_id: int, chat_id: int, session: Session = None):
        logger.info(f"Auto-approving user {user_id} in chat {chat_id}")
        
        try:
            if session is None:
                session = self.session
                
            # Remove from monitoring
            new_user = session.query(NewUser).filter_by(user_id=user_id, chat_id=chat_id).first()
            if new_user:
                session.delete(new_user)
                logger.info(f"User {user_id} removed from monitoring in chat {chat_id}")
                
            # Add to approved users list
            existing_approval = session.query(ApprovedUser).filter_by(user_id=user_id, chat_id=chat_id).first()
            if not existing_approval:
                approved_user = ApprovedUser(user_id=user_id, chat_id=chat_id, approved_at=datetime.now())
                session.add(approved_user)
                logger.info(f"User {user_id} auto-approved and added to approved list for chat {chat_id}")
                
        except Exception as e:
            logger.error(f"Error auto-approving user {user_id}: {str(e)}")
            raise
            
    async def _get_user_name(self, user_id: int) -> Optional[str]:
        try:
            user = await self.telegram_client.get_entity(user_id)
            return user.username if user.username else user.first_name
        except Exception as e:
            logger.error(f"Error fetching user name for user_id {user_id}: {str(e)}")
            return f"User_{user_id}"
            
    def add_message_to_queue(self, user_id: int, chat_id: int, message_id: int, message_text: str) -> MessageQueue:
        """Add a message to the processing queue"""
        logger.info(f"Adding message to queue for user {user_id} in chat {chat_id}")
        
        # Check if message is already in queue
        existing = self.session.query(MessageQueue).filter_by(
            user_id=user_id,
            chat_id=chat_id,
            message_id=message_id
        ).first()
        
        if existing:
            logger.info(f"Message already in queue: {existing.id}")
            return existing
            
        queue_item = MessageQueue(
            user_id=user_id,
            chat_id=chat_id,
            message_id=message_id,
            message_text=message_text,
            status='pending',
            created_at=datetime.now()
        )
        
        self.session.add(queue_item)
        self.session.commit()
        
        logger.info(f"Added message to queue with ID: {queue_item.id}")
        return queue_item
        
    def get_queue_status(self):
        """Get current queue status for admin"""
        # Use thread-safe session to handle concurrent access
        with self._get_thread_safe_session() as session:
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
        # Use thread-safe session to handle concurrent access
        with self._get_thread_safe_session() as session:
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
                logger.info(f"Reset {count} failed messages to pending status")
                return count
            except Exception:
                # If there's any database error, return 0
                return 0
        
    def clear_completed_messages(self, older_than_hours: int = 24) -> int:
        """Clear completed messages older than specified hours"""
        cutoff_time = datetime.now(UTC) - timedelta(hours=older_than_hours)
        
        deleted_count = self.session.query(MessageQueue).filter(
            MessageQueue.status == 'completed',
            MessageQueue.processed_at < cutoff_time
        ).delete()
        
        self.session.commit()
        logger.info(f"Cleared {deleted_count} completed messages older than {older_than_hours} hours")
        return deleted_count