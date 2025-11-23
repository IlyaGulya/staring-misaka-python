"""End-to-End Integration Tests for Staring Misaka Bot

These tests use real Telegram test servers (test.telegram.org) to validate
the complete bot workflow from user joining to spam detection and banning.

Requirements:
- Telegram test server credentials (see tests/E2E_SETUP.md)
- Pre-created test bot (use scripts/setup_test_bot.py)
- Test user and admin accounts with StringSession generated
- Test group/supergroup where bot has admin rights

Environment Variables (see .env.test.example):
- TEST_API_ID, TEST_API_HASH - Telegram API credentials
- TEST_BOT_TOKEN - Bot token (REQUIRED, pre-created via scripts/setup_test_bot.py)
- TEST_USER_SESSION - User StringSession for simulating users
- TEST_ADMIN_SESSION - Admin StringSession for approval workflow
- TEST_GROUP_ID - Test group chat ID (optional, created automatically if not set)
- TEST_ADMIN_ID - Admin user ID

Run with: pixi run test-e2e
"""

import asyncio
import pytest_asyncio
import pytest
from datetime import datetime, UTC
from unittest.mock import AsyncMock

from db import NewUser, PendingBanRequest, BannedUser, AdminSettings, ApprovedUser, MessageQueue
from tests.conftest import wait_for_condition


# Mark all tests in this file as e2e and use session-scoped event loop
pytestmark = [pytest.mark.e2e, pytest.mark.asyncio(loop_scope="session")]


@pytest_asyncio.fixture(autouse=True)
async def reset_e2e_state(e2e_session_factory, mock_llm_e2e, e2e_test_group_id, e2e_admin_client, e2e_user_client):
    """Reset database and mocks between tests"""
    import logging
    yield

    import time
    start_time = time.time()
    logging.getLogger(__name__).info("[RESET_STATE] Starting cleanup")

    # Reset LLM mock
    mock_llm_e2e.reset_mock()
    mock_llm_e2e.is_spam.return_value = False
    mock_llm_e2e.is_spam.side_effect = None

    # Clear all data tables
    with e2e_session_factory() as session:
        session.query(MessageQueue).delete()
        session.query(NewUser).delete()
        session.query(BannedUser).delete()
        session.query(ApprovedUser).delete()
        session.query(PendingBanRequest).delete()

        # Reset AdminSettings to default (require_approval=False)
        admin_settings = session.query(AdminSettings).first()
        if admin_settings:
            admin_settings.require_approval = False
            session.commit()
        else:
            # Create default if missing
            admin_settings = AdminSettings(require_approval=False)
            session.add(admin_settings)
            session.commit()

    elapsed = time.time() - start_time
    logging.getLogger(__name__).info(f"[RESET_STATE] ✓ Cleanup completed in {elapsed:.2f}s")

    # No need to unban/reinvite user - each test creates a fresh group
    # via the e2e_test_group_id fixture, and the group is deleted after the test.
    # The next test will get a brand new group with the user added fresh.


class TestE2ESpamDetectionFlow:
    """Test complete spam detection and auto-ban workflow"""

    @pytest.mark.asyncio
    async def test_complete_spam_detection_auto_ban(
        self,
        e2e_world,
        mock_llm_e2e
    ):
        """Test complete flow: user joins → sends spam → auto-banned → messages purged"""
        # Configure LLM to detect spam
        mock_llm_e2e.is_spam = AsyncMock(return_value=True)

        # Get user info
        user = await e2e_world.user_client.get_me()
        user_id = user.id

        import logging
        logging.getLogger(__name__).info(f"[TEST] Looking for user_id={user_id}, group_id={e2e_world.group_id}")

        # Step 1: User joins group → ChatAction event → NewUser row created
        await e2e_world.user_join_group()
        await e2e_world.wait_for_user_tracked(user_id)

        # Step 2: User sends spam message
        spam_message = "🚨 URGENT: Buy crypto now! Limited offer! Click here: scam-link.com 💰"
        msg = await e2e_world.user_send(spam_message)

        # Step 3: Wait until queue item is processed
        await e2e_world.wait_for_queue_completion(user_id, msg.id)

        # Step 4: Assert spam result
        queue_item = e2e_world.db_get_queue_item(user_id, msg.id)
        assert queue_item is not None, "Queue item should exist"
        assert queue_item.spam_result is True, "Message should be detected as spam"

        # Step 5: User must be banned both in DB and in Telegram
        await e2e_world.wait_for_user_banned(user_id)

        await e2e_world.assert_user_banned_everywhere(user_id)

        # Step 6: User removed from monitoring
        assert e2e_world.db_get_new_user(user_id) is None, "User should be removed from monitoring after ban"


