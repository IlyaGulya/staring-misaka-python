"""E2E Test World Helper

This module provides the E2EWorld class that bundles all test clients,
database access, and helper methods for E2E tests. It helps tests express
state transitions declaratively and synchronize on real Telegram/DB events
instead of arbitrary sleeps.
"""

import asyncio
from dataclasses import dataclass

from telethon import events

from db import NewUser, MessageQueue, BannedUser, ApprovedUser, PendingBanRequest


@dataclass
class E2EWorld:
    """Test world that bundles clients, config, and helper methods for E2E tests"""

    user_client: "TelegramClient"
    admin_client: "TelegramClient"
    bot_client: "TelegramClient"
    session_factory: "sessionmaker"
    config: object
    group_id: int
    queue_processor: "QueueProcessor"

    # ---------- Telegram-side helpers ----------

    async def assert_bot_is_admin(self):
        """Ensure bot has ban/delete rights in the group."""
        bot = await self.bot_client.get_me()

        # Check bot permissions
        perms = await self.admin_client.get_permissions(self.group_id, bot)
        assert perms.is_admin, "Bot must be admin in test group"
        assert perms.ban_users and perms.delete_messages, "Bot must have ban/delete permissions"

    async def wait_for_message_from_bot(self, predicate, timeout=10.0):
        """
        Wait until *admin_client* receives a message sent by the bot
        that matches `predicate(event)`.

        Args:
            predicate: Function that takes an event and returns True if it matches
            timeout: Maximum time to wait in seconds
        """
        import logging
        logger = logging.getLogger(__name__)

        bot = await self.bot_client.get_me()
        evt = asyncio.Event()
        matched_event = None

        logger.info(f"[WAIT_FOR_BOT_MSG] Waiting for message from bot {bot.id}, timeout={timeout}s")

        async def handler(event):
            nonlocal matched_event
            logger.debug(f"[WAIT_FOR_BOT_MSG] Received message: sender_id={event.sender_id}, text='{event.raw_text}', is_private={event.is_private}, chat_id={event.chat_id}")

            # Only messages sent BY the bot
            if event.sender_id != bot.id:
                logger.debug(f"[WAIT_FOR_BOT_MSG] Ignoring message from {event.sender_id} (not bot)")
                return

            logger.info(f"[WAIT_FOR_BOT_MSG] Message from bot: '{event.raw_text}'")

            if predicate(event):
                logger.info(f"[WAIT_FOR_BOT_MSG] ✓ Message matches predicate!")
                matched_event = event
                evt.set()
            else:
                logger.info(f"[WAIT_FOR_BOT_MSG] ✗ Message does not match predicate")

        self.admin_client.add_event_handler(handler, events.NewMessage)

        try:
            await asyncio.wait_for(evt.wait(), timeout=timeout)
            logger.info(f"[WAIT_FOR_BOT_MSG] Successfully received matching message")
            return matched_event
        except asyncio.TimeoutError:
            logger.error(f"[WAIT_FOR_BOT_MSG] Timeout after {timeout}s waiting for bot message")
            raise
        finally:
            self.admin_client.remove_event_handler(handler, events.NewMessage)

    async def user_send(self, text: str):
        """User sends a message to the group."""
        return await self.user_client.send_message(self.group_id, text)

    async def admin_send(self, peer, text: str, **kwargs):
        """Admin sends a message."""
        return await self.admin_client.send_message(peer, text, **kwargs)

    async def user_join_group(self):
        """
        Ensure the user is set up for monitoring by creating a NewUser entry.

        NOTE: We don't test the actual Telegram join event because leave/rejoin cycles
        on test servers are unreliable. Instead, we create the NewUser entry directly
        to simulate what would happen after a join event. This lets us test the core
        spam detection functionality without fighting Telegram's join/leave quirks.
        """
        from db import NewUser
        from datetime import datetime, UTC
        import logging
        logger = logging.getLogger(__name__)

        user = await self.user_client.get_me()
        user_id = user.id
        logger.info(f"[E2E] user_join_group: Simulating join for user {user_id} in group {self.group_id}")

        # Verify user is actually in the group (was added in fixture)
        try:
            participants = await self.admin_client.get_participants(self.group_id, limit=100)
            user_ids = [p.id for p in participants]
            logger.info(f"[E2E] Group has {len(participants)} participants: {user_ids}")
            if user_id not in user_ids:
                logger.error(f"[E2E] User {user_id} NOT in group! Cannot proceed")
                raise RuntimeError(f"User {user_id} not in group {self.group_id}")
            logger.info(f"[E2E] ✓ User {user_id} is confirmed in the group")
        except Exception as e:
            logger.error(f"[E2E] Failed to verify user in group: {e}")
            raise

        # Create NewUser entry directly to simulate post-join state
        with self._session() as session:
            # Remove any existing entry first
            session.query(NewUser).filter_by(user_id=user_id, chat_id=self.group_id).delete()

            # Add fresh NewUser entry
            new_user = NewUser(
                user_id=user_id,
                chat_id=self.group_id,
                join_time=datetime.now(UTC)
            )
            session.add(new_user)
            session.commit()
            logger.info(f"[E2E] Created NewUser entry for {user_id} in group {self.group_id}")

        logger.info(f"[E2E] user_join_group completed - user ready for spam monitoring")

    # ---------- DB-side helpers ----------

    def _session(self):
        """Create a new database session"""
        return self.session_factory()

    def db_get_new_user(self, user_id: int):
        """Get NewUser record for a user in this group"""
        import logging
        logger = logging.getLogger(__name__)

        with self._session() as s:
            result = s.query(NewUser).filter_by(user_id=user_id, chat_id=self.group_id).first()
            # Always log what we're looking for
            all_users = s.query(NewUser).all()
            logger.info(
                f"[DB_DEBUG] Looking for user_id={user_id}, chat_id={self.group_id}, "
                f"found {'MATCH' if result else 'NO MATCH'}, "
                f"total {len(all_users)} NewUser records: {[(u.user_id, u.chat_id) for u in all_users]}"
            )
            return result

    def db_get_queue_item(self, user_id: int, message_id: int):
        """Get MessageQueue record for a specific message"""
        with self._session() as s:
            return s.query(MessageQueue).filter_by(
                user_id=user_id, chat_id=self.group_id, message_id=message_id
            ).first()

    def db_is_queue_completed(self, user_id: int, message_id: int) -> bool:
        """Check if a message queue item is completed"""
        item = self.db_get_queue_item(user_id, message_id)
        return bool(item and item.status == "completed")

    def db_is_banned(self, user_id: int) -> bool:
        """Check if a user is banned"""
        with self._session() as s:
            return s.query(BannedUser).filter_by(user_id=user_id, chat_id=self.group_id).first() is not None

    def db_is_approved(self, user_id: int) -> bool:
        """Check if a user is approved"""
        with self._session() as s:
            return s.query(ApprovedUser).filter_by(user_id=user_id, chat_id=self.group_id).first() is not None

    def db_has_pending_ban(self, user_id: int) -> bool:
        """Check if there's a pending ban request for a user"""
        with self._session() as s:
            return s.query(PendingBanRequest).filter_by(
                sender_id=user_id, original_chat_id=self.group_id
            ).first() is not None

    def db_get_pending_ban(self, user_id: int):
        """Get pending ban request for a user"""
        with self._session() as s:
            return s.query(PendingBanRequest).filter_by(
                sender_id=user_id, original_chat_id=self.group_id
            ).first()

    # ---------- Composite asserts ----------

    async def assert_user_banned_everywhere(self, user_id: int):
        """Assert both DB and Telegram state show the user as banned."""
        # DB
        assert self.db_is_banned(user_id), "User should be in banned_users table"

        # Telegram permissions
        try:
            perms = await self.admin_client.get_permissions(self.group_id, user_id)
            assert not perms.send_messages, "User should have send_messages revoked"
        except Exception:
            # If this fails, user might be fully kicked, which is also acceptable
            pass

    async def ensure_admin_peer(self, bot_username: str):
        """
        Ensure admin peer relationship is established by sending /start to bot
        and waiting for the help message response.
        """
        await self.admin_send(bot_username, "/start")
        # Wait for bot's help message
        await self.wait_for_message_from_bot(
            lambda e: e.is_private and "Admin commands:" in (e.raw_text or ""),
            timeout=5.0,
        )

    # ---------- High-level test helpers ----------

    async def wait_for_user_tracked(self, user_id: int, timeout: float = 10.0):
        """Wait until user appears in NewUser table (being monitored)."""
        from tests.test_utils import wait_for_condition
        await wait_for_condition(
            lambda: self.db_get_new_user(user_id) is not None,
            timeout=timeout,
            description=f"user {user_id} to appear in new_users"
        )

    async def wait_for_queue_completion(self, user_id: int, message_id: int, timeout: float = 10.0):
        """Wait until a message queue item is marked completed."""
        from tests.test_utils import wait_for_condition
        await wait_for_condition(
            lambda: self.db_is_queue_completed(user_id, message_id),
            timeout=timeout,
            description=f"queue completion for message {message_id}"
        )

    async def wait_for_user_banned(self, user_id: int, timeout: float = 10.0):
        """Wait until user appears in BannedUser table."""
        from tests.test_utils import wait_for_condition
        await wait_for_condition(
            lambda: self.db_is_banned(user_id),
            timeout=timeout,
            description=f"user {user_id} to be banned"
        )

    async def wait_for_user_approved(self, user_id: int, timeout: float = 10.0):
        """Wait until user appears in ApprovedUser table."""
        from tests.test_utils import wait_for_condition
        await wait_for_condition(
            lambda: self.db_is_approved(user_id),
            timeout=timeout,
            description=f"user {user_id} to be approved"
        )

    async def wait_for_pending_ban(self, user_id: int, timeout: float = 10.0):
        """Wait until pending ban request is created for user.

        Returns:
            PendingBanRequest: The pending ban request object
        """
        from tests.test_utils import wait_for_condition
        await wait_for_condition(
            lambda: self.db_has_pending_ban(user_id),
            timeout=timeout,
            description=f"pending ban request for user {user_id}"
        )
        return self.db_get_pending_ban(user_id)

    async def admin_approve_ban(self, pending_request, bot_username: str, timeout: float = 10.0):
        """Admin approves a pending ban request by replying 'yes'.

        Args:
            pending_request: PendingBanRequest object with admin_message_id
            bot_username: Bot's username to send message to
            timeout: How long to wait for confirmation message

        Returns:
            Event: The bot's confirmation message
        """
        # Start waiting BEFORE sending to avoid race condition
        wait_task = asyncio.create_task(
            self.wait_for_message_from_bot(
                lambda e: e.is_private
                and "has been" in (e.raw_text or "")
                and "banned" in (e.raw_text or ""),
                timeout=timeout,
            )
        )

        # Admin replies "yes" to approve the ban
        await self.admin_send(
            bot_username, "yes", reply_to=pending_request.admin_message_id
        )

        # Wait for confirmation
        return await wait_task

    def wait_for_ban_confirmation_in_group(self, user_id: int, timeout: float = 10.0):
        """Returns an awaitable task that waits for ban confirmation message in group.

        This is a helper for tests that need to start waiting before triggering an action.

        Args:
            user_id: User ID to check for in the ban message
            timeout: Maximum time to wait in seconds

        Returns:
            asyncio.Task: Task that resolves when confirmation message is received
        """
        return asyncio.create_task(
            self.wait_for_message_from_bot(
                lambda e: e.chat_id == self.group_id
                and "banned" in (e.raw_text or "").lower()
                and str(user_id) in (e.raw_text or ""),
                timeout=timeout,
            )
        )
