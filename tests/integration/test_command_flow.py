# tests/integration/test_command_flow.py
from unittest.mock import AsyncMock, MagicMock

import pytest

from staring_misaka.db_models import MonitoredGroup
from tests.conftest import TEST_CHAT_ID_2, TEST_GROUP_ADMIN_ID, TEST_REGULAR_USER_ID, TEST_SUPER_ADMIN_ID

# Mark all tests in this file as async
pytestmark = pytest.mark.asyncio


async def test_add_group_command_by_admin(
        db_session, mock_telegram_client, command_handlers, event_handlers
):
    """
    GIVEN a user who is an admin in a chat (but not super admin)
    WHEN they use /add_group in that chat
    THEN the group should be added to MonitoredGroup table and cache updated.
    """
    # Arrange
    chat_id_to_add = TEST_CHAT_ID_2
    admin_user_id = TEST_GROUP_ADMIN_ID  # This user is mocked as admin of TEST_CHAT_ID_2 in conftest

    # Ensure the group doesn't exist yet
    existing = await db_session.get(MonitoredGroup, chat_id_to_add)
    assert existing is None

    mock_event = MagicMock(
        is_private=False,
        chat_id=chat_id_to_add,
        sender_id=admin_user_id,
        text="/add_group",
        reply=AsyncMock()
    )

    # Act
    await command_handlers.add_group_handler(mock_event)
    # REMOVED: await db_session.commit() # Commit is handled by command_handler's get_db_session context manager

    # Assert
    # The db_session fixture will roll back, so to check the state *after* the handler's commit
    # but before our test's rollback, we need to ensure the handler's session committed.
    # For assertion, we rely on the same session; the data will be visible.
    new_group = await db_session.get(MonitoredGroup, chat_id_to_add)
    assert new_group is not None
    assert new_group.chat_id == chat_id_to_add
    assert new_group.added_by_user_id == admin_user_id

    mock_event.reply.assert_called_once_with(
        "✅ Group successfully added for monitoring! New users' first messages will be checked."
    )
    event_handlers.update_monitored_chats_cache.assert_called_once()


async def test_add_group_command_by_non_admin(
        db_session, mock_telegram_client, command_handlers, event_handlers
):
    """
    GIVEN a user who is NOT an admin in a chat
    WHEN they use /add_group in that chat
    THEN the group should NOT be added and an error message sent.
    """
    # Arrange
    chat_id_to_add = TEST_CHAT_ID_2
    non_admin_user_id = TEST_REGULAR_USER_ID

    # Safeguard: Ensure the group doesn't exist from a previous test due to rollback failure.
    # This is more for robustness; db_session should handle perfect rollback.
    existing_group = await db_session.get(MonitoredGroup, chat_id_to_add)
    if existing_group:
        await db_session.delete(existing_group)
        await db_session.commit() # Commit this cleanup action if it happens

    mock_event = MagicMock(
        is_private=False,
        chat_id=chat_id_to_add,
        sender_id=non_admin_user_id,
        text="/add_group",
        reply=AsyncMock()
    )

    original_iter_participants = mock_telegram_client.iter_participants

    async def mock_iter_participants_no_admin_for_this_user(c_id, *args, filter=None, **kwargs):
        if c_id == chat_id_to_add and filter and filter.__name__ == 'ChannelParticipantsAdmins':
            # Yield a different admin, or no one
            yield MagicMock(spec=MagicMock, id=TEST_SUPER_ADMIN_ID, is_admin=True)
        else:  # Fallback to original mock for other cases if necessary
            async for p in original_iter_participants(c_id, *args, filter=filter, **kwargs):
                yield p
        if False: yield

    mock_telegram_client.iter_participants = mock_iter_participants_no_admin_for_this_user

    # Act
    await command_handlers.add_group_handler(mock_event)
    # REMOVED: await db_session.commit() # Commit is handled by command_handler's get_db_session

    # Assert
    group = await db_session.get(MonitoredGroup, chat_id_to_add)
    assert group is None

    mock_event.reply.assert_called_once_with(
        "Only group administrators can add this group for monitoring."
    )

    # Restore original mock if it was changed for this test specifically
    mock_telegram_client.iter_participants = original_iter_participants