class TestE2EAdminApprovalWorkflow:
    """Test admin approval workflow for spam detection"""

    @pytest.mark.asyncio
    async def test_admin_approval_workflow(
        self,
        e2e_world,
        e2e_bot_username,
        mock_llm_e2e
    ):
        """Test spam detected → admin notified → admin approves → user banned"""
        # Enable admin approval for this test
        with e2e_world._session() as session:
            admin_settings = session.query(AdminSettings).first()
            admin_settings.require_approval = True
            session.commit()

        # Configure LLM to detect spam
        mock_llm_e2e.is_spam = AsyncMock(return_value=True)

        user = await e2e_world.user_client.get_me()
        user_id = user.id

        # Step 1: User joins group → ChatAction event → NewUser row created
        await e2e_world.user_join_group()
        await e2e_world.wait_for_user_tracked(user_id)

        # Step 2: User sends spam message
        spam_message = "URGENT: Bitcoin giveaway! Send 0.1 BTC to get 1 BTC back!"
        msg = await e2e_world.user_send(spam_message)

        # Step 3: Wait for queue processing
        await e2e_world.wait_for_queue_completion(user_id, msg.id)

        # Step 4: Wait for pending ban request to be created
        pending_request = await e2e_world.wait_for_pending_ban(user_id)
        assert pending_request is not None, "Pending ban request should be created"

        # Step 5: Admin approves the ban
        await e2e_world.admin_approve_ban(pending_request, e2e_bot_username)

        # Step 6: Verify user was banned in DB
        await e2e_world.wait_for_user_banned(user_id)

        # Step 8: Verify user is banned everywhere and pending request removed
        await e2e_world.assert_user_banned_everywhere(user_id)
        assert not e2e_world.db_has_pending_ban(user_id), "Pending ban request should be removed after approval"


class TestE2EAutoApprovalFlow:
    """Test auto-approval for legitimate users"""

    @pytest.mark.asyncio
    async def test_auto_approval_non_spam(
        self,
        e2e_world,
        mock_llm_e2e
    ):
        """Test legitimate user → sends non-spam → auto-approved → no longer monitored"""
        # Configure LLM to NOT detect spam
        mock_llm_e2e.is_spam = AsyncMock(return_value=False)

        user = await e2e_world.user_client.get_me()
        user_id = user.id

        # Step 1: User joins group → ChatAction event → NewUser row created
        await e2e_world.user_join_group()
        await e2e_world.wait_for_user_tracked(user_id)

        # Step 2: User sends legitimate message
        legit_message = "Hello everyone! Happy to join this group. Looking forward to learning about dependency injection."
        msg = await e2e_world.user_send(legit_message)

        # Step 3: Wait for processing
        await e2e_world.wait_for_queue_completion(user_id, msg.id)

        # Step 4: Verify message was NOT detected as spam
        queue_item = e2e_world.db_get_queue_item(user_id, msg.id)
        assert queue_item is not None, "Queue item should exist"
        assert queue_item.spam_result is False, "Message should NOT be detected as spam"

        # Step 5: Wait for auto-approval
        await e2e_world.wait_for_user_approved(user_id)

        # Step 6: Verify user was auto-approved and removed from monitoring
        assert e2e_world.db_get_new_user(user_id) is None, "User should be removed from monitoring after passing spam check"
        assert e2e_world.db_is_approved(user_id), "User should be in approved list"
        assert not e2e_world.db_is_banned(user_id), "User should NOT be banned"

        # Step 7: Verify subsequent messages are ignored (not queued)
        second_msg = await e2e_world.user_send("Another message from me")
        await asyncio.sleep(2)  # Give time for handlers to potentially queue (they shouldn't)

        second_queue = e2e_world.db_get_queue_item(user_id, second_msg.id)
        assert second_queue is None, "Approved user's messages should not be queued"


class TestE2EManualBanCommand:
    """Test manual /sban command by admin"""

    @pytest.mark.asyncio
    async def test_manual_sban_command_by_user_id(
        self,
        e2e_world
    ):
        """Test admin uses /sban <user_id> to manually ban a user"""
        user = await e2e_world.user_client.get_me()
        user_id = user.id

        # Ensure the user is in the group
        await e2e_world.user_join_group()

        # User sends some messages
        await e2e_world.user_send("Message 1")
        await e2e_world.user_send("Message 2")
        await e2e_world.user_send("Message 3")

        # Admin sends /sban command in the group
        # Start waiting BEFORE sending to avoid race condition
        wait_task = e2e_world.wait_for_ban_confirmation_in_group(user_id)
        await e2e_world.admin_send(e2e_world.group_id, f"/sban {user_id} 5")

        # Wait for bot to confirm the ban
        await wait_task

        # Verify user is banned in Telegram
        await e2e_world.assert_user_banned_everywhere(user_id)